#!/usr/bin/env python3
"""Disposable real Synapse1.158 native REST/history test; owner API is the external stub.

Run --without-module first for the behavioral RED: native JOIN wrongly succeeds.
Both database modes retain real pre-module JOIN history across the restart.
"""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'matrixdotorg/synapse:v1.158.0'
STUB = '''import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
state=json.loads(Path('/fixture/policy.json').read_text())
leaks=0
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  body=json.dumps({'redirectRequests':leaks}).encode(); self.send_response(200)
  self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body)))
  self.end_headers(); self.wfile.write(body)
 def do_POST(self):
  global state,leaks
  data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.headers.get('X-Oriso-Group-Policy-Token') != Path('/fixture/token').read_text():
   self.send_response(401); self.end_headers(); return
  if self.path=='/fixture-state':
   state=data; self.send_response(200); self.end_headers(); return
  if self.path=='/leak':
   leaks+=1; self.send_response(200); self.end_headers(); return
  if state.get('outage'):
   self.send_response(503); self.end_headers(); return
  if state.get('mode')=='owner-unauthorized':
   self.send_response(401); self.end_headers(); return
  room=data['roomId']; actor=data['matrixUserId']; group=state.get('rooms',{}).get(room)
  if data.get('contractVersion')==2:
   result={'contractVersion':2,'roomId':room,'matrixUserId':actor,'applicable':group is not None,
    'roomIds':group or [],'historyComplete':state.get('complete',True)}
   mode=state.get('mode')
   if mode=='redirect':
    self.send_response(307); self.send_header('Location','http://leak-receiver:8080/leak'); self.end_headers(); return
   if mode=='context-binding':result['matrixUserId']='@other:test.local'
   if mode=='context-oversize':result['extra']='x'*70000
   if mode=='context-duplicate':result['roomIds']=[room,room]
  elif state.get('mode')=='generic-forbidden':
   self.send_response(403); self.end_headers(); return
  elif state.get('mode')=='typed-html-forbidden':
   self.send_response(403); self.send_header('X-Reason','DPA_NEW_COUNSELLING_NOT_ALLOWED'); self.send_header('Content-Type','text/html'); self.end_headers(); self.wfile.write(b'<html>not a canonical refusal</html>'); return
  elif group is not None and not state.get('allowed',False):
   body=json.dumps({'reason':'DPA_NEW_COUNSELLING_NOT_ALLOWED'}).encode()
   self.send_response(403); self.send_header('X-Reason','DPA_NEW_COUNSELLING_NOT_ALLOWED')
   self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
  else: result={'contractVersion':1,'roomId':room,'matrixUserId':actor,'applicable':group is not None}
  body=json.dumps(result).encode(); self.send_response(200); self.send_header('Content-Type','application/json')
  self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''
STORAGE_PROBE = '''import json,sys,yaml
from pathlib import Path
from oriso_group_policy import AppliedHistory
from synapse.storage.engines import create_engine
config=yaml.safe_load(Path('/fixture/homeserver.yaml').read_text())['database']
engine=create_engine(config)
if config['name']=='sqlite3':
 import sqlite3
 db=sqlite3.connect(config['args']['database'],timeout=10)
else:
 import psycopg2
 db=psycopg2.connect(**{k:v for k,v in config['args'].items() if not k.startswith('cp_')})
class Txn:
 database_engine=engine
 def __init__(self):self.cursor=db.cursor()
 def execute(self,sql,args=()):self.cursor.execute(engine.convert_param_style(sql),args)
 def fetchone(self):return self.cursor.fetchone()
