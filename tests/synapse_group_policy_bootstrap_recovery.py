#!/usr/bin/env python3
"""Pinned Synapse/Twisted regression; only the DB interaction boundary is substituted.

This complements, and does not replace, the real SQLite/Postgres native tests.
"""
from pathlib import Path
import subprocess
import sys


def inside():
    import synapse
    from synapse.api.errors import SynapseError
    from twisted.internet.defer import Deferred, DeferredList, ensureDeferred, succeed
    from twisted.internet.task import react
    from oriso_group_policy import AppliedHistory

    assert synapse.__version__ == '1.158.0'

    class Boundary:
        def __init__(self):
            self.bootstrap = []
            self.operations = []

        def is_mine(self, actor):
            return True

        def run_db_interaction(self, name, callback, *args):
            if name == 'oriso_avv_history_schema':
                pending = Deferred()
                self.bootstrap.append(pending)
                return pending
            self.operations.append(name)
            return succeed(True)

    async def scenario(reactor):
        api = Boundary()
        first, second = AppliedHistory(api), AppliedHistory(api)
        room, actor = '!bootstrap:test.local', '@bootstrap:test.local'
        waiting = [ensureDeferred(first.commenced(room, actor)),
                   ensureDeferred(second.known_positive(room, actor)),
                   ensureDeferred(first.reconcile())]
        failed = DeferredList(waiting, consumeErrors=True)
        assert len(api.bootstrap) == 1, 'first generation must be shared'
        api.bootstrap[0].errback(RuntimeError('synthetic bootstrap DB outage'))
        failed_results = await failed
        failed_operations = list(api.operations)

        # Two instances already existed during the failure; a third is created
        # after recovery. All consumers must join exactly one fresh generation.
        third = AppliedHistory(api)
        waiting = [ensureDeferred(first.commenced(room, actor)),
                   ensureDeferred(second.known_positive(room, actor)),
                   ensureDeferred(third.snapshot(room)),
                   ensureDeferred(first.forget(room)),
                   ensureDeferred(second.record(room, actor)),
                   ensureDeferred(third.forget_actor(actor)),
                   ensureDeferred(first.reconcile())]
        recovered = DeferredList(waiting, consumeErrors=True)
        assert len(api.bootstrap) == 2, 'recovery must start one shared retry without restart'
        assert all(not ok and isinstance(value.value, SynapseError)
                   and value.value.code == 502 for ok, value in failed_results[:2])
        assert failed_results[2][0], 'reconciliation logs failure and awaits its next schedule'
        assert not failed_operations, 'failed schema must not run receipt transactions'
        assert not recovered.called, 'recovery consumers must wait for schema readiness'
        api.bootstrap[1].callback(None)
        results = await recovered
        assert all(ok for ok, value in results), 'every history/cleanup entry point must recover'
        assert results[0][1] is True and results[1][1] is True
        assert 'oriso_avv_reconcile_receipts' in api.operations
        await first.commenced(room, actor)
        await second.reconcile()
        assert len(api.bootstrap) == 2, 'successful readiness must remain cached'
        print('PASS Synapse1.158.0 real Twisted: shared bootstrap failure, bounded retry, all history/cleanup consumers recover; DB boundary substituted')

    react(lambda reactor: ensureDeferred(scenario(reactor)))


if __name__ == '__main__':
    if '--inside' in sys.argv:
        inside()
    else:
        root = Path(__file__).resolve().parents[1]
        subprocess.run(['docker', 'run', '--rm', '--network=none',
                        '-v', f'{root / "files/matrix"}:/module:ro',
                        '-v', f'{Path(__file__).resolve()}:/test.py:ro',
                        '-e', 'PYTHONPATH=/module', '--entrypoint', 'python',
                        'matrixdotorg/synapse:v1.158.0', '/test.py', '--inside'], check=True)
