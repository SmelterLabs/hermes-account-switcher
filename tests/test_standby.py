"""The standby worker: Hermes loaded when the dialog opens, so a click switches without that wait. Hermetic."""
import json
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import switch_worker as worker
import windows_ops as ops
import plugin_api as api


class FakeWriter:
    def __init__(self):
        self.ready = None
        self.closed = False

    def start(self):
        pass

    def wait_ready(self):
        self.ready = True
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'RECEIPT', tmp_path / 'receipt.json')
    monkeypatch.setattr(ops, 'LOCK', tmp_path / 'account-switcher.lock')
    monkeypatch.setattr(ops, 'STANDBY', tmp_path / 'standby.json')
    monkeypatch.setattr(ops, 'bound_settings', nullcontext)
    monkeypatch.setattr(ops, 'require_switch_interfaces', lambda: None)
    monkeypatch.setattr(ops, 'STANDBY_POLL', 0.01)
    monkeypatch.setattr(worker.psutil, 'Process', lambda pid=None: SimpleNamespace(create_time=lambda: 1000.0))
    return tmp_path


def request_switch(operation_id, standby=True):
    ops.LOCK.write_text(json.dumps({'operation_id': operation_id, 'pid': 1}))
    ops.RECEIPT.write_text(json.dumps({'operation_id': operation_id, 'state': 'accepted', 'standby': standby}))


def test_a_standby_takes_the_waiting_switch_with_its_warm_writer(paths, monkeypatch):
    writers = []
    monkeypatch.setattr(ops, 'HermesWriter', lambda: writers.append(FakeWriter()) or writers[-1])
    ran = []
    monkeypatch.setattr(worker, '_main_bound', lambda operation_id, writer=None: ran.append((operation_id, writer)))
    request_switch('op1')
    worker.main('standby')
    assert ran == [('op1', writers[0])]
    assert ops.claim_file('op1').exists()  # The switch's own worker removes it when it finishes.
    assert not ops.STANDBY.exists()
    assert writers[0].closed is False  # The switch owns it now and closes it in unfreeze.


def test_a_standby_ignores_a_switch_meant_for_a_launched_worker_and_gives_up_in_time(paths, monkeypatch):
    writer = FakeWriter()
    monkeypatch.setattr(ops, 'HermesWriter', lambda: writer)
    monkeypatch.setattr(ops, 'STANDBY_SECONDS', 0.05)
    monkeypatch.setattr(worker, '_main_bound', lambda *a, **k: pytest.fail('not its switch'))
    request_switch('op2', standby=False)
    worker.main('standby')
    assert writer.closed and not ops.STANDBY.exists() and not ops.claim_file('op2').exists()


def test_a_standby_that_cannot_load_hermes_leaves_quietly(paths, monkeypatch):
    def broken():
        raise ops.UnsupportedRuntime('fixture')
    monkeypatch.setattr(ops, 'require_switch_interfaces', broken)
    worker.main('standby')
    assert not ops.STANDBY.exists()


def test_only_one_worker_claims_a_switch(paths):
    assert ops.claim('op3') is True
    assert ops.claim('op3') is False


def test_the_switch_route_hands_over_to_a_standby_or_claims_it_itself(paths, monkeypatch):
    monkeypatch.setattr(ops, 'STANDBY_CLAIM_WAIT', 0.05)
    ops.claim('taken')
    assert api.taken_by_standby('taken') is True
    assert api.taken_by_standby('nobody') is False
    assert ops.claim('nobody') is False  # The route holds it, so a late standby cannot also run it.


def test_standby_liveness_needs_the_same_process_and_a_recent_start(paths, monkeypatch):
    now = time.time()
    monkeypatch.setattr(ops.psutil, 'Process', lambda pid: SimpleNamespace(create_time=lambda: now))
    ops.STANDBY.write_text(json.dumps({'pid': 5, 'born': now}))
    assert ops.standby_alive()['pid'] == 5
    ops.STANDBY.write_text(json.dumps({'pid': 5, 'born': now - 30}))  # A recycled pid.
    assert ops.standby_alive() is None
    ops.STANDBY.unlink()
    assert ops.standby_alive() is None


def test_start_standby_launches_once_and_never_while_a_switch_runs(paths, monkeypatch):
    launched = []
    launch = lambda *args: launched.append(args)
    ops.start_standby('pythonw.exe', 'switch_worker.py', launch)
    assert launched == [('pythonw.exe', 'switch_worker.py', 'standby', ops.ROOT)]
    monkeypatch.setattr(ops, 'standby_alive', lambda: {'pid': 5})
    ops.start_standby('pythonw.exe', 'switch_worker.py', launch)
    monkeypatch.setattr(ops, 'standby_alive', lambda: None)
    ops.LOCK.write_text('{}')
    ops.start_standby('pythonw.exe', 'switch_worker.py', launch)
    assert len(launched) == 1


def test_a_warm_writer_is_used(monkeypatch):
    warm = FakeWriter()
    record = {}
    switch = ops.WindowsOps(record, writer=warm)
    assert switch.writer is warm
