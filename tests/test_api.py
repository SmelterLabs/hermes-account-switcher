"""API tests: all account, process, and file state is replaced by fixtures."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace, ModuleType
import threading
from contextlib import nullcontext
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import plugin_api as api
REAL_BOUND_SETTINGS = api.ops.bound_settings
from test_switch import entries
from switch_core import identify

CLAUDE_ROWS = [{'key': 'anthropic', 'label': 'Anthropic', 'email': 'first@example.invalid', 'logged_in': True, 'cached_email': None},
               {'key': 'gmail', 'label': 'Gmail', 'email': 'work@example.invalid', 'logged_in': False, 'cached_email': None},
               {'key': 'work', 'label': 'Work', 'email': 'third@example.invalid', 'logged_in': False, 'cached_email': None}]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api.ops, 'bound_settings', nullcontext)
    monkeypatch.setattr(api, 'settings_digest', lambda: 'fixture-digest')
    monkeypatch.setattr(api, 'desktop_idle_blockers', lambda: [])
    monkeypatch.setattr(api, 'authorize', lambda request: None)
    monkeypatch.setattr(api.ops, 'accounts', lambda: identify(entries()))
    # Status reads every profile's store; the fixture has root only, resolved at call time
    # so a test that breaks `accounts` still breaks status.
    monkeypatch.setattr(api.ops, 'codex_stores', lambda: [(api.ops.ROOT, api.ops.accounts())])
    monkeypatch.setattr(api.ops, 'claude_selection', lambda: {'selected': None, 'state': 'unset', 'stores': {}})
    monkeypatch.setattr(api.ops, 'claude_accounts', lambda: CLAUDE_ROWS)
    monkeypatch.setattr(api.ops, 'preflight', lambda own_state=None: {'ok': False, 'blockers': ['fixture conversation is busy'],
                                                       **api.ops.selection_summary()})
    monkeypatch.setattr(api.ops, 'RECEIPT', tmp_path / 'receipt.json')
    monkeypatch.setattr(api.ops, 'LOCK', tmp_path / 'lock.json')
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_status_contract_and_secret_allowlist(client, monkeypatch):
    monkeypatch.setattr(api.ops, 'preflight', lambda own_state=None: pytest.fail('Status must not run the full preflight'))
    monkeypatch.setattr(api.ops, 'desktop', lambda: pytest.fail('Status must not scan processes'))
    response = client.get('/status')
    assert response.status_code == 200
    data = response.json()
    assert data['codex']['selected'] == 'personal'
    assert [a['label'] for a in data['codex']['accounts']] == ['Personal', 'Work']
    assert data['claude'] == {'selected': None, 'state': 'unset', 'accounts': CLAUDE_ROWS}
    assert data['scope'] == 'All Hermes profiles on this PC'
    assert data['blockers'] == [] and data['in_progress'] is False and data['last_operation'] is None
    assert 'access_token' not in response.text and 'account-one' not in response.text


def test_missing_setup_returns_guidance_without_reading_auth(client, monkeypatch, tmp_path):
    monkeypatch.setattr(api.ops, 'bound_settings', REAL_BOUND_SETTINGS)
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(tmp_path / 'absent.json'))
    monkeypatch.setattr(api.ops, 'accounts', lambda: pytest.fail('auth must not be read before setup'))
    response = client.get('/status')
    assert response.status_code == 200
    assert response.json()['setup_needed'] is True and response.json()['codex']['accounts'] == []
    # Everything that would act still refuses, and says why.
    response = client.get('/preflight')
    assert response.status_code == 409
    assert 'settings are missing' in response.json()['detail']


@pytest.fixture
def codex_pool(monkeypatch):
    auth = ModuleType('hermes_cli.auth')
    monkeypatch.setitem(sys.modules, 'hermes_cli', ModuleType('hermes_cli'))
    monkeypatch.setitem(sys.modules, 'hermes_cli.auth', auth)
    monkeypatch.setattr(auth, 'read_credential_pool', lambda provider: entries(), raising=False)


def idle_proof_module(monkeypatch, answer):
    """A stand-in for Hermes's own idle proof: a dict, or an exception to raise."""
    module = ModuleType('hermes_cli.web_server_idle_proof')
    def idle_proof():
        if isinstance(answer, Exception):
            raise answer
        return answer
    module.idle_proof = idle_proof
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server_idle_proof', module)
    return module


def test_local_state_reads_the_selection_and_names_the_idle_check(client, codex_pool, monkeypatch):
    monkeypatch.setattr(api, 'desktop_idle_blockers', api._compat.desktop_idle_blockers)
    idle_proof_module(monkeypatch, {'idle': True, 'reason': None})
    state = client.get('/local-state')
    assert state.status_code == 200
    assert state.json()['selected'] == 'personal' and state.json()['uses_codex'] is True
    assert state.json()['blockers'] == []
    assert state.json()['activity_check'] == 'hermes_cli.web_server_idle_proof.idle_proof'
    assert 'live_pools' not in state.json() and 'guard_installed' not in state.json()


