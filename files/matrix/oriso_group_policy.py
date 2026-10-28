"""AVV admission companion for the reviewed Synapse1.158.0 storage contract.

The native callback only removes the AVV veto for proven earlier participation.
Synapse still applies ordinary authentication, invitations, bans and room rules.
"""
import hmac
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import synapse
import treq
from synapse.api.errors import Codes, SynapseError
from synapse.http.server import DirectServeJsonResource
from synapse.module_api import NOT_SPAM, make_deferred_yieldable, run_in_background
from synapse.storage.engines import PostgresEngine
from synapse.types import RoomID, UserID
from twisted.internet import reactor
from twisted.web.client import Agent
from twisted.web.http_headers import Headers

SCHEMA = 'synapse-1.158.0-applied-membership-v1'
TOKEN_HEADER = b'X-Oriso-Group-Policy-Token'
HISTORY_PATH = '/oriso-internal/group-participation-history'
UNAVAILABLE = 'DPA_POLICY_UNAVAILABLE'
DENIED = 'DPA_NEW_COUNSELLING_NOT_ALLOWED'
logger = logging.getLogger(__name__)
_INITIALIZATIONS = WeakKeyDictionary()


def unavailable():
    return SynapseError(502, 'Counselling verification is temporarily unavailable',
                        Codes.UNKNOWN, additional_fields={'reason': UNAVAILABLE})


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('Duplicate property')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def valid_room(value):
    return isinstance(value, str) and len(value) <= 255 and RoomID.is_valid(value)


def valid_actor(value):
    return isinstance(value, str) and len(value) <= 255 and UserID.is_valid(value)


def settings(config, policy=False):
    if synapse.__version__ != '1.158.0':
        raise ValueError('AVV membership schema requires reviewed Synapse1.158.0')
    expected = {'token_file', 'policy_url'} if policy else {'token_file'}
    if not isinstance(config, dict) or set(config) != expected:
        raise ValueError('Invalid AVV module configuration')
    token = Path(config['token_file']).read_bytes().strip()
    if len(token) < 32 or len(token) > 256 or any(byte < 33 or byte > 126 for byte in token):
        raise ValueError('AVV module requires a dedicated 32-256 byte credential')
    if policy:
        target = urlsplit(config['policy_url'])
        if (target.scheme != 'http' or target.hostname != 'userservice'
                or target.port != 8080 or target.path != '/internal/matrix/group-join-policy'
                or target.username or target.password or target.query or target.fragment):
            # A credential-bearing callback cannot be redirected to an arbitrary origin.
            # The isolated integration fixture gives its owner stub this service name.
            raise ValueError('AVV policy URL must be the cluster UserService endpoint')
    return token


