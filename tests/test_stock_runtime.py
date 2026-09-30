"""The backend must import and parse profile configuration on stock Hermes, which ships
hermes_yaml (ruamel-backed) instead of PyYAML. The host parser is modeled explicitly in
this suite; the real isolated-host import is a separate acceptance probe."""
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops


def model_host_yaml(monkeypatch, parse):
    module = types.ModuleType('hermes_yaml')
    module.safe_load = parse
    monkeypatch.setitem(sys.modules, 'hermes_yaml', module)
    monkeypatch.setitem(sys.modules, 'yaml', None)


def without_any_host_yaml(monkeypatch):
    monkeypatch.setitem(sys.modules, 'yaml', None)
    monkeypatch.setitem(sys.modules, 'hermes_yaml', None)


def install_backend_package(root):
    package = root / 'plugins/codex-account-switch/dashboard'
    package.mkdir(parents=True, exist_ok=True)
    (package / 'manifest.json').write_text('{}', encoding='utf-8')


def write_config(home, enabled=(), disabled=()):
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(
        json.dumps({'plugins': {'enabled': list(enabled), 'disabled': list(disabled)}}), encoding='utf-8')


def test_importing_the_backend_needs_no_pyyaml(monkeypatch):
    for name in ('windows_ops', 'compat', 'switch_core', 'settings'):
        monkeypatch.delitem(sys.modules, name, raising=False)
    without_any_host_yaml(monkeypatch)
    assert importlib.import_module('windows_ops') is not None


def test_profile_configuration_parses_through_host_hermes_yaml(monkeypatch, tmp_path):
    model_host_yaml(monkeypatch, json.loads)
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    install_backend_package(tmp_path)
    write_config(tmp_path, ['codex-account-switch'])
    write_config(tmp_path / 'profiles' / 'beta', ['codex-account-switch'])
    assert ops.plugin_profile_blockers() == []


def test_denylist_wins_over_allowlist_for_host_parsed_configuration(monkeypatch, tmp_path):
    model_host_yaml(monkeypatch, json.loads)
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    install_backend_package(tmp_path)
    write_config(tmp_path, ['codex-account-switch'])
    write_config(tmp_path / 'profiles' / 'beta', ['codex-account-switch'], ['codex-account-switch'])
    assert ops.plugin_profile_blockers() == [
        'beta: Account Switcher is not enabled in this profile. Install it into this profile\'s folder, run '
        '"hermes -p beta plugins enable codex-account-switch", then restart Hermes Desktop.']


def test_the_first_profile_is_told_the_command_without_a_profile_flag(monkeypatch, tmp_path):
    # Seen on a real stock install: "hermes: ... backend is not enabled" named a folder and no way out.
    model_host_yaml(monkeypatch, json.loads)
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    install_backend_package(tmp_path)
    write_config(tmp_path, [])
    assert ops.plugin_profile_blockers() == [
        'default: Account Switcher is not enabled in this profile. Install it into this profile\'s folder, run '
        '"hermes plugins enable codex-account-switch", then restart Hermes Desktop.']


def test_missing_host_parser_is_an_explicit_refusal(monkeypatch, tmp_path):
    without_any_host_yaml(monkeypatch)
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    install_backend_package(tmp_path)
    write_config(tmp_path, ['codex-account-switch'])
    blockers = ops.plugin_profile_blockers()
    assert len(blockers) == 1 and 'No YAML parser is available' in blockers[0]


def test_broken_host_parser_is_an_explicit_refusal(monkeypatch, tmp_path):
    def broken(text):
        raise ValueError('fixture parse failure')

    model_host_yaml(monkeypatch, broken)
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    install_backend_package(tmp_path)
    write_config(tmp_path, ['codex-account-switch'])
    blockers = ops.plugin_profile_blockers()
    assert len(blockers) == 1 and 'could not parse the profile configuration' in blockers[0]


def test_preflight_refuses_without_desktop_scans_when_no_parser_exists(monkeypatch, tmp_path):
    without_any_host_yaml(monkeypatch)
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    install_backend_package(tmp_path)
    (tmp_path / 'plugins/claude-subscription-directsdk').mkdir(parents=True)
    write_config(tmp_path, ['codex-account-switch', 'claude-subscription-directsdk'])
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {}, 'claude': {}})
    monkeypatch.setattr(ops, 'desktop', lambda: pytest.fail('No Desktop scan without a parser'))
    result = ops.preflight()
    assert result['ok'] is False
    assert 'No YAML parser is available' in ' '.join(result['blockers'])
