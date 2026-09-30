"""Backend discovery across the legacy module and managed launcher forms."""
import io
from pathlib import Path
import sys
from types import SimpleNamespace
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops


def test_workers_use_committed_runtime_without_mid_operation_relaunch(monkeypatch, tmp_path):
    pm = ModuleType('pm')
    environments = ModuleType('pm.environments')
    pm.environments = environments
    monkeypatch.setitem(sys.modules, 'pm', pm)
    monkeypatch.setitem(sys.modules, 'pm.environments', environments)
    managed = tmp_path / 'managed-venv'
    (managed / 'Scripts').mkdir(parents=True)
    (managed / 'Scripts/python.exe').touch()
    monkeypatch.setattr(environments, 'committed_venv', lambda root: managed, raising=False)
    assert ops.runtime_python() == managed / 'Scripts/python.exe'
    monkeypatch.setattr(environments, 'committed_venv', lambda root: None)
    with pytest.raises(Exception, match='unsupported'):
        ops.runtime_python()


@pytest.mark.parametrize('launcher', ['module', 'managed_python', 'managed_isolated_python', 'managed_exe'])
def test_discovers_authenticated_desktop_listener(monkeypatch, launcher):
    managed = str(ops.ROOT / 'hermes-agent/.hermes/bin/hermes.exe')
    prefix = {'module': ['python.exe', '-m', 'hermes_cli.main'],
              'managed_python': ['python.exe', managed],
              'managed_isolated_python': ['python.exe', '-I', managed],
              'managed_exe': [managed]}[launcher]
    command = prefix + ['--profile', 'beta', 'serve', '--port', '0']
    child = SimpleNamespace(pid=42, cmdline=lambda: command, create_time=lambda: 100,
                            environ=lambda: {'HERMES_DASHBOARD_SESSION_TOKEN': 'fixture-only'},
                            net_connections=lambda **kw: [SimpleNamespace(
                                status=ops.psutil.CONN_LISTEN, laddr=SimpleNamespace(port=12345))])
    main = SimpleNamespace(children=lambda **kw: [child])
    probes = []

    def respond(request, timeout):
        probes.append(request)
        return io.BytesIO(b'{"gateway_state":"running"}')

    monkeypatch.setattr(ops.urllib.request, 'urlopen', respond)
    result = ops.backends(main)
    assert [(row['pid'], row['profile'], row['port']) for row in result] == [(42, 'beta', 12345)]
    assert probes[0].get_header('Authorization') == 'Bearer fixture-only'


def test_stock_single_profile_backend_has_no_profile_argument(monkeypatch):
    # Command line seen on a real stock install, 2026-09-28.
    managed = str(ops.ROOT / 'hermes-agent/.hermes/bin/hermes.exe')
    command = ['python.exe', '-I', managed, 'serve', '--host', '127.0.0.1', '--port', '0']
    child = SimpleNamespace(pid=42, cmdline=lambda: command, create_time=lambda: 100,
                            environ=lambda: {'HERMES_DASHBOARD_SESSION_TOKEN': 'fixture-only'},
                            net_connections=lambda **kw: [SimpleNamespace(
                                status=ops.psutil.CONN_LISTEN, laddr=SimpleNamespace(port=12345))])
    monkeypatch.setattr(ops.urllib.request, 'urlopen', lambda request, timeout: io.BytesIO(b'{"gateway_state":"running"}'))
    result = ops.backends(SimpleNamespace(children=lambda **kw: [child]))
    assert [(row['pid'], row['profile']) for row in result] == [(42, 'default')]


@pytest.mark.parametrize('command', [
    ['python.exe', '-c', 'hermes_cli.main', '--profile', 'beta', 'serve'],
    ['C:/unrelated/hermes.exe', '--profile', 'beta', 'serve'],
    ['python.exe', 'C:/unrelated/hermes.exe', '--profile', 'beta', 'serve'],
])
def test_unrelated_descendant_is_not_a_backend(command):
    child = SimpleNamespace(cmdline=lambda: command,
                            environ=lambda: pytest.fail('Unrelated process must not be probed'))
    with pytest.raises(ops.SwitchError, match='No live Desktop backends'):
        ops.backends(SimpleNamespace(children=lambda **kw: [child]))