class AppliedHistory:
    def __init__(self, api):
        self.api = api
        # The callback and private resource receive the same ModuleApi. Start
        # DDL once: concurrent Postgres CREATE TABLE IF NOT EXISTS can still
        # race in pg_type's unique index even when the final table is identical.
        if api not in _INITIALIZATIONS:
            _INITIALIZATIONS[api] = api.run_db_interaction('oriso_avv_history_schema', self._initialize)
        self.ready = _INITIALIZATIONS[api]

    @staticmethod
    def _initialize(txn):
        # Module-owned metadata only. Normal room purge does not retain message
        # content here; an explicit final group deletion removes these receipts.
        txn.execute('''CREATE TABLE IF NOT EXISTS oriso_avv_join_receipts (
            room_id TEXT NOT NULL, user_id TEXT NOT NULL, event_id TEXT NOT NULL,
            PRIMARY KEY (room_id, user_id))''')
        txn.execute('''CREATE TABLE IF NOT EXISTS oriso_avv_history_rooms (
            room_id TEXT PRIMARY KEY, complete INTEGER NOT NULL)''')

    async def commenced(self, room, actor):
        if not valid_room(room) or not valid_actor(actor) or not self.api.is_mine(actor):
            raise unavailable()
        try:
            await self.ready
            return await self.api.run_db_interaction('oriso_avv_applied_join', self._read, room, actor)
        except Exception as failure:
            # SQL/schema/decoding details can contain identifiers; never expose them.
            logger.warning('AVV verification unavailable: category=history type=%s', type(failure).__name__)
            raise unavailable() from None

    async def known_positive(self, room, actor):
        """Read-only fast path; unrelated Matrix rooms must not gain a ledger."""
        if not valid_room(room) or not valid_actor(actor) or not self.api.is_mine(actor):
            raise unavailable()
        try:
            await self.ready
            return await self.api.run_db_interaction('oriso_avv_existing_join', self._positive, room, actor)
        except Exception:
            raise unavailable() from None

    @staticmethod
    def _positive(txn, room, actor):
        txn.execute('SELECT complete FROM oriso_avv_history_rooms WHERE room_id = ?', (room,))
        marker = txn.fetchone()
        if marker is not None and marker[0] == -1:
            raise ValueError('Retired group is unavailable')
        return AppliedHistory._receipt(txn, room, actor) or bool(AppliedHistory._proofs(txn, room, actor))

    @staticmethod
    def _read(txn, room, actor):
        txn.execute('SELECT complete FROM oriso_avv_history_rooms WHERE room_id = ?', (room,))
        marker = txn.fetchone()
        if marker is not None and marker[0] == -1:
            raise ValueError('Retired group history unavailable')
        if AppliedHistory._receipt(txn, room, actor):
            return True
        # Positive proof is usable even if unrelated legacy history has a gap.
        proofs = AppliedHistory._proofs(txn, room, actor)
        if proofs:
            AppliedHistory._save(txn, room, proofs)
            return True
        AppliedHistory._snapshot(txn, room)
        return AppliedHistory._receipt(txn, room, actor)

    @staticmethod
    def _receipt(txn, room, actor):
        txn.execute('''SELECT 1 FROM oriso_avv_join_receipts r
                        JOIN users u ON u.name = r.user_id
                       WHERE r.room_id = ? AND r.user_id = ? AND u.deactivated = ?''', (room, actor, 0))
        return txn.fetchone() is not None

    @staticmethod
    def _proofs(txn, room, actor=None, applied=True):
        # room_memberships includes outliers and may include failed candidates. It
        # alone is NOT participation proof. Only accepted, forward-persisted,
        # non-outlier/non-partial, non-soft-failed self JOINs are considered.
        # The local-only server keeps pre-module membership events in this store.
        actor_filter = ' AND m.user_id = ? AND m.sender = ?' if actor else ' AND m.sender = m.user_id'
        applied_filter = ''' AND EXISTS (SELECT 1 FROM current_state_delta_stream d
                            WHERE d.event_id = e.event_id AND d.room_id = m.room_id
                              AND d.type = 'm.room.member' AND d.state_key = m.user_id)''' if applied else ''
        txn.execute('''
            SELECT m.user_id, m.event_id, j.internal_metadata, j.json
              FROM room_memberships m
              JOIN events e ON e.event_id = m.event_id AND e.room_id = m.room_id
              JOIN event_json j ON j.event_id = e.event_id AND j.room_id = e.room_id
              JOIN users u ON u.name = m.user_id AND u.deactivated = ?
              LEFT JOIN rejections r ON r.event_id = e.event_id
              LEFT JOIN partial_state_events p ON p.event_id = e.event_id
             WHERE m.room_id = ?
               AND m.membership = 'join' AND e.type = 'm.room.member'
               AND e.outlier = ? AND e.stream_ordering > 0
               AND m.event_stream_ordering = e.stream_ordering
               AND r.event_id IS NULL AND p.event_id IS NULL
        ''' + actor_filter + applied_filter, (0, room, False, *([actor, actor] if actor else [])))
        proofs = []
        for member, event_id, metadata, raw in txn:
            meta, event = strict_json(metadata), strict_json(raw)
            if not isinstance(meta, dict) or not isinstance(event, dict):
                raise ValueError('Unusable membership evidence')
            if meta.get('soft_failed', False) is True or meta.get('outlier', False) is True:
                continue
            if (meta.get('soft_failed', False) is not False
                    or meta.get('outlier', False) is not False):
                raise ValueError('Unusable membership metadata')
            if (event.get('room_id') == room and event.get('state_key') == member
                    and event.get('sender') == member and event.get('type') == 'm.room.member'
                    and isinstance(event.get('content'), dict)
                    and event['content'].get('membership') == 'join'):
                proofs.append((member, event_id))
            else:
                raise ValueError('Contradictory stored membership evidence')
        return proofs

    @staticmethod
    def _save(txn, room, proofs):
        if AppliedHistory._lock_room(txn, room) == -1:
            return
        for actor, event_id in proofs:
            # Serialize receipt inserts with native deactivation. SQLite's room
            # write lock serializes the transaction; Postgres shares the native
            # users row so deactivation's UPDATE must precede or follow the
            # insert, never slip between the active check and receipt commit.
            suffix = ' FOR SHARE' if isinstance(txn.database_engine, PostgresEngine) else ''
            txn.execute('SELECT deactivated FROM users WHERE name = ?' + suffix, (actor,))
            user = txn.fetchone()
            if user is None or user[0]:
                continue
            txn.execute('''INSERT INTO oriso_avv_join_receipts (room_id, user_id, event_id)
                           VALUES (?, ?, ?) ON CONFLICT (room_id, user_id) DO NOTHING''',
                        (room, actor, event_id))

    @staticmethod
    def _lock_room(txn, room):
        txn.execute('''INSERT INTO oriso_avv_history_rooms (room_id, complete) VALUES (?, 0)
                       ON CONFLICT (room_id) DO NOTHING''', (room,))
        txn.execute('UPDATE oriso_avv_history_rooms SET complete = complete WHERE room_id = ?', (room,))
        txn.execute('SELECT complete FROM oriso_avv_history_rooms WHERE room_id = ?', (room,))
        return txn.fetchone()[0]

    @staticmethod
    def _snapshot(txn, room):
        if AppliedHistory._lock_room(txn, room) == -1:
            raise ValueError('Retired group cannot be snapshotted')
        txn.execute('SELECT 1 FROM rooms WHERE room_id = ?', (room,))
        if txn.fetchone() is None:
            txn.execute('SELECT 1 FROM oriso_avv_history_rooms WHERE room_id = ? AND complete = 1', (room,))
            if txn.fetchone():
                return
            raise ValueError('History coverage unavailable')
        txn.execute('''SELECT 1 FROM current_state_delta_stream d
                        JOIN events e ON e.event_id = d.event_id AND e.room_id = d.room_id
                        JOIN event_json j ON j.event_id = e.event_id
                       WHERE d.room_id = ? AND d.type = 'm.room.create'
                         AND e.outlier = ? AND e.stream_ordering > 0 LIMIT 1''', (room, False))
        if txn.fetchone() is None:
            raise ValueError('Room origin coverage unavailable')
        proofs = AppliedHistory._proofs(txn, room)
        candidates = AppliedHistory._proofs(txn, room, applied=False)
        if set(candidates) - set(proofs):
            # An accepted candidate without its application record might be a
            # pruned historical JOIN. Absence must not become a fabricated miss.
            raise ValueError('History application coverage unavailable')
        txn.execute('''SELECT 1 FROM current_state_delta_stream d
                        JOIN users u ON u.name = d.state_key AND u.deactivated = ?
                        LEFT JOIN events e ON e.event_id = d.event_id
                        LEFT JOIN event_json j ON j.event_id = d.event_id
                       WHERE d.room_id = ? AND d.type = 'm.room.member'
                         AND d.event_id IS NOT NULL AND (e.event_id IS NULL OR j.event_id IS NULL)
                       LIMIT 1''', (0, room))
        if txn.fetchone():
            raise ValueError('History retention coverage unavailable')
        AppliedHistory._save(txn, room, proofs)
        txn.execute('''INSERT INTO oriso_avv_history_rooms (room_id, complete) VALUES (?, 1)
                       ON CONFLICT (room_id) DO UPDATE SET complete = 1''', (room,))

    async def snapshot(self, room):
        try:
            await self.ready
            await self.api.run_db_interaction('oriso_avv_snapshot_room', self._snapshot, room)
        except Exception:
            raise unavailable() from None

    async def forget(self, room):
        def remove(txn):
            txn.execute('''INSERT INTO oriso_avv_history_rooms (room_id, complete) VALUES (?, -1)
                           ON CONFLICT (room_id) DO UPDATE SET complete = -1''', (room,))
            txn.execute('DELETE FROM oriso_avv_join_receipts WHERE room_id = ?', (room,))
        try:
            await self.ready
            await self.api.run_db_interaction('oriso_avv_forget_room', remove)
        except Exception:
            raise unavailable() from None

    async def record(self, room, actor):
        def save(txn):
            txn.execute('SELECT complete FROM oriso_avv_history_rooms WHERE room_id = ?', (room,))
            scoped = txn.fetchone()
            if scoped is None or scoped[0] == -1:
                return
            self._save(txn, room, self._proofs(txn, room, actor))
        await self.ready
        await self.api.run_db_interaction('oriso_avv_record_join', save)

    async def forget_actor(self, actor):
        def remove(txn):
            txn.execute('DELETE FROM oriso_avv_join_receipts WHERE user_id = ?', (actor,))
        try:
            await self.ready
            await self.api.run_db_interaction('oriso_avv_forget_actor', remove)
        except Exception as failure:
            logger.warning('AVV receipt cleanup failed: category=actor type=%s', type(failure).__name__)
            raise unavailable() from None

    async def reconcile(self):
        def remove(txn):
            txn.execute('''DELETE FROM oriso_avv_join_receipts
                            WHERE NOT EXISTS (
                              SELECT 1 FROM users u
                               WHERE u.name = oriso_avv_join_receipts.user_id
                                 AND u.deactivated = ?)
                        ''', (0,))
        try:
            await self.ready
            await self.api.run_db_interaction('oriso_avv_reconcile_receipts', remove)
        except Exception as failure:
            # Native deactivation is already committed and Synapse logs a
            # callback failure without retrying it. Keep retrying only owned
            # metadata, without credentials, actor IDs or raw SQL in logs.
            logger.warning('AVV receipt cleanup pending: category=reconcile type=%s', type(failure).__name__)


