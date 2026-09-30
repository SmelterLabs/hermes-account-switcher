"""Worker lifecycle, including a caller with the old API still in memory."""
import json
from types import SimpleNamespace
import pytest
import switch_worker as worker
import worker_launch
from contextlib import nullcontext

@pytest.fixture
def operation(tmp_path, monkeypatch):
    receipt = tmp_path/'receipt.json'
    lock = tmp_path/'lock.json'
    receipt.write_text(json.dumps({'operation_id':'test','target':{'codex':'work','claude':None},'state':'accepted',
                                   'settings_sha256': 'fixture-digest'}))
    lock.write_text(json.dumps({'operation_id':'test','pid':1}))
    monkeypatch.setattr(worker.ops,'RECEIPT',receipt)
    monkeypatch.setattr(worker.ops,'LOCK',lock)
    monkeypatch.setattr(worker.ops,'bound_settings',nullcontext)
    monkeypatch.setattr(worker,'settings_digest',lambda:'fixture-digest')
    monkeypatch.setattr(worker.time,'sleep',lambda _:None)
    monkeypatch.setattr(worker,'desktop',lambda:SimpleNamespace(pid=10))
    return receipt,lock

def test_old_api_hands_off_without_clearing_successor_lock(operation,monkeypatch):
    receipt,lock=operation
    monkeypatch.setattr(worker.psutil,'Process',lambda:SimpleNamespace(parents=lambda:[SimpleNamespace(pid=10)]))
    calls=[]
    monkeypatch.setattr(worker_launch,'launch',lambda *a:calls.append(a))
    monkeypatch.setattr(worker,'switch_transaction',lambda *a:pytest.fail('Coupled worker must not restart'))
    worker.main('test')
    assert len(calls)==1
    assert lock.exists()
    assert json.loads(receipt.read_text())['state']=='accepted'

def test_independent_worker_completes_and_releases(operation,monkeypatch):
    receipt,lock=operation
    monkeypatch.setattr(worker.psutil,'Process',lambda:SimpleNamespace(parents=lambda:[]))
    monkeypatch.setattr(worker,'switch_transaction',lambda *a:{'codex':'work'})
    worker.main('test')
    assert not lock.exists()
    final=json.loads(receipt.read_text())
    assert final['state']=='complete' and final['message']=='Codex Work verified after restart.'

def test_launch_failure_is_terminal_not_pending(operation,monkeypatch):
    receipt,lock=operation
    monkeypatch.setattr(worker.psutil,'Process',lambda:SimpleNamespace(parents=lambda:[SimpleNamespace(pid=10)]))
    def fail(*a):raise RuntimeError('fixture')
    monkeypatch.setattr(worker_launch,'launch',fail)
    worker.main('test')
    assert not lock.exists()
    assert json.loads(receipt.read_text())['state']=='failed'


def test_a_process_exit_inside_the_switch_is_a_failure_not_a_pending_receipt(operation, monkeypatch):
    # Seen on a real stock install: something under the switch raised SystemExit, `except Exception` let it
    # through, and the receipt stayed "accepted" forever with nothing switched.
    receipt, lock = operation
    monkeypatch.setattr(worker.psutil, 'Process', lambda: SimpleNamespace(parents=lambda: []))
    def leave(*a): raise SystemExit('hermes: fixture exit reason')
    monkeypatch.setattr(worker, 'switch_transaction', leave)
    worker.main('test')
    assert not lock.exists()
    final = json.loads(receipt.read_text())
    assert final['state'] == 'failed'
    assert final['message'] == 'The switch failed safely during accepted (SystemExit).'
    assert final['exit_reason'] == 'hermes: fixture exit reason'


def test_a_failure_names_the_stage_it_happened_in_and_its_plain_reason(operation, monkeypatch):
    # Seen on a real stock install: the settings file changed while Hermes was down. Recovery had already
    # moved the stage on to "reopening", and the receipt read "failed safely during reopening (SettingsError)".
    from settings import SettingsError
    receipt, lock = operation
    monkeypatch.setattr(worker.psutil, 'Process', lambda: SimpleNamespace(parents=lambda: []))

    def fail_then_recover(ops_object, target):
        ops_object.record['state'] = 'applying'
        ops_object.record.setdefault('failed_during', ops_object.record['state'])  # what recover() records
        ops_object.record['state'] = 'reopening'
        raise SettingsError('Account-switch settings changed during this operation; account writes refused.')
    monkeypatch.setattr(worker, 'switch_transaction', fail_then_recover)
    worker.main('test')
    final = json.loads(receipt.read_text())
    assert final['state'] == 'failed' and final['failed_during'] == 'applying'
    assert final['message'] == 'Account-switch settings changed during this operation; account writes refused.'

    receipt.write_text(json.dumps({'operation_id': 'test', 'target': {'codex': 'work', 'claude': None},
                                   'state': 'accepted', 'settings_sha256': 'fixture-digest'}))
    lock.write_text(json.dumps({'operation_id': 'test', 'pid': 1}))

    def fail_privately(ops_object, target):
        ops_object.record['failed_during'] = 'applying'
        ops_object.record['state'] = 'reopening'
        raise ValueError('fixture-private-secret')
    monkeypatch.setattr(worker, 'switch_transaction', fail_privately)
    worker.main('test')
    final = json.loads(receipt.read_text())
    assert final['message'] == 'The switch failed safely during applying (ValueError).'
    assert 'fixture-private-secret' not in receipt.read_text()


