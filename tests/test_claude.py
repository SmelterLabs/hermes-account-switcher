"""Claude account stores, identity and the post-restart rules. Hermetic: temp homes, fake CLI, fake Anthropic."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops
from switch_core import SwitchError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    monkeypatch.setattr(ops, 'CLAUDE_AUTH', tmp_path / 'claude-auth')
    monkeypatch.setattr(ops, 'runtime_python', lambda: tmp_path / 'fixture-python.exe')
    (tmp_path / 'config.yaml').write_text('{}', encoding='utf-8')
    (tmp_path / '.env').write_text('OTHER_KEY=keep-me\n', encoding='utf-8')
    for name in ('beta', 'alpha'):
        (tmp_path / 'profiles' / name).mkdir(parents=True)
        (tmp_path / 'profiles' / name / 'config.yaml').write_text('{}', encoding='utf-8')
    (tmp_path / 'profiles' / 'beta' / '.env').write_text('export QUOTED="x"\n', encoding='utf-8')
    # env_set.py is a Hermes-venv subprocess in production; emulate its .env edit here.
    def fake_command(args, timeout=60, home=None):
        assert args[1] == str(ops.ENV_SET) and args[2] == ops.CLAUDE_VAR
        path = Path(home) / '.env'
        lines = [l for l in (path.read_text(encoding='utf-8').splitlines() if path.exists() else [])
                 if not l.startswith(ops.CLAUDE_VAR + '=')]
        if args[3] != '--remove':
            lines.append(f'{ops.CLAUDE_VAR}={args[3]}')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    monkeypatch.setattr(ops, 'command', fake_command)
    with ops.bound_settings():
        yield tmp_path


def login(key, email, expires_in=3600, cached=None):
    folder = ops.claude_dir(key)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
        'accessToken': f'fixture-access-{key}', 'refreshToken': 'fixture-refresh',
        'expiresAt': int((time.time() + expires_in) * 1000)}}), encoding='utf-8')
    (folder / '.claude.json').write_text(json.dumps({'oauthAccount': {'emailAddress': cached or email}}), encoding='utf-8')


def test_selection_is_unset_until_every_store_names_one_folder(home):
    assert ops.claude_selection()['state'] == 'unset'
    ops.apply_claude('gmail')
    state = ops.claude_selection()
    assert state == {'selected': 'gmail', 'state': 'set', 'stores': {home.name: 'gmail', 'alpha': 'gmail', 'beta': 'gmail'}}
    assert 'OTHER_KEY=keep-me' in (home / '.env').read_text() and 'QUOTED' in (home / 'profiles/beta/.env').read_text()
    # The value the plugin reads must resolve to the Hermes-owned folder.
    assert Path(ops.env_values(home)[ops.CLAUDE_VAR]).resolve() == ops.claude_dir('gmail').resolve()


def test_disagreeing_or_foreign_stores_are_mixed_not_guessed(home):
    ops.apply_claude('gmail')
    (home / 'profiles/alpha/.env').write_text(f'{ops.CLAUDE_VAR}=C:/somewhere/else\n', encoding='utf-8')
    assert ops.claude_selection()['state'] == 'mixed'
    (home / 'profiles/alpha/.env').write_text('', encoding='utf-8')
    assert ops.claude_selection()['state'] == 'mixed'


def test_unset_removes_the_variable_everywhere(home):
    ops.apply_claude('work')
    ops.apply_claude('unset')
    assert ops.claude_selection()['state'] == 'unset'
    assert 'OTHER_KEY=keep-me' in (home / '.env').read_text()


def test_apply_refuses_when_readback_disagrees(home, monkeypatch):
    monkeypatch.setattr(ops, 'claude_selection', lambda: {'selected': 'gmail', 'state': 'set', 'stores': {}})
    with pytest.raises(SwitchError, match='readback'):
        ops.apply_claude('work')


def test_accounts_summary_never_carries_tokens(home):
    login('anthropic', 'first@example.invalid')
    rows = ops.claude_accounts()
    assert [r['key'] for r in rows] == ['anthropic', 'gmail', 'work']
    assert rows[0]['logged_in'] is True and rows[1]['logged_in'] is False
    assert 'fixture-access' not in json.dumps(rows)


def test_identity_uses_token_owner_not_cached_label(home, monkeypatch):
    login('gmail', 'second@example.invalid', cached='wrong-label@example.invalid')
    monkeypatch.setattr(ops, '_oauth_profile', lambda token: 'second@example.invalid')
    assert ops.claude_identity('gmail') == {'email': 'second@example.invalid', 'method': 'token'}


def test_one_network_blip_does_not_read_as_an_unverifiable_login(home, monkeypatch):
    # Seen on the test machine: about one identity check in seven failed to reach Anthropic and passed on
    # the next try, which would have refused a switch for nothing.
    login('gmail', 'second@example.invalid')
    monkeypatch.setattr(ops.time, 'sleep', lambda seconds: None)
    calls = []

    def flaky(token):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('fixture network blip')
        return 'second@example.invalid'
    monkeypatch.setattr(ops, '_oauth_profile', flaky)
    assert ops.claude_identity('gmail') == {'email': 'second@example.invalid', 'method': 'token'}
    assert len(calls) == 2

    def down(token):
        calls.append(1)
        raise OSError('fixture outage')
    calls.clear()
    monkeypatch.setattr(ops, '_oauth_profile', down)
    with pytest.raises(SwitchError, match='could not be verified'):
        ops.claude_identity('gmail')
    assert len(calls) == 2


def test_identity_rejects_wrong_owner_without_leaking(home, monkeypatch):
    login('gmail', 'second@example.invalid')
    monkeypatch.setattr(ops, '_oauth_profile', lambda token: 'someone@example.invalid')
    with pytest.raises(SwitchError) as e:
        ops.claude_identity('gmail')
    assert 'fixture-access' not in str(e.value)
    assert 'someone@example.invalid, not second@example.invalid' in str(e.value)


def test_identity_missing_login_names_the_helper(home):
    with pytest.raises(SwitchError, match='setup.cmd claude work'):
        ops.claude_identity('work')


def test_expired_token_gets_one_handshake_then_refuses_cached_label(home, monkeypatch):
    login('anthropic', 'first@example.invalid', expires_in=-10)
    calls = []
    monkeypatch.setattr(ops, '_handshake', lambda key: calls.append(key))
    monkeypatch.setattr(ops, '_oauth_profile', lambda token: pytest.fail('expired token must not be sent'))
    with pytest.raises(SwitchError, match='could not be verified'):
        ops.claude_identity('anthropic')
    assert calls == ['anthropic']


def test_expired_token_refreshed_by_handshake_is_verified_strongly(home, monkeypatch):
    login('anthropic', 'first@example.invalid', expires_in=-10)
    monkeypatch.setattr(ops, '_handshake', lambda key: login('anthropic', 'first@example.invalid'))
    monkeypatch.setattr(ops, '_oauth_profile', lambda token: 'first@example.invalid')
    assert ops.claude_identity('anthropic')['method'] == 'token'


def test_desktop_relaunches_through_the_shell(monkeypatch):
    launched = []
    monkeypatch.setattr(ops.subprocess, 'Popen', lambda args, **kw: launched.append(args))
    ops.launch_desktop()
    assert launched == [['explorer.exe', str(ops.EXE)]]


@pytest.fixture
def verifying(home, monkeypatch):
    """A WindowsOps whose restart already happened; only the verification rules are real."""
    worker = ops.WindowsOps({'started_at': 100.0, 'old_desktop_pid': 1, 'old_gateway_pid': 2})
    worker.old_main = SimpleNamespace(pid=1)
    worker.old_gateway = 2
    monkeypatch.setattr(worker, 'stage', lambda name: None)
    monkeypatch.setattr(ops, 'desktop', lambda: SimpleNamespace(pid=11))
    monkeypatch.setattr(ops, 'service', lambda: {'pid': 22, 'status': 'running'})
    monkeypatch.setattr(ops, 'backends', lambda main: [{'profile': 'beta', 'pid': 33, 'born': 200.0, 'port': 1, 'token': 't'}])
    monkeypatch.setattr(ops, 'wait_for', lambda fn, timeout, message: fn() or (_ for _ in ()).throw(SwitchError(message)))
    monkeypatch.setattr(ops, 'codex_stores', lambda: [(home, {})])
    monkeypatch.setattr(ops, 'common_selection', lambda stores: 'work')
    monkeypatch.setattr(ops, 'accounts', lambda: {'work': {}})
    monkeypatch.setattr(ops, 'RECEIPT', home / 'receipt.json')
    return worker


def test_codex_quota_failure_after_restart_is_a_warning_not_a_failure(verifying, monkeypatch):
    monkeypatch.setattr(ops, 'call', lambda b, route, body=None: {'selected': 'work', 'uses_codex': True, 'live_pools': [{'selected': 'work'}]})
    monkeypatch.setattr(ops, 'usage', lambda item: (_ for _ in ()).throw(SwitchError('OpenAI unreachable')))
    result = verifying.verify({'codex': 'work', 'claude': None})
    assert result['codex'] == 'work' and result['new_desktop_pid'] == 11
    assert verifying.record['warnings'] == ['Switch verified; OpenAI quota re-check failed afterwards: OpenAI unreachable']


def test_claude_verification_requires_backend_env_and_every_store(verifying, monkeypatch):
    ops.apply_claude('work')
    login('work', 'third@example.invalid')
    monkeypatch.setattr(ops, '_oauth_profile', lambda token: 'third@example.invalid')
    good = {'selected': None, 'uses_codex': False, 'live_pools': [], 'claude_config_dir': ops.claude_value('work')}
    monkeypatch.setattr(ops, 'call', lambda b, route, body=None: good)
    result = verifying.verify({'codex': None, 'claude': 'work'})
    assert result['claude'] == 'work' and result['claude_identity'] == 'token'
    # A backend still holding the old folder in its environment is not verified.
    monkeypatch.setattr(ops, 'call', lambda b, route, body=None: {**good, 'claude_config_dir': None})
    with pytest.raises(SwitchError, match='Not every reopened Desktop backend'):
        verifying.verify({'codex': None, 'claude': 'work'})


def test_recovery_restores_an_unset_claude_selection(home, monkeypatch):
    ops.apply_claude('gmail')
    worker = ops.WindowsOps({})
    worker.old_tree = []
    monkeypatch.setattr(worker, 'stage', lambda name: None)
    monkeypatch.setattr(worker, 'start', lambda: None)
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped'})
    worker.recover({'claude': 'unset'}, changed=True)
    assert ops.claude_selection()['state'] == 'unset'
    assert worker.record['recovery'] == 'previous_preference_restored'


def test_profile_hermes_marked_deleted_is_not_a_store(home):
    # A half-deleted profile keeps its folder while Hermes refuses every write to it.
    ghost = home / 'profiles' / 'ghost'
    ghost.mkdir()
    (ghost / 'config.yaml').write_text('{}', encoding='utf-8')
    (home / 'profiles' / '.deleted').mkdir()
    (home / 'profiles' / '.deleted' / 'ghost').write_text('deleted\n', encoding='utf-8')
    assert [p.name for p in ops.profile_homes()] == [home.name, 'alpha', 'beta']
    ops.apply_claude('gmail')
    assert ops.claude_selection()['state'] == 'set' and not (ghost / '.env').exists()


def test_recovery_reopens_hermes_even_when_the_restore_fails(home, monkeypatch):
    worker = ops.WindowsOps({})
    started = []
    calls = []

    def refuse(request, allow_changed=False):
        calls.append(allow_changed)
        raise SwitchError('store refused the write')
    monkeypatch.setattr(worker, 'apply', refuse)
    monkeypatch.setattr(worker, 'start', lambda: started.append(True))
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped'})
    worker.recover({'claude': 'work'}, changed=True)
    assert calls == [True]
    assert started == [True]
    assert worker.record['recovery'] == 'restore_failed; profile stores may disagree'


PROVIDER = ops.CLAUDE_PROVIDER


@pytest.fixture
def provider_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    monkeypatch.setattr(ops, 'runtime_python', lambda: tmp_path / 'fixture-python.exe')
    package = tmp_path / 'plugins/codex-account-switch/dashboard'
    package.mkdir(parents=True)
    (package / 'manifest.json').write_text('{}', encoding='utf-8')
    for home in (tmp_path, tmp_path / 'profiles/beta', tmp_path / 'profiles/alpha'):
        home.mkdir(parents=True, exist_ok=True)
        (home / 'config.yaml').write_text(
            json.dumps({'plugins': {'enabled': ['codex-account-switch', PROVIDER]}}), encoding='utf-8')
    (tmp_path / '.env').write_text('OTHER_KEY=keep-me\n', encoding='utf-8')
    return tmp_path


def enable_provider(home, enabled=True, disabled=False):
    (home / 'config.yaml').write_text(json.dumps({'plugins': {
        'enabled': ['codex-account-switch', PROVIDER] if enabled else ['codex-account-switch'],
        'disabled': [PROVIDER] if disabled else []}}), encoding='utf-8')


def test_absent_provider_blocks_preflight_without_scans_or_mutation(provider_home, monkeypatch):
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {}, 'claude': {}})
    monkeypatch.setattr(ops, 'desktop', lambda: pytest.fail('No Desktop scan while the provider is missing'))
    monkeypatch.setattr(ops, 'command', lambda *a, **kw: pytest.fail('No mutation while the provider is missing'))
    result = ops.preflight()
    assert result['ok'] is False
    assert result['blockers'] == [
        'The Claude subscription provider plugin is not installed; Claude switching would have no effect. '
        'Install it with "hermes plugins install claude-subscription-directsdk".']
    assert (provider_home / '.env').read_text() == 'OTHER_KEY=keep-me\n'


def test_disabled_provider_blocks_even_when_also_enabled(provider_home):
    (provider_home / 'plugins' / PROVIDER).mkdir()
    enable_provider(provider_home, enabled=True, disabled=True)
    enable_provider(provider_home / 'profiles/beta', enabled=True, disabled=True)
    blockers = ops.claude_provider_blockers()
    # Hermes's first profile is named "default" to the person reading this, never after its folder.
    assert blockers == [
        'default: the Claude subscription provider plugin is disabled.',
        'beta: the Claude subscription provider plugin is disabled.']


def test_the_provider_needs_no_enabled_entry_and_is_found_under_its_real_name(provider_home):
    # Seen while preparing the first live Claude run: the provider installs as
    # "claude-subscription-directsdk-experimental" and, being a model provider, is loaded by Hermes without
    # an "enabled" entry. The check looked for another folder name and demanded the entry, so every real
    # install would have been refused.
    assert ops.CLAUDE_PROVIDER == 'claude-subscription-directsdk-experimental'
    (provider_home / 'plugins' / ops.CLAUDE_PROVIDER).mkdir()
    for home in (provider_home, provider_home / 'profiles/beta', provider_home / 'profiles/alpha'):
        enable_provider(home, enabled=False)
    assert ops.claude_provider_blockers() == []


def test_installed_and_enabled_provider_clears_provider_blockers(provider_home):
    (provider_home / 'plugins' / PROVIDER).mkdir()
    assert ops.claude_provider_blockers() == []
    assert ops.plugin_profile_blockers() == []


def test_codex_only_configuration_is_never_blocked_by_the_missing_provider(provider_home, monkeypatch):
    data = {'hermes_root': str(provider_home), 'hermes_source': str(provider_home / 'source'),
            'desktop_exe': str(provider_home / 'Desktop.exe'), 'gateway_service': 'FixtureGateway',
            'codex': {'one': {'label': 'One', 'email': 'one@example.invalid'}}, 'claude': {}}
    path = provider_home / 'codex-only-settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    assert ops.claude_provider_blockers() == []


def test_switch_request_refuses_before_identity_when_provider_is_absent(provider_home, monkeypatch):
    monkeypatch.setattr(ops, 'require_switch_interfaces', lambda: None)
    monkeypatch.setattr(ops, 'runtime_python', lambda: provider_home / 'fixture-python.exe')
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {}, 'claude': {}})
    monkeypatch.setattr(ops, 'desktop', lambda: pytest.fail('No Desktop scan while the provider is missing'))
    monkeypatch.setattr(ops, 'command', lambda *a, **kw: pytest.fail('No mutation while the provider is missing'))
    worker = ops.WindowsOps({'state': 'accepted'})
    with ops.bound_settings():
        with pytest.raises(SwitchError, match='provider plugin is not installed'):
            worker.preflight({'codex': None, 'claude': 'gmail'})
    assert (provider_home / '.env').read_text() == 'OTHER_KEY=keep-me\n'


# Seen on the author's own PC: the other Codex account is the idle one by definition, so its access token had
# lapsed, and a switch to it was refused as "expired" although Hermes renews it at first use.

def test_a_switch_to_a_login_whose_token_lapsed_goes_ahead_with_a_note(home, monkeypatch):
    from types import SimpleNamespace
    from test_switch import entries, token_with
    from switch_core import identify
    rows = entries()
    rows[1]['access_token'] = token_with('work@example.invalid', 'account-two', exp=1000)
    lapsed = identify(rows)
    monkeypatch.setattr(ops, 'ensure_settings_unchanged', lambda: None)
    monkeypatch.setattr(ops, 'require_switch_interfaces', lambda: None)
    monkeypatch.setattr(ops, 'runtime_python', lambda: home / 'fixture-python.exe')
    monkeypatch.setattr(ops, 'preflight', lambda: {'ok': True, 'blockers': [], 'codex': {'selected': 'personal'},
                                                   'claude': {'selected': None, 'state': 'unset'}})
    monkeypatch.setattr(ops, 'accounts', lambda: lapsed)
    monkeypatch.setattr(ops, 'usage', lambda item: pytest.fail('a lapsed token is not sent to OpenAI'))
    monkeypatch.setattr(ops, 'desktop', lambda: SimpleNamespace(pid=11))
    monkeypatch.setattr(ops, 'backends', lambda main: [])
    monkeypatch.setattr(ops, 'gateway_in_use', lambda: False)
    monkeypatch.setattr(ops, 'RECEIPT', home / 'receipt.json')
    worker = ops.WindowsOps({'state': 'accepted'})
    monkeypatch.setattr(worker, 'stage', lambda name: None)
    with ops.bound_settings():
        worker.preflight({'codex': 'work', 'claude': None})
    [note] = worker.record['warnings']
    assert note.startswith("The Work login's access token had lapsed") and 'renews' in note
    assert worker.record['previous'] == {'codex': 'personal'}
    # A login whose token is good is still asked about.
    asked = []
    monkeypatch.setattr(ops, 'accounts', lambda: identify(entries()))
    monkeypatch.setattr(ops, 'usage', lambda item: asked.append(item['key']))
    worker = ops.WindowsOps({'state': 'accepted'})
    monkeypatch.setattr(worker, 'stage', lambda name: None)
    with ops.bound_settings():
        worker.preflight({'codex': 'work', 'claude': None})
    assert asked == ['work'] and 'warnings' not in worker.record
