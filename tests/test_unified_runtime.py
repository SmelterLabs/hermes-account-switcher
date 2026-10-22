"""Unified gateway runtime (Hermes v0.22 on): Desktop starts no backend; the gateway serves this plugin and runs
every turn. Hermetic: fake processes, recorded commands. Found on the owner's PC after the v0.22 update: every
preflight said "No live Desktop backends were found." because Desktop no longer has one."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops
from switch_core import SwitchError

OLD = {'status': 'running', 'pid': 50, 'born': None}
NEW = {'status': 'running', 'pid': 60, 'born': None}
REAL_SERVICE_OWNS_GATEWAY = ops.service_owns_gateway


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    data = {'hermes_root': str(tmp_path), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Hermes.exe'), 'gateway_service': 'FixtureGateway',
            'codex': {'only': {'label': 'Only', 'email': 'only@example.invalid'}}, 'claude': {}}
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    monkeypatch.setattr(ops, 'gateway_hosts_desktop', lambda: True)
    monkeypatch.setattr(ops, 'desktop', lambda: pytest.fail('Desktop is not an account consumer'))
    monkeypatch.setattr(ops, 'backends', lambda main: pytest.fail('there is no Desktop backend'))
    monkeypatch.setattr(ops, 'launch_desktop', lambda: pytest.fail('Desktop never closed, so it never reopens'))
    monkeypatch.setattr(ops, 'call', lambda *a, **kw: pytest.fail('the gateway answers in-process'))
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {'selected': 'only'}, 'claude': {'selected': None, 'state': 'unset'}})
    monkeypatch.setattr(ops, 'plugin_profile_blockers', lambda: [])
    monkeypatch.setattr(ops, 'claude_provider_blockers', lambda: [])
    monkeypatch.setattr(ops, 'ensure_settings_unchanged', lambda: None)
    monkeypatch.setattr(ops.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(ops, 'service_owns_gateway', lambda name: True)
    with ops.bound_settings():
        monkeypatch.setattr(ops, 'RECEIPT', tmp_path / 'receipt.json')
        yield tmp_path


def idle_ledger(hosted, monkeypatch, active=0, state='running'):
    monkeypatch.setattr(ops, 'gateway_states', lambda require_drain=False:
                        [(hosted, {'pid': 50, 'gateway_state': state, 'active_agents': active})])


def test_preflight_uses_the_gateways_own_idle_proof(hosted, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: OLD)
    idle_ledger(hosted, monkeypatch)
    result = ops.preflight(lambda: {'blockers': []})
    assert result['ok'] is True and result['blockers'] == [] and result['desktop_restarts'] is False
    # A Codex switch needs no restart here, so running work, even a prompt waiting on the person, blocks
    # nothing: the switch applies beside it. A part of the switch that restarts waits for everything.
    busy = ops.preflight(lambda: {'blockers': ['A conversation is waiting for input.']})
    assert busy['ok'] is True and busy['blockers'] == []
    assert busy['blockers_codex'] == []
    assert busy['busy'] == ['A conversation is waiting for input.']


def test_preflight_reports_gateway_work_and_blocks_a_stopped_gateway(hosted, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: OLD)
    idle_ledger(hosted, monkeypatch, active=1)
    running = ops.preflight(lambda: {'blockers': []})
    assert running['busy'] and running['blockers_codex'] == [] and running['blockers'] == []
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped', 'pid': None})
    idle_ledger(hosted, monkeypatch)
    assert 'The Hermes gateway is not running.' in ops.preflight(lambda: {'blockers': []})['blockers']


def test_the_worker_preflight_needs_no_desktop(hosted, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: OLD)
    monkeypatch.setattr(ops, 'require_switch_interfaces', lambda: None)
    monkeypatch.setattr(ops, 'runtime_python', lambda: hosted / 'python.exe')
    idle_ledger(hosted, monkeypatch)
    record = {}
    worker = ops.WindowsOps(record)
    worker.preflight({})
    assert worker.hosted and worker.gateway and worker.old_main is None
    assert record['desktop_restarts'] is False and record['old_desktop_pid'] is None and record['old_gateway_pid'] == 50


def test_stop_and_start_restart_only_the_gateway(hosted, monkeypatch):
    commands = []
    monkeypatch.setattr(ops, 'command', lambda args, **kw: commands.append(args[-1]))
    states = iter([OLD, {'status': 'stopped', 'pid': None}])
    monkeypatch.setattr(ops, 'service', lambda: next(states, {'status': 'stopped', 'pid': None}))
    monkeypatch.setattr(ops, 'process_alive', lambda process: False)
    monkeypatch.setattr(ops.psutil, 'Process', lambda pid: SimpleNamespace(pid=pid))
    idle_ledger(hosted, monkeypatch, state='draining')
    worker = ops.WindowsOps({})
    worker.hosted = True
    worker.stop()
    assert worker.stopped and worker.old_tree == []
    assert [step['step'] for step in worker.record['checkpoints']] == ['gateway_stopped']
    monkeypatch.setattr(ops, 'service', lambda: NEW)
    monkeypatch.setattr(ops, 'gateway_healthy', lambda: True)
    worker.start()
    assert 'Stop-Service' in commands[0] and 'Start-Service' in commands[1] and len(commands) == 2


def test_verify_requires_a_new_gateway_and_the_stores(hosted, monkeypatch):
    worker = ops.WindowsOps({})
    worker.hosted = True
    worker.old_gateway = ops.gateway_identity(OLD)
    monkeypatch.setattr(ops, 'service', lambda: OLD)
    with pytest.raises(SwitchError, match='did not restart'):
        worker.verify({'codex': 'only'})
    monkeypatch.setattr(ops, 'service', lambda: NEW)
    monkeypatch.setattr(ops, 'codex_stores', lambda: [])
    monkeypatch.setattr(ops, 'common_selection', lambda stores: 'other')
    with pytest.raises(SwitchError, match='does not try the selected Codex account first'):
        worker.verify({'codex': 'only'})
    monkeypatch.setattr(ops, 'common_selection', lambda stores: 'only')
    monkeypatch.setattr(ops, 'usage', lambda account: None)
    monkeypatch.setattr(ops, 'accounts', lambda: {'only': {}})
    result = worker.verify({'codex': 'only'})
    assert result['new_gateway_pid'] == 60 and result['new_desktop_pid'] is None and result['codex'] == 'only'


def test_recovery_restarts_the_gateway_without_opening_desktop(hosted, monkeypatch):
    started = []
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped', 'pid': None})
    worker = ops.WindowsOps({})
    worker.hosted = True
    monkeypatch.setattr(worker, 'start', lambda: started.append(True))
    worker.recover({'codex': 'only'}, changed=False)
    assert started == [True]


def owned_by(monkeypatch, owner, state='running'):
    """The root ledger names gateway 60; `owner` is the service whose process tree holds it (None: none does)."""
    monkeypatch.setattr(ops, 'service_owns_gateway', REAL_SERVICE_OWNS_GATEWAY)
    ledger = ops.ROOT / 'gateway_state.json'
    ledger.write_text(json.dumps({'pid': 60, 'gateway_state': state}), encoding='utf-8')
    monkeypatch.setattr(ops.psutil, 'pid_exists', lambda pid: True)
    monkeypatch.setattr(ops, 'gateway_owner', lambda pid: owner if pid == 60 else None)


def test_service_owns_gateway_only_when_its_own_process_serves(hosted, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: NEW)
    owned_by(monkeypatch, 'FixtureGateway')
    assert ops.service_owns_gateway('FixtureGateway') is True
    owned_by(monkeypatch, None)  # Hermes Desktop started a gateway of its own
    assert ops.service_owns_gateway('FixtureGateway') is False
    owned_by(monkeypatch, 'FixtureGateway', state='draining')
    assert ops.service_owns_gateway('FixtureGateway') is False
    owned_by(monkeypatch, 'FixtureGateway')
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped', 'pid': None})
    assert ops.service_owns_gateway('FixtureGateway') is False


def test_verify_fails_when_a_gateway_other_than_the_service_serves(hosted, monkeypatch):
    """Found live on 1.3.0: Desktop replaced the stopped service with its own gateway, the service could not
    start beside it, and the switch still reported complete."""
    monkeypatch.setattr(ops, 'service', lambda: NEW)
    monkeypatch.setattr(ops, 'codex_stores', lambda: [])
    monkeypatch.setattr(ops, 'common_selection', lambda stores: 'only')
    monkeypatch.setattr(ops, 'usage', lambda account: None)
    monkeypatch.setattr(ops, 'accounts', lambda: {'only': {}})
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(ops.time, 'monotonic', lambda: next(clock))
    worker = ops.WindowsOps({})
    worker.hosted = True
    worker.old_gateway = ops.gateway_identity(OLD)
    owned_by(monkeypatch, None)
    with pytest.raises(SwitchError, match='not the Windows service FixtureGateway'):
        worker.verify({'codex': 'only'})
    owned_by(monkeypatch, 'FixtureGateway')
    assert worker.verify({'codex': 'only'})['codex'] == 'only'


def test_preflight_refuses_while_a_gateway_other_than_the_service_serves(hosted, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: NEW)
    idle_ledger(hosted, monkeypatch)
    owned_by(monkeypatch, None)
    result = ops.preflight(lambda: {'blockers': []})
    assert result['ok'] is False and any('not the Windows service' in b for b in result['blockers'])
    owned_by(monkeypatch, 'FixtureGateway')
    assert ops.preflight(lambda: {'blockers': []})['ok'] is True