def test_a_failed_hermes_command_names_its_stage_and_a_failed_restore(operation, monkeypatch):
    # Seen on a real stock install with the login store locked: the receipt said "see the recorded stage",
    # which the dialog never shows, and did not say the old order could not be written back.
    receipt, lock = operation
    monkeypatch.setattr(worker.psutil, 'Process', lambda: SimpleNamespace(parents=lambda: []))

    def fail(ops_object, target):
        ops_object.record.update(failed_during='applying', state='reopening',
                                 recovery='restore_failed; profile stores may disagree')
        raise worker.ops.LocalOperationFailed('A required local Hermes operation failed.')
    monkeypatch.setattr(worker, 'switch_transaction', fail)
    worker.main('test')
    final = json.loads(receipt.read_text())
    assert final['state'] == 'failed'
    assert final['message'] == ('A required local Hermes operation failed during applying. The previous account '
                                'order could not be written back; check which account is selected.')


def test_recovery_keeps_the_stage_the_failure_happened_in(monkeypatch):
    import windows_ops as ops
    record = {'state': 'applying'}
    switch = ops.WindowsOps(record)
    switch.gateway = False
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'running', 'pid': 1})
    monkeypatch.setattr(ops, 'atomic', lambda path, data: None)
    switch.recover({'codex': 'personal'}, changed=False)
    assert record['failed_during'] == 'applying'
    record['state'] = 'reopening'
    switch.recover({'codex': 'personal'}, changed=False)
    assert record['failed_during'] == 'applying'


def test_nothing_the_plugin_starts_inherits_the_backends_interpreter_overrides(monkeypatch, tmp_path):
    # Seen on a real stock install: with the Desktop backend's PYTHONPATH inherited, importing a Hermes
    # module relaunched the worker under another interpreter and exited before the first check.
    import windows_ops as ops
    monkeypatch.setenv('PYTHONPATH', 'C:/backend/site-packages')
    monkeypatch.setenv('PythonHome', 'C:/backend')
    monkeypatch.setenv('HERMES_DESKTOP', '1')
    env = worker_launch.clean_env(tmp_path)
    assert not {key for key in env if key.upper() in ('PYTHONPATH', 'PYTHONHOME')}
    assert env['HERMES_HOME'] == str(tmp_path) and env['HERMES_DESKTOP'] == '1'
    seen = []
    monkeypatch.setattr(worker_launch.subprocess, 'run', lambda args, **kw: seen.append(kw['env']) or SimpleNamespace(returncode=0))
    worker_launch.launch('pythonw.exe', tmp_path / 'switch_worker.py', 'op', tmp_path)
    ops.command(['hermes.exe', 'auth', 'list'], home=tmp_path)
    assert len(seen) == 2 and all('PYTHONPATH' not in env for env in seen)


def test_changed_settings_refuse_before_worker_claim_or_switch(operation, monkeypatch):
    receipt, lock = operation
    monkeypatch.setattr(worker, 'settings_digest', lambda: 'changed-digest')
    monkeypatch.setattr(worker, 'switch_transaction', lambda *a: pytest.fail('No switch may run'))
    worker.main('test')
    assert not lock.exists()
    final = json.loads(receipt.read_text())
    assert final['state'] == 'failed'
    assert 'nothing was switched' in final['message']


def test_the_worker_gets_one_hidden_console_that_everything_it_starts_shares(monkeypatch, tmp_path):
    # Traced on the author's PC: a windowless worker made each git call Hermes's code makes open a new console,
    # which Windows 11 showed in Windows Terminal.
    import subprocess
    (tmp_path / 'pythonw.exe').write_bytes(b''); (tmp_path / 'python.exe').write_bytes(b'')
    monkeypatch.setattr(worker_launch.sys, 'executable', str(tmp_path / 'pythonw.exe'))
    python, info, flags = worker_launch.worker_start()
    assert python == str(tmp_path / 'python.exe')
    assert info.wShowWindow == subprocess.SW_HIDE and info.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert flags & subprocess.CREATE_NEW_CONSOLE and not flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_BREAKAWAY_FROM_JOB and flags & subprocess.CREATE_NEW_PROCESS_GROUP
    (tmp_path / 'python.exe').unlink()
    assert worker_launch.worker_start()[0] == str(tmp_path / 'pythonw.exe')
