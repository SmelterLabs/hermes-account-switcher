"""Safety integrations fail before receipts, drains, or account writes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import compat
import windows_ops as ops
from switch_core import SwitchError


def test_missing_private_interface_fails_closed(monkeypatch):
    monkeypatch.setattr(compat, 'import_module', lambda name: (_ for _ in ()).throw(ImportError(name)))
    with pytest.raises(compat.UnsupportedRuntime, match='safety interfaces'):
        compat.require_switch_interfaces()


def _dispatcher(source):
    """A stand-in tui_gateway.server whose functions reference the same names the real ones do."""
    from types import SimpleNamespace
    scope = {}
    exec(source, scope)
    return SimpleNamespace(**{name: value for name, value in scope.items() if callable(value)})


CURRENT = '''
def _handle_admitted_request(req): return req
def handle_request(req): return _handle_admitted_request(req)
def dispatch(req):
    if req.get('long'):
        def run(): return _handle_admitted_request(req)
        return run()
    return handle_request(req)
'''
OLDER = '''
def handle_request(req): return req
def dispatch(req): return handle_request(req)
'''
NEW_BYPASS = CURRENT + '''
def _handle_streamed_request(req): return req
def dispatch(req):
    if req.get('stream'): return _handle_streamed_request(req)
    return handle_request(req)
'''


@pytest.mark.parametrize('source,target', [(CURRENT, '_handle_admitted_request'), (OLDER, 'handle_request')])
def test_known_dispatchers_are_fully_gated(source, target):
    server = _dispatcher(source)
    assert compat.gate_target(server) == target
    compat.require_gated_admission(server)


def test_a_dispatcher_with_an_ungated_route_is_refused():
    with pytest.raises(ValueError):
        compat.require_gated_admission(_dispatcher(NEW_BYPASS))


def test_unsupported_runtime_never_stages_or_freezes(monkeypatch, tmp_path):
    receipt = tmp_path / 'receipt.json'
    monkeypatch.setattr(ops, 'RECEIPT', receipt)
    monkeypatch.setattr(ops, 'ensure_settings_unchanged', lambda: None)
    monkeypatch.setattr(ops, 'require_switch_interfaces', lambda: (_ for _ in ()).throw(
        compat.UnsupportedRuntime('unsupported fixture')))
    monkeypatch.setattr(ops, 'runtime_python', lambda: pytest.fail('runtime lookup must follow compatibility'))
    worker = ops.WindowsOps({'state': 'accepted'})
    with pytest.raises(SwitchError, match='unsupported fixture'):
        worker.preflight({'codex': None, 'claude': None})
    assert worker.record == {'state': 'accepted'}
    assert not receipt.exists()


def test_changed_settings_refuse_before_any_apply_command(monkeypatch, tmp_path):
    import json
    data = {'hermes_root': str(tmp_path), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Desktop.exe'), 'gateway_service': 'FixtureGateway',
            'codex': {'one': {'label': 'One', 'email': 'one@example.invalid'}}, 'claude': {}}
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    with ops.bound_settings():
        data['codex']['one']['email'] = 'changed@example.invalid'
        path.write_text(json.dumps(data), encoding='utf-8')
        monkeypatch.setattr(ops, 'command', lambda *a, **kw: pytest.fail('No command may run'))
        monkeypatch.setattr(ops, 'service', lambda: pytest.fail('No consumer check after changed settings'))
        worker = ops.WindowsOps({'state': 'accepted'})
        with pytest.raises(Exception, match='changed during this operation'):
            worker.apply({'codex': 'one'})
        assert worker.record == {'state': 'accepted'}


@pytest.mark.parametrize('stamp', ['missing', 'old', 'fresh'])
def test_gateway_ledger_requires_fresh_heartbeat(monkeypatch, tmp_path, stamp):
    from datetime import datetime, timedelta, timezone
    import json
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    monkeypatch.setattr(ops.psutil, 'pid_exists', lambda pid: True)
    data = {'pid': 123, 'gateway_state': 'running', 'active_agents': 0}
    if stamp != 'missing':
        delta = timedelta(minutes=10) if stamp == 'old' else timedelta(seconds=1)
        data['updated_at'] = (datetime.now(timezone.utc) - delta).isoformat()
    (tmp_path / 'gateway_state.json').write_text(json.dumps(data), encoding='utf-8')
    if stamp == 'fresh':
        assert ops.gateway_states()[0][1]['active_agents'] == 0
    else:
        with pytest.raises(SwitchError, match='activity is unknown'):
            ops.gateway_states()


def test_missing_profile_enable_blocks_before_desktop_or_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    package = tmp_path / 'plugins/codex-account-switch/dashboard'
    package.mkdir(parents=True)
    (package / 'manifest.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'plugins/claude-subscription-directsdk').mkdir()
    (tmp_path / 'config.yaml').write_text(
        'plugins:\n  enabled: [codex-account-switch, claude-subscription-directsdk]\n', encoding='utf-8')
    profile = tmp_path / 'profiles' / 'other'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('plugins:\n  enabled: [claude-subscription-directsdk]\n', encoding='utf-8')
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {}, 'claude': {}})
    monkeypatch.setattr(ops, 'desktop', lambda: pytest.fail('No Desktop scan while plugin is unavailable'))
    result = ops.preflight()
    assert result['ok'] is False
    assert len(result['blockers']) == 1
    assert result['blockers'][0].startswith('other: Account Switcher is not enabled in this profile.')
