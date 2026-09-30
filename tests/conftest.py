"""All identities and homes in the suite are synthetic."""
import json
import pytest


@pytest.fixture(autouse=True)
def fixture_settings(tmp_path, monkeypatch):
    data = {
        'hermes_root': str(tmp_path),
        'hermes_source': str(tmp_path / 'source'),
        'desktop_exe': str(tmp_path / 'Hermes.exe'),
        'gateway_service': 'FixtureGateway',
        'codex': {
            'personal': {'label': 'Personal', 'email': 'personal@example.invalid'},
            'work': {'label': 'Work', 'email': 'work@example.invalid'},
        },
        'claude': {
            'anthropic': {'label': 'Anthropic', 'email': 'first@example.invalid', 'directory': str(tmp_path / 'claude-auth' / 'anthropic')},
            'gmail': {'label': 'Gmail', 'email': 'second@example.invalid', 'directory': str(tmp_path / 'claude-auth' / 'gmail')},
            'work': {'label': 'Work', 'email': 'third@example.invalid', 'directory': str(tmp_path / 'claude-auth' / 'work')},
        },
    }
    path = tmp_path / 'switch-settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
