"""Conservative recovery must preserve every unproven or unsafe operation."""
import json
import os
import time
from types import SimpleNamespace
import pytest
from test_api import api, client


@pytest.mark.parametrize('reason', ['live_owner', 'young', 'worker', 'uncertain', 'applying_live_worker', 'mismatch',
                                    'claimed', 'unknown_state'])
def test_recovery_preserves_unproven_or_unsafe_lock(client, monkeypatch, reason):
    api.ops.LOCK.write_text(json.dumps({'operation_id': 'held', 'pid': 99999999}))
    age = 0 if reason == 'young' else 60
    os.utime(api.ops.LOCK, (time.time() - age, time.time() - age))
    state = {'applying_live_worker': 'applying', 'unknown_state': 'something-new'}.get(reason, 'checking')
    record = {'operation_id': 'different' if reason == 'mismatch' else 'held', 'state': state}
    api.ops.RECEIPT.write_text(json.dumps(record))
    monkeypatch.setattr(api.ops.psutil, 'pid_exists', lambda pid: reason == 'live_owner')
    processes = []
    if reason in ('worker', 'uncertain', 'applying_live_worker'):
        processes = [SimpleNamespace(info={'name': 'pythonw.exe',
                     'cmdline': None if reason == 'uncertain' else ['pythonw.exe', 'switch_worker.py']})]
    monkeypatch.setattr(api.ops.psutil, 'process_iter', lambda attrs: processes)
    monkeypatch.setattr(api.ops, 'clear_own_drains', lambda: pytest.fail('No marker may be touched'))
    if reason == 'claimed':
        api.ops.LOCK.with_name(api.ops.LOCK.name + '.held.abandoned').write_text('{}')
    monkeypatch.setattr(api.ops, 'preflight', lambda own_state=None: pytest.fail('Unsafe to preflight'))
    data = client.get('/status').json()
    assert 'already in progress' in data['blockers'][0]
    assert api.ops.LOCK.exists()
    assert json.loads(api.ops.RECEIPT.read_text()) == record


def interrupted(monkeypatch, state, gateway='stopped', old_gateway_pid=4321, markers=True, operation='cut'):
    """A switch whose worker is provably gone: old lock, dead owner, no worker process."""
    api.ops.LOCK.write_text(json.dumps({'operation_id': operation, 'pid': 99999999}))
    os.utime(api.ops.LOCK, (time.time() - 60, time.time() - 60))
    api.ops.RECEIPT.write_text(json.dumps({'operation_id': operation, 'state': state,
                                           'old_gateway_pid': old_gateway_pid, 'previous': {'codex': 'personal'}}))
    monkeypatch.setattr(api.ops.psutil, 'pid_exists', lambda pid: False)
    monkeypatch.setattr(api.ops.psutil, 'process_iter', lambda attrs: [])
    monkeypatch.setattr(api.ops, 'service', lambda: {'status': gateway, 'pid': None})
    cleared = []
    monkeypatch.setattr(api.ops, 'clear_own_drains', lambda: cleared.append(True) or markers)
    return cleared


@pytest.mark.parametrize('state', ['restarting', 'applying', 'reopening', 'verifying'])
def test_a_switch_cut_off_after_hermes_closed_is_cleared_and_says_where_it_stopped(client, monkeypatch, state):
    # Seen on a real stock install: the worker was ended while Hermes was down. The lock stayed for good and
    # every later status said "already in progress", with no way out from the interface.
    cleared = interrupted(monkeypatch, state)
    data = client.get('/status').json()
    assert data['blockers'] == [] and data['in_progress'] is False
    last = data['last_operation']
    assert last['state'] == 'failed' and last['interrupted_during'] == state
    assert f'interrupted during {state}' in last['message']
    assert 'The Hermes gateway is not running; start it again.' in last['message']
    assert cleared == [True]
    assert not api.ops.LOCK.exists()
    # The stores are reported as they are; nothing is reordered by the recovery.
    assert data['codex']['selected'] == 'personal'


def test_a_cut_off_switch_does_not_ask_for_a_gateway_that_is_running_or_never_existed(client, monkeypatch):
    interrupted(monkeypatch, 'applying', gateway='running')
    assert 'gateway' not in client.get('/status').json()['last_operation']['message']
    interrupted(monkeypatch, 'applying', gateway='stopped', old_gateway_pid=None, operation='cut-again')
    last = client.get('/status').json()['last_operation']
    assert last['operation_id'] == 'cut-again' and 'gateway' not in last['message']


def test_a_cut_off_switch_admits_when_the_markers_could_not_be_checked(client, monkeypatch):
    interrupted(monkeypatch, 'restarting', markers=False)
    message = client.get('/status').json()['last_operation']['message']
    assert 'The gateway restart markers could not be checked.' in message
    assert not api.ops.LOCK.exists()


def test_a_cut_off_check_also_clears_its_own_markers(client, monkeypatch):
    # The freeze writes drain markers while the receipt still says "checking".
    cleared = interrupted(monkeypatch, 'checking')
    last = client.get('/status').json()['last_operation']
    assert last['state'] == 'failed' and 'interrupted_during' not in last
    assert 'No switch was completed' in last['message']
    assert cleared == [True]


def test_only_this_plugins_drain_markers_are_removed(monkeypatch, tmp_path):
    homes = [tmp_path, tmp_path / 'profiles' / 'beta', tmp_path / 'profiles' / 'gamma']
    owners = {homes[0]: 'codex-account-switch', homes[1]: 'hermes-update', homes[2]: None}
    removed = []
    drain = SimpleNamespace(
        read_drain_request=lambda home: {'principal': owners[home]} if owners[home] else None,
        clear_drain_request=lambda home: removed.append(home))
    monkeypatch.setattr(api.ops, 'drain_control', lambda: drain)
    monkeypatch.setattr(api.ops, 'profile_homes', lambda: homes)
    assert api.ops.clear_own_drains() is True
    assert removed == [homes[0]]

    def broken():
        raise ImportError('fixture')
    monkeypatch.setattr(api.ops, 'drain_control', broken)
    assert api.ops.clear_own_drains() is False