body=json.loads(sys.argv[1]);txn=Txn()
AppliedHistory._save(txn,body['roomId'],[(body['actor'],body['eventId'])])
print('READY',flush=True)
sys.stdin.readline()
db.commit()
'''


def docker(*args, check=True):
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=check).stdout.strip()


def request(origin, path, data=None, token=None, headers=None, method=None):
    hdr = dict(headers or {})
    if token:
        hdr['Authorization'] = 'Bearer ' + token
    if data is not None:
        hdr['Content-Type'] = 'application/json'
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
    req = urllib.request.Request(origin + path, data, hdr, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=8)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    return response.status, body


def require(status, body, expected, label):
    assert status == expected, f'{label}: expected HTTP{expected}, got HTTP{status}; code={body.get("errcode")}; reason={body.get("reason")}'
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', choices=['sqlite', 'postgres'], default='sqlite')
    parser.add_argument('--without-module', action='store_true')
    args = parser.parse_args()
    run = 'avv294-' + uuid.uuid4().hex[:10]
    network, synapse, owner, pg = [run + suffix for suffix in ['-net', '-synapse', '-owner', '-pg']]
    containers = []
    with tempfile.TemporaryDirectory(prefix=run) as tmp:
        fixture = Path(tmp)
        fixture.chmod(0o755)
        token = secrets.token_urlsafe(48)
        (fixture / 'token').write_text(token)
        (fixture / 'token').chmod(0o644)
        (fixture / 'owner.py').write_text(STUB)
        (fixture / 'storage_probe.py').write_text(STORAGE_PROBE)
        state = {'rooms': {}, 'allowed': False, 'complete': True}
        owner_origin = None

        def save_state():
            if owner_origin:
                for attempt in range(50):
                    try:
                        status, body = request(owner_origin, '/fixture-state', state,
                            headers={'X-Oriso-Group-Policy-Token': token})
                        require(status, body, 200, 'fixture owner control')
                        return
                    except OSError:
                        time.sleep(.1)
                raise AssertionError('Owner fixture control startup failed')
            target = fixture / 'policy.next'
            target.write_text(json.dumps(state))
            target.replace(fixture / 'policy.json')

        def run_synapse(enabled):
            modules = '''modules:
 - module: oriso_group_policy.GroupPolicy
   config:
     token_file: /fixture/token
     policy_url: http://userservice:8080/internal/matrix/group-join-policy
''' if enabled else ''
            history = ''' - port: 8801
   type: http
   tls: false
   bind_addresses: ['0.0.0.0']
   resources: []
   additional_resources:
     /oriso-internal/group-participation-history:
       module: oriso_group_policy.HistoryResource
       config:
         token_file: /fixture/token
''' if enabled else ''
            database = '''database:
 name: sqlite3
 args:
   database: /data/homeserver.db
'''
            if args.database == 'postgres':
                database = '''database:
 name: psycopg2
 args:
   user: synapse
   password: fixture-database-only
   database: synapse
   host: postgres
   cp_min: 1
   cp_max: 5
'''
            config = f'''server_name: test.local
pid_file: /data/homeserver.pid
public_baseurl: http://test.local/
report_stats: false
enable_registration: true
enable_registration_without_verification: true
macaroon_secret_key: fixture-macaroon-only
form_secret: fixture-form-only
signing_key_path: /data/test.signing.key
media_store_path: /data/media_store
federation_domain_whitelist: []
listeners:
 - port: 8008
   type: http
   tls: false
   bind_addresses: ['0.0.0.0']
   resources:
    - names: [client]
{history}{database}{modules}
rc_registration: {{per_second: 100, burst_count: 1000}}
rc_login:
 address: {{per_second: 100, burst_count: 1000}}
 account: {{per_second: 100, burst_count: 1000}}
 failed_attempts: {{per_second: 100, burst_count: 1000}}
rc_joins:
 local: {{per_second: 100, burst_count: 1000}}
 remote: {{per_second: 100, burst_count: 1000}}
rc_invites:
 per_room: {{per_second: 100, burst_count: 1000}}
 per_user: {{per_second: 100, burst_count: 1000}}
 per_issuer: {{per_second: 100, burst_count: 1000}}
