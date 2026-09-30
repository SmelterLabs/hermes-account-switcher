"""Portable configuration must never infer identity from a display label."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
from settings import SettingsError, load_settings


def test_missing_settings_requires_setup(tmp_path):
    with pytest.raises(SettingsError, match='settings'):
        load_settings(tmp_path / 'missing.json')
    assert not (tmp_path / 'missing.json').exists()


def test_arbitrary_accounts_and_runtime_paths(tmp_path):
    data = {
        'hermes_root': str(tmp_path / 'hermes'),
        'hermes_source': str(tmp_path / 'source'),
        'desktop_exe': str(tmp_path / 'Hermes.exe'),
        'gateway_service': 'FixtureGateway',
        'codex': {'alpha': {'label': 'A', 'email': 'a@example.invalid'},
                  'beta': {'label': 'B', 'email': 'b@example.invalid'},
                  'gamma': {'label': 'C', 'email': 'c@example.invalid'}},
        'claude': {'first': {'label': 'First', 'email': 'one@example.invalid', 'directory': str(tmp_path / 'one')},
                   'second': {'label': 'Second', 'email': 'two@example.invalid', 'directory': str(tmp_path / 'two')}}
    }
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    settings = load_settings(path)
    assert list(settings.codex) == ['alpha', 'beta', 'gamma']
    assert settings.claude['second'].directory == tmp_path / 'two'
    assert settings.gateway_service == 'FixtureGateway'


def test_three_codex_accounts_use_verified_claims_and_arbitrary_keys(tmp_path, monkeypatch):
    from test_switch import token
    from switch_core import identify, normalize_request, selected, summaries
    data = {'hermes_root': str(tmp_path), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Hermes.exe'), 'gateway_service': 'FixtureGateway',
            'codex': {key: {'label': key.upper(), 'email': f'{key}@example.invalid'}
                      for key in ('alpha', 'beta', 'gamma')}, 'claude': {}}
    path = tmp_path / 'arbitrary.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    rows = [{'id': key, 'priority': priority, 'label': 'untrusted',
             'access_token': token(f'{key}@example.invalid', f'account-{key}')}
            for priority, key in enumerate(('beta', 'gamma', 'alpha'))]
    accounts = identify(rows)
    assert selected(accounts) == 'beta'
    assert [row['key'] for row in summaries(accounts)] == ['beta', 'gamma', 'alpha']
    assert normalize_request({'codex': 'gamma', 'confirmed': True})['codex'] == 'gamma'


def test_default_settings_path_is_shared_across_profiles(tmp_path, monkeypatch):
    from settings import settings_path
    monkeypatch.delenv('HERMES_SWITCH_SETTINGS', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    root_path = settings_path()
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profiles' / 'worker'))
    assert settings_path() == root_path


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_one_provider_can_be_configured_without_the_other(tmp_path, monkeypatch, provider):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
    import windows_ops as ops
    from switch_core import normalize_request, SwitchError
    data = {'hermes_root': str(tmp_path), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Hermes.exe'), 'gateway_service': 'FixtureGateway',
            'codex': {}, 'claude': {}}
    if provider == 'codex':
        data['codex'] = {'only': {'label': 'Only', 'email': 'only@example.invalid'}}
    else:
        data['claude'] = {'only': {'label': 'Only', 'email': 'only@example.invalid',
                                   'directory': str(tmp_path / 'claude-only')}}
    path = tmp_path / 'one-provider.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    assert normalize_request({provider: 'only', 'confirmed': True})[provider] == 'only'
    other = 'claude' if provider == 'codex' else 'codex'
    with pytest.raises(SwitchError):
        normalize_request({other: 'only', 'confirmed': True})
    if provider == 'claude':
        assert ops.selection_summary()['codex'] == {'selected': None, 'accounts': [], 'effective': None, 'warnings': []}


def test_snapshot_pins_identity_and_rejects_file_change(tmp_path, monkeypatch):
    from settings import settings_snapshot, ensure_settings_unchanged
    data = {'hermes_root': str(tmp_path), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Hermes.exe'), 'gateway_service': 'FixtureGateway',
            'codex': {'one': {'label': 'One', 'email': 'one@example.invalid'}}, 'claude': {}}
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    with settings_snapshot():
        data['codex']['one']['email'] = 'changed@example.invalid'
        path.write_text(json.dumps(data), encoding='utf-8')
        assert load_settings().codex['one'].email == 'one@example.invalid'
        with pytest.raises(SettingsError, match='changed during this operation'):
            ensure_settings_unchanged()


def test_binding_updates_all_paths_after_late_setup(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
    import windows_ops as ops
    configured = tmp_path / 'custom-home'
    data = {'hermes_root': str(configured), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Desktop.exe'), 'gateway_service': 'FixtureGateway',
            'codex': {'one': {'label': 'One', 'email': 'one@example.invalid'}}, 'claude': {}}
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    with ops.bound_settings():
        assert ops.ROOT == configured
        assert ops.LOCK == configured / 'logs/codex-account-switch.lock'
        assert ops.RECEIPT == configured / 'logs/codex-account-switch-last.json'
        assert ops.EXE == tmp_path / 'Desktop.exe'


def test_gateway_health_target_must_be_loopback(tmp_path):
    data = {'hermes_root': str(tmp_path), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Desktop.exe'), 'gateway_service': 'FixtureGateway',
            'gateway_health_url': 'https://example.invalid/health',
            'codex': {'one': {'label': 'One', 'email': 'one@example.invalid'}}, 'claude': {}}
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(SettingsError, match='loopback'):
        load_settings(path)


@pytest.mark.parametrize(('change', 'message'), [
    ({'codex': {'bad/key': {'label': 'A', 'email': 'a@example.invalid'}}}, 'keys'),
    ({'codex': {'a': {'label': 'A', 'email': 'same@example.invalid'}, 'b': {'label': 'B', 'email': 'same@example.invalid'}}}, 'distinct'),
    ({'claude': {'a': {'label': 'A', 'email': 'a@example.invalid', 'directory': 'relative/path'}}}, 'absolute'),
])
def test_invalid_settings_fail_closed(tmp_path, change, message):
    data = {'hermes_root': str(tmp_path), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Hermes.exe'), 'gateway_service': 'FixtureGateway',
            'codex': {}, 'claude': {}}
    data.update(change)
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(SettingsError, match=message):
        load_settings(path)