@pytest.mark.parametrize('proof,expected', [
    ({'idle': False, 'reason': 'turn_in_flight', 'detail': 'session:abc,def'},
     'A conversation is running, initializing, or waiting for input.'),
    ({'idle': False, 'reason': 'awaiting_human_input'}, 'A conversation is waiting for input.'),
    ({'idle': False, 'reason': 'turn_in_flight', 'detail': 'delegation'}, 'A conversation owns running background work.'),
    ({'idle': False, 'reason': 'turn_in_flight', 'detail': 'cron:daily'}, 'A scheduled job is running.'),
    ({'idle': False, 'reason': 'turn_in_flight', 'detail': 'retirement_admission'},
     'Hermes Desktop is already closing this backend.'),
    ({'idle': False, 'reason': 'turn_in_flight', 'detail': None}, 'Desktop is busy.'),
    ({'idle': None, 'reason': 'turn_probe_unavailable'}, 'Hermes could not tell whether Desktop is idle; retry in a moment.'),
])
def test_a_busy_or_unknown_desktop_is_a_blocker_in_plain_words(client, codex_pool, monkeypatch, proof, expected):
    """The refusal rests on Hermes's own idle proof: the session table, delegations, cron and prompts
    waiting on a person. Anything Hermes cannot read is a blocker, never idle."""
    monkeypatch.setattr(api, 'desktop_idle_blockers', api._compat.desktop_idle_blockers)
    idle_proof_module(monkeypatch, proof)
    state = client.get('/local-state').json()
    assert state['blockers'] == [expected]
    assert state['activity_check'] == 'hermes_cli.web_server_idle_proof.idle_proof'
    assert 'abc' not in json.dumps(state)  # session ids stay inside Hermes


def test_a_missing_idle_check_blocks_and_says_the_check_is_gone(client, codex_pool, monkeypatch):
    monkeypatch.setattr(api, 'desktop_idle_blockers', api._compat.desktop_idle_blockers)
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server_idle_proof', None)  # import fails
    state = client.get('/local-state').json()
    assert state['blockers'] == ["Hermes's idle check is unavailable; account switching is disabled."]
    assert state['activity_check'] is None


def test_the_plugin_has_no_admission_hold_routes(client):
    for route in ('/freeze', '/release'):
        assert client.post(route, json={}).status_code == 404


def test_preflight_carries_blockers_and_selection(client):
    data = client.get('/preflight').json()
    assert data['ok'] is False
    assert data['blockers'] == ['fixture conversation is busy']
    assert data['codex']['selected'] == 'personal' and data['claude']['state'] == 'unset'


def test_preflight_error_is_explicit_not_a_malformed_success(client, monkeypatch):
    def broken(own_state=None): raise api.SwitchError('No live Desktop backends were found.')
    monkeypatch.setattr(api.ops, 'preflight', broken)
    response = client.get('/preflight')
    assert response.status_code == 409
    assert response.json()['detail'] == 'No live Desktop backends were found.'


def test_preflight_unexpected_error_does_not_leak(client, monkeypatch):
    def broken(own_state=None): raise ValueError('fixture-private-secret')
    monkeypatch.setattr(api.ops, 'preflight', broken)
    response = client.get('/preflight')
    assert response.status_code == 409
    assert 'fixture-private-secret' not in response.text


@pytest.mark.parametrize('body', [{}, {'codex': 'work'}, {'codex': 'work', 'confirmed': 'true'},
    {'codex': 'work', 'confirmed': True, 'command': 'ignored'}, {'codex': '../wrong', 'confirmed': True},
    {'confirmed': True}, {'target': 'work', 'confirmed': True}])
def test_invalid_or_unconfirmed_post_has_no_side_effects(client, monkeypatch, body):
    monkeypatch.setattr(api.ops, 'desktop', lambda: pytest.fail('Process discovery must not happen'))
    assert client.post('/switch', json=body).status_code == 400
    assert not api.ops.LOCK.exists()


def test_pending_operation_reports_blocker_without_rechecking(client, monkeypatch):
    api.ops.LOCK.write_text('{}')
    monkeypatch.setattr(api.ops, 'preflight', lambda own_state=None: pytest.fail('Do not poll restarting backends'))
    data = client.get('/status').json()
    assert 'already in progress' in data['blockers'][0] and data['in_progress'] is True


def test_unknown_account_error_does_not_leak(client, monkeypatch):
    def broken(own_state=None): raise ValueError('fixture-private-secret')
    monkeypatch.setattr(api.ops, 'accounts', broken)
    response = client.get('/status')
    assert response.status_code == 409
    assert 'fixture-private-secret' not in response.text


def test_dead_operation_is_recovered_before_status(client, monkeypatch):
    import os
    import time
    api.ops.LOCK.write_text(json.dumps({'operation_id': 'abandoned', 'pid': 99999999}))
    os.utime(api.ops.LOCK, (time.time() - 60, time.time() - 60))
    api.ops.RECEIPT.write_text(json.dumps({'operation_id': 'abandoned', 'state': 'checking'}))
    monkeypatch.setattr(api.ops.psutil, 'pid_exists', lambda pid: False)
    monkeypatch.setattr(api.ops.psutil, 'process_iter', lambda attrs: [])
    data = client.get('/status').json()
    assert data['blockers'] == [] and data['in_progress'] is False
    assert data['last_operation']['state'] == 'failed'
    assert not api.ops.LOCK.exists()


def test_hermes_loads_the_api_without_putting_generic_modules_in_its_server(tmp_path):
    """Hermes's web server imports plugin_api.py by file path, in its own long-lived process. The siblings
    must not land there as top-level `settings`, `compat`, ... modules, nor dashboard/ on its sys.path."""
    import subprocess
    probe = """
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('hermes_dashboard_plugin_codex-account-switch', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.router.routes
generic = {'settings', 'compat', 'switch_core', 'windows_ops', 'first_run', 'desktop_gate', 'worker_launch'}
print(sorted(generic & set(sys.modules)), str(Path(sys.argv[1]).parent) in sys.path)
"""
    api_file = Path(__file__).parents[1] / 'dashboard' / 'plugin_api.py'
    result = subprocess.run([sys.executable, '-c', probe, str(api_file)], cwd=tmp_path, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ['[]', 'False']