'''
            (fixture / 'homeserver.yaml').write_text(config)
            docker('run', '-d', '--name', synapse, '--network', network,
                   '-p', '127.0.0.1::8008', '-p', '127.0.0.1::8801',
                   '-e', 'SYNAPSE_CONFIG_PATH=/fixture/homeserver.yaml', '-e', 'PYTHONPATH=/modules',
                   '--mount', f'type=bind,source={fixture},target=/fixture',
                   '--mount', f'type=bind,source={ROOT / "files/matrix"},target=/modules,readonly',
                   '--mount', f'type=volume,source={run}-data,target=/data', IMAGE)
            containers.append(synapse)
            origin = 'http://' + docker('port', synapse, '8008/tcp')
            for _ in range(120):
                try:
                    status, _ = request(origin, '/_matrix/client/versions')
                    if status == 200:
                        return origin, 'http://' + docker('port', synapse, '8801/tcp')
                except (OSError, urllib.error.URLError):
                    pass
                if docker('inspect', synapse, '--format', '{{.State.Running}}') != 'true':
                    logs = subprocess.run(['docker', 'logs', synapse], capture_output=True, text=True)
                    log = Path(tempfile.gettempdir()) / 'avv294-synapse-startup-private.log'
                    log.touch(mode=0o600, exist_ok=True)
                    log.write_text(logs.stdout + logs.stderr)
                    raise AssertionError('Synapse startup failed; private log retained in OS temporary directory')
                time.sleep(.5)
            raise AssertionError('Synapse startup timeout')

        try:
            save_state()
            docker('network', 'create', network)
            docker('run', '--rm', '--user', '0:0', '--entrypoint', 'chown',
                   '--mount', f'type=volume,source={run}-data,target=/data',
                   IMAGE, '991:991', '/data')
            docker('run', '-d', '--name', owner, '--network', network, '--network-alias', 'userservice',
                   '--network-alias', 'leak-receiver',
                   '-p', '127.0.0.1::8080',
                   '--mount', f'type=bind,source={fixture},target=/fixture,readonly',
                   'python:3.13-slim', 'python', '/fixture/owner.py')
            containers.append(owner)
            owner_origin = 'http://' + docker('port', owner, '8080/tcp')
            save_state()
            if args.database == 'postgres':
                docker('run', '-d', '--name', pg, '--network', network, '--network-alias', 'postgres',
                       '-e', 'POSTGRES_USER=synapse', '-e', 'POSTGRES_DB=synapse',
                       '-e', 'POSTGRES_PASSWORD=fixture-database-only', '-e', 'POSTGRES_INITDB_ARGS=--locale=C --encoding=UTF8',
                       'postgres:16-alpine')
                containers.append(pg)
                for _ in range(60):
                    result = subprocess.run(['docker', 'exec', pg, 'pg_isready', '-U', 'synapse'], capture_output=True)
                    if result.returncode == 0:
                        break
                    time.sleep(.3)
            origin, _ = run_synapse(False)

            def database(sql, parameters=()):
                # Fixture setup / corruption probes only, in this disposable DB.
                connect = "import sqlite3; db=sqlite3.connect('/data/homeserver.db')"
                if args.database == 'postgres':
                    connect = "import psycopg2; db=psycopg2.connect(host='postgres',user='synapse',password='fixture-database-only',dbname='synapse')"
                    sql = sql.replace('?', '%s')
                script = connect + '\nimport json\nc=db.cursor()\nc.execute(' + repr(sql) + ',' + repr(tuple(parameters)) + ')\nr=c.fetchall() if c.description else []\ndb.commit()\nprint(json.dumps(r))'
                result = subprocess.run(['docker', 'exec', '-i', synapse, 'python', '-'],
                                        input=script, capture_output=True, text=True, check=True)
                return json.loads(result.stdout)

            def register(name):
                status, body = request(origin, '/_matrix/client/v3/register', {
                    'username': name, 'password': secrets.token_urlsafe(24), 'auth': {'type': 'm.login.dummy'}})
                return require(status, body, 200, 'register')['access_token']

            creator, former, newcomer, shadow = [register(x) for x in ['creator', 'former', 'newcomer', 'shadow']]
            database('UPDATE users SET admin = ? WHERE name = ?', (1, '@creator:test.local'))

            def room():
                status, body = request(origin, '/_matrix/client/v3/createRoom', {'preset': 'private_chat'}, creator)
                return require(status, body, 200, 'create room')['room_id']

            def invite(room_id, actor):
                status, body = request(origin, f'/_matrix/client/v3/rooms/{room_id}/invite', {'user_id': actor}, creator)
                require(status, body, 200, 'invite')

            def join(room_id, actor_token):
                return request(origin, f'/_matrix/client/v3/join/{room_id}', {}, actor_token)

            first = room()
            invite(first, '@former:test.local')
            require(*join(first, former), 200, 'pre-module actual JOIN')
            require(*request(origin, f'/_matrix/client/v3/rooms/{first}/leave', {}, former), 200, 'pre-module leave')
            invite(first, '@newcomer:test.local')
            state['rooms'][first] = [first]
            save_state()
            docker('rm', '-f', synapse)
            containers.remove(synapse)
            origin, history_origin = run_synapse(not args.without_module)
            status, body = join(first, newcomer)
            require(status, body, 403, 'expired native first JOIN')
            assert body.get('reason') == 'DPA_NEW_COUNSELLING_NOT_ALLOWED', 'legal refusal reason must remain typed'
            if args.without_module:
                raise AssertionError('Unreachable: baseline lacks native AVV hook')
            invite(first, '@former:test.local')
            require(*join(first, former), 200, 'return with pre-module actual history')
            second = room()
            state['rooms'][second] = [second, first]
            save_state()
            invite(second, '@former:test.local')
            require(*join(second, former), 200, 'existing participant later occurrence')
            invite(second, '@newcomer:test.local')
            require(*join(second, newcomer), 403, 'newcomer later occurrence')
            payload = {'contractVersion': 1, 'schemaVersion': 'synapse-1.158.0-applied-membership-v1',
                       'roomId': first, 'matrixUserId': '@former:test.local'}
            path = '/oriso-internal/group-participation-history'
            header = {'X-Oriso-Group-Policy-Token': token}
            status, body = request(history_origin, path, payload, headers=header)
            require(status, body, 200, 'private history')
            assert body == {**payload, 'participation': 'COMMENCED'}
            require(*request(origin, path, payload, headers=header), 404, 'public listener must not mount history')
            require(*request(history_origin, path, payload), 401, 'missing history credential')
            require(*request(history_origin, path, {**payload, 'extra': True}, headers=header), 400, 'malformed history')
            require(*request(history_origin, path, b'{"contractVersion":1,"contractVersion":2}', headers=header), 400, 'duplicate JSON history')
            require(*request(history_origin, path, b' ' * 4097, headers=header), 400, 'oversized history')
            payload['matrixUserId'] = '@newcomer:test.local'
            status, body = request(history_origin, path, payload, headers=header)
            require(status, body, 200, 'invitation is not JOIN')
            assert body['participation'] == 'NOT_COMMENCED'
            state['complete'] = False
            save_state()
            require(*join(first, newcomer), 502, 'unknown legacy series history')
            state['complete'] = True
            state['outage'] = True
            save_state()
            require(*join(first, newcomer), 502, 'owner outage')
            state['outage'] = False
            for mode in ['redirect', 'context-binding', 'context-oversize', 'context-duplicate', 'owner-unauthorized', 'generic-forbidden', 'typed-html-forbidden']:
                state['mode'] = mode
                save_state()
                require(*join(first, newcomer), 502, 'unusable owner ' + mode)
            _, result = request(owner_origin, '/fixture-result')
            assert result['redirectRequests'] == 0, 'dedicated credential must never follow redirect'
            state.pop('mode')
            state['allowed'] = True
            save_state()
            require(*join(first, newcomer), 200, 'same invitation retry after current confirmation')
            # All earlier participants, including people who have LEFT, must be
            # captured before the existing destructive recurrence retirement.
            require(*request(origin, f'/_matrix/client/v3/rooms/{first}/leave', {}, former), 200, 'former leaves before purge')
            require(*request(history_origin, path, {'contractVersion': 2, 'operation': 'SNAPSHOT_ROOM', 'roomId': first}, headers=header), 200, 'snapshot all prior participation')
            require(*request(origin, f'/_synapse/admin/v2/rooms/{first}', {'purge': True}, creator, method='DELETE'), 200, 'normal native v2 room purge accepted')
            for _ in range(100):
                if database('SELECT COUNT(*) FROM events WHERE room_id = ?', (first,))[0][0] == 0:
                    break
                time.sleep(.1)
            assert database('SELECT COUNT(*) FROM events WHERE room_id = ?', (first,))[0][0] == 0
            payload['matrixUserId'] = '@former:test.local'
            status, body = request(history_origin, path, payload, headers=header)
            require(status, body, 200, 'history receipt survives real purge')
            assert body['participation'] == 'COMMENCED'
            payload['matrixUserId'] = '@shadow:test.local'
            status, body = request(history_origin, path, payload, headers=header)
            require(status, body, 200, 'complete snapshot can disprove a newcomer')
            assert body['participation'] == 'NOT_COMMENCED'
            third = room()
            state['rooms'][third] = [third, first]
            state['allowed'] = False
            save_state()
            invite(third, '@former:test.local')
            require(*join(third, former), 200, 'later occurrence with purged prior room')
            invite(third, '@shadow:test.local')
            require(*join(third, shadow), 403, 'newcomer cannot borrow another actor receipt')
            require(*request(history_origin, path, {'contractVersion': 2, 'operation': 'SNAPSHOT_ROOM', 'roomId': '!missing:test.local'}, headers=header), 502, 'unknown room coverage')
            require(*request(origin, f'/_matrix/client/v3/rooms/{third}/ban', {'user_id': '@former:test.local'}, creator), 200, 'ban')
            require(*join(third, former), 403, 'history must not bypass ordinary ban')
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@former:test.local',))[0][0] > 0
            proof = database('SELECT room_id,event_id FROM oriso_avv_join_receipts WHERE user_id = ? LIMIT 1', ('@former:test.local',))[0]
            database('DELETE FROM oriso_avv_join_receipts WHERE room_id = ? AND user_id = ?', (proof[0], '@former:test.local'))
            # Execute the actual receipt writer against the real DB, hold its
            # transaction, then invoke real native deactivation concurrently.
            payload_probe = json.dumps({'roomId': proof[0], 'actor': '@former:test.local', 'eventId': proof[1]})
            with subprocess.Popen(['docker', 'exec', '-i', synapse, 'python', '/fixture/storage_probe.py', payload_probe],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as writer:
                assert writer.stdout.readline().strip() == 'READY', 'receipt writer fixture failed'
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    deactivation = executor.submit(request, origin, '/_synapse/admin/v1/deactivate/@former:test.local', {'erase': True}, creator)
                    time.sleep(.15)
                    serialized = not deactivation.done()
                    writer.stdin.write('COMMIT\n')
                    writer.stdin.flush()
                    writer.wait(timeout=10)
                    require(*deactivation.result(timeout=10), 200, 'serialized native actor deactivation')
                    assert serialized, 'native deactivation must serialize after in-flight receipt write'
                assert writer.returncode == 0
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@former:test.local',))[0][0] == 0
            require(*request(history_origin, path, {'contractVersion': 2, 'operation': 'SNAPSHOT_ROOM', 'roomId': third}, headers=header), 200, 'post-deactivation snapshot')
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@former:test.local',))[0][0] == 0
            # A late callback carrying a genuine old accepted JOIN must not
            # recreate actor data after the native cleanup has acknowledged it.
            late = subprocess.run(['docker', 'exec', '-i', synapse, 'python', '/fixture/storage_probe.py', payload_probe],
                                  input='COMMIT\n', capture_output=True, text=True, check=True)
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@former:test.local',))[0][0] == 0
            cleanup_actor = register('cleanup')
            cleanup_room = room()
            state['rooms'][cleanup_room] = [cleanup_room]
            state['allowed'] = True
            save_state()
            invite(cleanup_room, '@cleanup:test.local')
            require(*join(cleanup_room, cleanup_actor), 200, 'cleanup-retry fixture actual JOIN')
            if args.database == 'sqlite':
                database("CREATE TRIGGER avv_fixture_delete_failure BEFORE DELETE ON oriso_avv_join_receipts WHEN OLD.user_id = '@cleanup:test.local' BEGIN SELECT RAISE(ABORT, 'fixture-delete-unavailable'); END")
            else:
                database("CREATE FUNCTION avv_fixture_fail_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.user_id = '@cleanup:test.local' THEN RAISE EXCEPTION 'fixture-delete-unavailable'; END IF; RETURN OLD; END $$")
                database('CREATE TRIGGER avv_fixture_delete_failure BEFORE DELETE ON oriso_avv_join_receipts FOR EACH ROW EXECUTE FUNCTION avv_fixture_fail_delete()')
            require(*request(origin, '/_synapse/admin/v1/deactivate/@cleanup:test.local', {'erase': True}, creator), 200, 'native deactivation completes despite failing module callback')
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@cleanup:test.local',))[0][0] > 0
            status, body = request(history_origin, path, {**payload, 'roomId': cleanup_room, 'matrixUserId': '@cleanup:test.local'}, headers=header)
            require(status, body, 200, 'deactivated actor history during cleanup outage')
            assert body['participation'] == 'NOT_COMMENCED', 'stale inactive receipt must not be usable while cleanup retries'
            database('DROP TRIGGER avv_fixture_delete_failure' + (' ON oriso_avv_join_receipts' if args.database == 'postgres' else ''))
            if args.database == 'postgres':
                database('DROP FUNCTION avv_fixture_fail_delete()')
            until = time.monotonic() + 15
            while time.monotonic() < until:
                if database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@cleanup:test.local',))[0][0] == 0:
                    break
                time.sleep(.25)
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@cleanup:test.local',))[0][0] == 0, 'receipt cleanup must recover after DB failure without another user request'
            state['allowed'] = False
            save_state()
            creator_proof = database('SELECT event_id FROM oriso_avv_join_receipts WHERE room_id = ? AND user_id = ?', (third, '@creator:test.local'))[0][0]
            require(*request(history_origin, path, {'contractVersion': 2, 'operation': 'FORGET_ROOM', 'roomId': third}, headers=header), 200, 'final group erasure')
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE room_id = ?', (third,))[0][0] == 0
            retired_probe = json.dumps({'roomId': third, 'actor': '@creator:test.local', 'eventId': creator_proof})
            subprocess.run(['docker', 'exec', '-i', synapse, 'python', '/fixture/storage_probe.py', retired_probe],
                           input='COMMIT\n', capture_output=True, text=True, check=True)
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE room_id = ?', (third,))[0][0] == 0
            require(*request(history_origin, path, {'contractVersion': 2, 'operation': 'SNAPSHOT_ROOM', 'roomId': third}, headers=header), 502, 'retired room cannot resurrect receipt')
            state['rooms'].pop(third)
            save_state()
            require(*join(third, shadow), 502, 'final deletion tombstone covers native join after owner mapping removal')
            # Simulate retention damage in this isolated DB: a real formerly
            # applied JOIN remains but its application-stream row was removed.
            # An old positive cache/receipt is explicitly removed for this probe.
            uncertain_token = register('uncertain')
            uncertain_room = room()
            invite(uncertain_room, '@uncertain:test.local')
            require(*join(uncertain_room, uncertain_token), 200, 'ordinary unmapped room')
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE room_id = ?', (uncertain_room,))[0][0] == 0, 'unrelated rooms must not acquire AVV receipts'
            assert database('SELECT COUNT(*) FROM oriso_avv_history_rooms WHERE room_id = ?', (uncertain_room,))[0][0] == 0, 'unrelated rooms must not acquire AVV coverage metadata'
            require(*request(origin, f'/_matrix/client/v3/rooms/{uncertain_room}/leave', {}, uncertain_token), 200, 'uncertain actor leave')
            database('DELETE FROM oriso_avv_join_receipts WHERE room_id = ? AND user_id = ?', (uncertain_room, '@uncertain:test.local'))
            database("DELETE FROM current_state_delta_stream WHERE room_id = ? AND state_key = ? AND event_id IN (SELECT event_id FROM room_memberships WHERE membership = 'join')", (uncertain_room, '@uncertain:test.local'))
            uncertain_payload = {**payload, 'roomId': uncertain_room, 'matrixUserId': '@uncertain:test.local'}
            require(*request(history_origin, path, uncertain_payload, headers=header), 502, 'pruned application evidence cannot become a negative')
            event_id = database("SELECT event_id FROM room_memberships WHERE room_id = ? AND user_id = ? AND membership = 'join'", (uncertain_room, '@uncertain:test.local'))[0][0]
            original_event = json.loads(database('SELECT json FROM event_json WHERE event_id = ?', (event_id,))[0][0])
            database('UPDATE event_json SET json = ? WHERE event_id = ?', (json.dumps({**original_event, 'state_key': '@other:test.local'}), event_id))
            require(*request(history_origin, path, uncertain_payload, headers=header), 502, 'contradictory stored JOIN cannot become a negative')
            database('UPDATE event_json SET json = ? WHERE event_id = ?', (json.dumps(original_event), event_id))
            database('UPDATE events SET outlier = ? WHERE event_id = ?', (True, event_id))
            status, body = request(history_origin, path, uncertain_payload, headers=header)
            require(status, body, 200, 'persisted outlier is not participation')
            assert body['participation'] == 'NOT_COMMENCED'
            database('UPDATE events SET outlier = ? WHERE event_id = ?', (False, event_id))
            metadata = json.loads(database('SELECT internal_metadata FROM event_json WHERE event_id = ?', (event_id,))[0][0])
            database('UPDATE event_json SET internal_metadata = ? WHERE event_id = ?', (json.dumps({**metadata, 'soft_failed': True}), event_id))
            status, body = request(history_origin, path, uncertain_payload, headers=header)
            require(status, body, 200, 'persisted soft-failed JOIN is not participation')
            assert body['participation'] == 'NOT_COMMENCED'
            database('UPDATE event_json SET internal_metadata = ? WHERE event_id = ?', (json.dumps(metadata), event_id))
            database('INSERT INTO rejections (event_id,reason,last_check) VALUES (?,?,?)', (event_id, 'fixture-rejected-candidate', '2026-10-28'))
            status, body = request(history_origin, path, uncertain_payload, headers=header)
            require(status, body, 200, 'persisted rejected JOIN is not participation')
            assert body['participation'] == 'NOT_COMMENCED'
            # A stale receipt from a previous interrupted cleanup must be
            # reconciled on module startup, without another deactivation call.
            database('INSERT INTO oriso_avv_join_receipts (room_id,user_id,event_id) VALUES (?,?,?)', (proof[0], '@former:test.local', proof[1]))
            docker('rm', '-f', synapse)
            containers.remove(synapse)
            origin, history_origin = run_synapse(True)
            until = time.monotonic() + 3
            while time.monotonic() < until:
                if database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@former:test.local',))[0][0] == 0:
                    break
                time.sleep(.1)
            assert database('SELECT COUNT(*) FROM oriso_avv_join_receipts WHERE user_id = ?', ('@former:test.local',))[0][0] == 0, 'startup reconciliation must clear stale deactivated receipts'
            print(f'PASS real Synapse1.158.0/{args.database}: native refusal, pre-module return, recurrence after actual purge, private auth/schema, legacy uncertainty, outage/retry, ordinary ban, native deactivation and final erasure')
        finally:
            if sys.exc_info()[0] is not None and synapse in containers:
                logs = subprocess.run(['docker', 'logs', synapse], capture_output=True, text=True)
                log = Path(tempfile.gettempdir()) / 'avv294-synapse-failure-private.log'
                log.touch(mode=0o600, exist_ok=True)
                log.write_text(logs.stdout + logs.stderr)
                logs = subprocess.run(['docker', 'logs', owner], capture_output=True, text=True)
                log = Path(tempfile.gettempdir()) / 'avv294-owner-failure-private.log'
                log.touch(mode=0o600, exist_ok=True)
                log.write_text(logs.stdout + logs.stderr)
            for name in reversed(containers):
                docker('rm', '-f', name, check=False)
            docker('volume', 'rm', run + '-data', check=False)
            docker('network', 'rm', network, check=False)


if __name__ == '__main__':
    main()