class HistoryResource(DirectServeJsonResource):
    """Mounted ONLY on the separate private listener.additional_resources."""
    def __init__(self, config, api):
        super().__init__()
        self.token = settings(config)
        self.history = AppliedHistory(api)

    @staticmethod
    def parse_config(config):
        return config

    async def _async_render_POST(self, request):
        token = request.requestHeaders.getRawHeaders(TOKEN_HEADER, [])
        if len(token) != 1 or not hmac.compare_digest(token[0], self.token):
            raise SynapseError(401, 'Unauthorized', Codes.UNAUTHORIZED)
        content = request.requestHeaders.getRawHeaders(b'Content-Type', [])
        try:
            if len(content) != 1 or content[0].split(b';')[0].strip().lower() != b'application/json':
                raise ValueError('Invalid content type')
            raw = request.content.read(4097)
            body = strict_json(raw)
            if (len(raw) <= 4096 and isinstance(body, dict)
                    and set(body) == {'contractVersion', 'operation', 'roomId'}
                    and type(body['contractVersion']) is int and body['contractVersion'] == 2
                    and body['operation'] in ('SNAPSHOT_ROOM', 'FORGET_ROOM') and valid_room(body['roomId'])):
                if body['operation'] == 'SNAPSHOT_ROOM':
                    await self.history.snapshot(body['roomId'])
                else:
                    await self.history.forget(body['roomId'])
                return 200, {'contractVersion': 2, 'roomId': body['roomId'], 'complete': True}
            if (len(raw) > 4096 or not isinstance(body, dict)
                    or set(body) != {'contractVersion', 'schemaVersion', 'roomId', 'matrixUserId'}
                    or type(body['contractVersion']) is not int or body['contractVersion'] != 1
                    or body['schemaVersion'] != SCHEMA
                    or not valid_room(body['roomId']) or not valid_actor(body['matrixUserId'])):
                raise ValueError('Invalid contract')
        except SynapseError:
            raise
        except Exception:
            raise SynapseError(400, 'Invalid participation request', Codes.BAD_JSON) from None
        commenced = await self.history.commenced(body['roomId'], body['matrixUserId'])
        return 200, {**body, 'participation': 'COMMENCED' if commenced else 'NOT_COMMENCED'}


class GroupPolicy:
    def __init__(self, config, api):
        self.token = settings(config, policy=True)
        self.url = config['policy_url']
        self.api = api
        # The only allowed target is the fixed cluster UserService. Do not
        # route a dedicated policy credential through a configurable web proxy.
        self.agent = Agent(reactor, connectTimeout=5)
        self.history = AppliedHistory(api)
        run_in_background(self.history.reconcile)
        api.looping_background_call(self.history.reconcile, 10000, desc='oriso_avv_receipt_reconciliation')
        api.register_spam_checker_callbacks(user_may_join_room=self.user_may_join_room)
        api.register_third_party_rules_callbacks(on_new_event=self.on_new_event,
            on_user_deactivation_status_changed=self.on_user_deactivation_status_changed)

    async def on_new_event(self, event, state_events):
        if event.type == 'm.room.member' and event.content.get('membership') == 'join':
            try:
                await self.history.record(event.room_id, event.state_key)
            except Exception as failure:
                logger.warning('AVV verification unavailable: category=receipt type=%s', type(failure).__name__)

    async def on_user_deactivation_status_changed(self, actor, deactivated, by_admin):
        if deactivated:
            await self.history.forget_actor(actor)

    @staticmethod
    def parse_config(config):
        return config

    async def _request(self, body, limit):
        try:
            response = await make_deferred_yieldable(treq.request(
                'POST', self.url, data=json.dumps(body).encode(),
                headers=Headers({TOKEN_HEADER: [self.token], b'Content-Type': [b'application/json'], b'Connection': [b'close']}),
                agent=self.agent, allow_redirects=False,
                unbuffered=True, timeout=5))
            chunks = bytearray()

            def collect(chunk):
                if len(chunks) + len(chunk) > limit:
                    raise ValueError('Response too large')
                chunks.extend(chunk)

            await make_deferred_yieldable(treq.collect(response, collect).addTimeout(5, reactor))
            # Consume bounded error bodies too; an unread response must not
            # accumulate connections during repeated downstream failures.
            content_type = response.headers.getRawHeaders(b'Content-Type', [])
            json_response = len(content_type) == 1 and content_type[0].split(b';')[0].strip().lower() == b'application/json'
            if (response.code == 403 and json_response
                    and response.headers.getRawHeaders(b'X-Reason', []) == [DENIED.encode()]
                    and strict_json(chunks) == {'reason': DENIED}):
                return None
            if response.code != 200 or not json_response:
                raise unavailable()
            return strict_json(chunks)
        except Exception as failure:
            logger.warning('AVV verification unavailable: category=owner type=%s causes=%s',
                           type(failure).__name__, ','.join(type(item.value).__name__ for item in getattr(failure, 'reasons', [])))
            raise unavailable() from None

    async def user_may_join_room(self, actor, room, is_invited):
        try:
            if not valid_room(room) or not valid_actor(actor):
                raise unavailable()
            # Exact-room prior participation is sufficient even when a legacy
            # series lost its older room mapping or the owner verifier is down.
            if await self.history.known_positive(room, actor):
                return NOT_SPAM
            context = await self._request({'contractVersion': 2, 'operation': 'CONTEXT',
                                           'roomId': room, 'matrixUserId': actor}, 65536)
            if (not isinstance(context, dict)
                    or set(context) != {'contractVersion', 'roomId', 'matrixUserId', 'applicable', 'roomIds', 'historyComplete'}
                    or type(context['contractVersion']) is not int or context['contractVersion'] != 2
                    or context['roomId'] != room or context['matrixUserId'] != actor
                    or type(context['applicable']) is not bool or type(context['historyComplete']) is not bool
                    or not isinstance(context['roomIds'], list) or len(context['roomIds']) > 512
                    or any(not valid_room(value) for value in context['roomIds'])
                    or len(set(context['roomIds'])) != len(context['roomIds'])):
                raise unavailable()
            if not context['applicable']:
                if context['roomIds']:
                    raise unavailable()
                return NOT_SPAM
            if room not in context['roomIds']:
                raise unavailable()
            for prior in context['roomIds']:
                if await self.history.commenced(prior, actor):
                    return NOT_SPAM
            policy = await self._request({'contractVersion': 1, 'roomId': room, 'matrixUserId': actor}, 4096)
            if policy is None:
                if not context['historyComplete']:
                    raise unavailable()
                return Codes.FORBIDDEN, {'reason': DENIED}
            if (not isinstance(policy, dict)
                    or set(policy) != {'contractVersion', 'roomId', 'matrixUserId', 'applicable'}
                    or type(policy['contractVersion']) is not int or policy['contractVersion'] != 1
                    or policy['roomId'] != room or policy['matrixUserId'] != actor
                    or policy['applicable'] is not True):
                raise unavailable()
            return NOT_SPAM
        except SynapseError:
            raise
        except Exception:
            raise unavailable() from None
