"""Stock Hermes starts its own gateway (no Windows service). Hermetic: fake processes, recorded commands."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops
from settings import SettingsError, load_settings
from switch_core import SwitchError

GATEWAY = ['C:/venv/Scripts/python.exe', '-m', 'hermes_cli.main', 'gateway', 'run']


def write_settings(tmp_path, monkeypatch, service):
    data = {'hermes_root': str(tmp_path), 'hermes_source': str(tmp_path / 'source'),
            'desktop_exe': str(tmp_path / 'Hermes.exe'), 'gateway_service': service,
            'codex': {'only': {'label': 'Only', 'email': 'only@example.invalid'}}, 'claude': {}}
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    return path


def processes(monkeypatch, *rows):
    found = [SimpleNamespace(info={'pid': pid, 'cmdline': cmdline, 'create_time': born}) for pid, cmdline, born in rows]
    monkeypatch.setattr(ops.psutil, 'process_iter', lambda attrs=None: iter(found))


@pytest.fixture
def stock(tmp_path, monkeypatch):
    write_settings(tmp_path, monkeypatch, None)
    monkeypatch.setattr(ops, 'runtime_python', lambda: tmp_path / 'fixture-python.exe')
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'bin' / 'hermes.exe').write_bytes(b'')
    monkeypatch.setattr(ops.time, 'sleep', lambda seconds: None)
    with ops.bound_settings():
        yield tmp_path


def test_settings_accept_null_service_and_reject_a_guess(tmp_path, monkeypatch):
    write_settings(tmp_path, monkeypatch, None)
    assert load_settings().gateway_service is None
    for bad in ('', 7, 'two words'):
        write_settings(tmp_path, monkeypatch, bad)
        with pytest.raises(SettingsError, match='gateway_service'):
            load_settings()


def test_no_gateway_process_reads_as_stopped(stock, monkeypatch):
    processes(monkeypatch, (10, ['python.exe', 'unrelated.py'], 1.0), (11, None, 2.0),
              # Hermes's own restart watcher and lifecycle verbs are not a running gateway.
              (12, ['python.exe', '-c', 'import subprocess; hermes gateway restart'], 3.0),
              (13, ['python.exe', '-m', 'hermes_cli.main', 'gateway', 'stop'], 4.0))
    assert ops.service() == {'status': 'stopped', 'pid': None, 'born': None}


def test_running_gateway_is_the_newest_matching_process(stock, monkeypatch):
    # A venv launcher parent shares the worker's command line; the worker is the younger one.
    processes(monkeypatch, (20, GATEWAY, 100.0), (21, GATEWAY, 100.4),
              (22, ['C:/x/hermes.exe', '--profile', 'alpha', 'GATEWAY', 'RUN'], 90.0))
    assert ops.service() == {'status': 'running', 'pid': 21, 'born': 100.4}


def test_a_recycled_pid_still_counts_as_a_restart():
    assert ops.gateway_identity({'pid': 5, 'born': 1.0}) != ops.gateway_identity({'pid': 5, 'born': 2.0})
    assert ops.gateway_identity({'pid': 5, 'status': 'running'}) == (5, None)   # a Windows service has no birth time


@pytest.fixture
def recorded(stock, monkeypatch):
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=1)   # Hermes's start prints a failure and still exits 0 or 1; never trusted
    monkeypatch.setattr(ops.subprocess, 'run', run)
    return calls


def test_stock_stop_uses_hermes_own_stop_per_owner_home(recorded, stock, monkeypatch):
    states = iter([{'status': 'running', 'pid': 21, 'born': 1.0}, {'status': 'stopped', 'pid': None, 'born': None}])
    monkeypatch.setattr(ops, 'service', lambda: next(states))
    worker = ops.WindowsOps({})
    worker.gateway_homes = [stock, stock / 'profiles' / 'alpha']
    worker.stop_gateway()
    assert [args[-2:] for args, _ in recorded] == [['gateway', 'stop'], ['gateway', 'stop']]
    assert [kw['env']['HERMES_HOME'] for _, kw in recorded] == [str(stock), str(stock / 'profiles' / 'alpha')]
    assert all('--all' not in args and 'powershell.exe' not in args for args, _ in recorded)
    assert all(kw['env']['HERMES_NONINTERACTIVE'] == '1' for _, kw in recorded)


def test_gateway_commands_go_through_hermes_own_launcher(recorded, stock):
    # Seen on a real stock install: `python -m hermes_cli.main gateway start` spawns a gateway that exits at once.
    ops.gateway_command('start', stock)
    (args, _), = recorded
    assert args == [str(stock / 'bin' / 'hermes.exe'), 'gateway', 'start']
    (stock / 'bin' / 'hermes.exe').unlink()
    with pytest.raises(SwitchError, match='Hermes command was not found'):
        ops.gateway_command('start', stock)


def test_stock_health_is_the_gateways_own_ledger_not_a_web_address(stock, monkeypatch):
    assert load_settings().gateway_health_url is None
    monkeypatch.setattr(ops, 'ROOT', stock)
    monkeypatch.setattr(ops.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('stock serves no health endpoint'))
    processes(monkeypatch, (21, GATEWAY, 100.4))
    ledger = {'pid': 21, 'gateway_state': 'running'}
    monkeypatch.setattr(ops, 'gateway_states', lambda require_drain=False: [(stock, ledger)])
    assert ops.gateway_healthy()
    ledger['pid'] = 20   # a ledger left behind by the previous process is not health
    assert not ops.gateway_healthy()


def test_a_service_install_keeps_the_web_health_check(tmp_path, monkeypatch):
    write_settings(tmp_path, monkeypatch, 'FixtureGateway')
    assert load_settings().gateway_health_url == 'http://127.0.0.1:8642/health'


def test_stock_stop_refuses_to_continue_while_a_gateway_survives(recorded, stock, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'running', 'pid': 21, 'born': 1.0})
    monkeypatch.setattr(ops, 'wait_for', lambda fn, timeout, message: fn() or (_ for _ in ()).throw(SwitchError(message)))
    with pytest.raises(SwitchError, match='did not stop'):
        ops.WindowsOps({}).stop_gateway()


def test_stock_start_never_installs_a_login_task_and_waits_for_a_real_process(recorded, stock, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'running', 'pid': 30, 'born': 9.0})
    ops.WindowsOps({}).start_gateway()
    (args, kw), = recorded
    assert args[-2:] == ['gateway', 'start'] and kw['env']['HERMES_HOME'] == str(stock)
    assert kw['env']['HERMES_GATEWAY_INSTALL_START_ON_LOGIN'] == '0'
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped', 'pid': None, 'born': None})
    monkeypatch.setattr(ops, 'wait_for', lambda fn, timeout, message: fn() or (_ for _ in ()).throw(SwitchError(message)))
    with pytest.raises(SwitchError, match='did not start'):
        ops.WindowsOps({}).start_gateway()


NO_GATEWAY = {'status': 'stopped', 'pid': None, 'born': None}


@pytest.fixture
def desktop_only(stock, monkeypatch):
    """A fresh stock install: Hermes Desktop is open, no gateway was ever set up."""
    monkeypatch.setattr(ops, 'service', lambda: NO_GATEWAY)
    monkeypatch.setattr(ops, 'gateway_states', lambda require_drain=False: pytest.fail('there is no gateway ledger to read'))
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {'selected': 'only'}, 'claude': {'selected': None, 'state': 'unset'}})
    monkeypatch.setattr(ops, 'plugin_profile_blockers', lambda: [])
    monkeypatch.setattr(ops, 'claude_provider_blockers', lambda: [])
    monkeypatch.setattr(ops, 'desktop', lambda: SimpleNamespace(pid=7))
    monkeypatch.setattr(ops, 'backends', lambda main: [])
    return stock


def test_stock_install_without_a_gateway_can_still_switch(desktop_only):
    assert ops.gateway_in_use() is False
    assert ops.preflight()['blockers'] == []


def test_a_backend_answers_for_itself_without_calling_itself(desktop_only, monkeypatch):
    # Seen on a real stock install: the preflight route asked its own backend over HTTP and waited on
    # itself for 8.6 s, every time, then reported the plugin as not responding.
    import os
    mine = {'pid': os.getpid(), 'profile': 'default', 'port': 1, 'token': 't'}
    other = {'pid': os.getpid() + 1, 'profile': 'second', 'port': 2, 'token': 't'}
    monkeypatch.setattr(ops, 'backends', lambda main: [mine, other])
    asked = []
    monkeypatch.setattr(ops, 'call', lambda b, route, body=None: asked.append(b['profile']) or {'blockers': ['busy elsewhere']})
    result = ops.preflight(lambda: {'blockers': ['busy here']})
    assert asked == ['second']
    assert result['blockers'] == ['default: busy here', 'second: busy elsewhere']


def test_a_declared_service_that_is_down_still_blocks(tmp_path, monkeypatch):
    write_settings(tmp_path, monkeypatch, 'FixtureGateway')
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped', 'pid': None})
    with ops.bound_settings():
        assert ops.gateway_in_use() is True


def test_desktop_only_switch_never_touches_a_gateway(desktop_only, recorded, monkeypatch):
    launched = []
    monkeypatch.setattr(ops, 'launch_desktop', lambda: launched.append(True))
    monkeypatch.setattr(ops.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('no gateway health check'))
    monkeypatch.setattr(ops, 'ensure_settings_unchanged', lambda: None)
    monkeypatch.setattr(ops, 'RECEIPT', desktop_only / 'receipt.json')
    worker = ops.WindowsOps({})
    worker.gateway = False
    worker.old_main = SimpleNamespace(pid=7)
    worker.freeze()
    worker.start()
    assert launched == [True] and recorded == []


def test_freeze_asks_every_backend_again_and_refuses_new_work(desktop_only, monkeypatch):
    """No admission hold of the plugin's own: the second answer from Hermes's idle proof, taken just
    before the close, is what stands between preflight and the window closing."""
    asked = []
    def call(backend, route, body=None):
        asked.append((backend['profile'], route, body))
        return {'blockers': ['A conversation is running, initializing, or waiting for input.']
                if backend['profile'] == 'second' else []}
    monkeypatch.setattr(ops, 'call', call)
    monkeypatch.setattr(ops, 'ensure_settings_unchanged', lambda: None)
    monkeypatch.setattr(ops, 'RECEIPT', desktop_only / 'receipt.json')
    monkeypatch.setattr(ops, 'backends', lambda main: [{'pid': 1, 'profile': 'default'}, {'pid': 2, 'profile': 'second'}])
    worker = ops.WindowsOps({})
    worker.gateway = False
    worker.old_main = SimpleNamespace(pid=7)
    worker.bs = [{'pid': 1, 'profile': 'default'}, {'pid': 2, 'profile': 'second'}]
    with pytest.raises(SwitchError, match='Desktop work started during preflight'):
        worker.freeze()
    assert asked == [('default', '/local-state', None), ('second', '/local-state', None)]
    worker.unfreeze()  # nothing to release: there is no hold


def test_a_gateway_appearing_mid_switch_stops_a_desktop_only_switch(desktop_only, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'running', 'pid': 40, 'born': 5.0})
    worker = ops.WindowsOps({})
    worker.gateway = False
    with pytest.raises(SwitchError, match='gateway started during the switch'):
        worker.stop_gateway()


def test_desktop_only_recovery_does_not_open_a_second_desktop(desktop_only, monkeypatch):
    monkeypatch.setattr(ops, 'launch_desktop', lambda: pytest.fail('Desktop is still open'))
    monkeypatch.setattr(ops, 'process_alive', lambda process: True)
    worker = ops.WindowsOps({})
    worker.gateway = False
    worker.old_main = SimpleNamespace(pid=7)
    worker.recover({'codex': 'only'}, changed=False)
    assert worker.record['recovery'] == 'not_needed'


def test_a_configured_service_still_uses_the_service_route(tmp_path, monkeypatch):
    write_settings(tmp_path, monkeypatch, 'FixtureGateway')
    commands = []
    monkeypatch.setattr(ops, 'command', lambda args, **kw: commands.append(args))
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped', 'pid': None})
    with ops.bound_settings():
        ops.WindowsOps({}).stop_gateway()
    assert commands[0][0] == 'powershell.exe' and 'Stop-Service -Name FixtureGateway' in commands[0][-1]


# A gateway run by a Windows service hides its command line from an ordinary process. Found by installing the
# plugin the way a user would on such a PC: settings written by setup said "no service", the command-line search
# saw no gateway, and a switch would have restarted Hermes Desktop alone.

def gateway_record(home, pid, state='running', age=0):
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    (home / 'gateway_state.json').write_text(json.dumps(
        {'pid': pid, 'gateway_state': state, 'active_agents': 0, 'updated_at': stamp}), encoding='utf-8')


@pytest.fixture
def hidden(stock, monkeypatch):
    """Settings name no service, and no gateway is visible by its command line."""
    monkeypatch.setattr(ops, 'ROOT', stock)
    processes(monkeypatch)
    monkeypatch.setattr(ops.psutil, 'pid_exists', lambda pid: True)
    monkeypatch.setattr(ops, 'gateway_owner', lambda pid: 'FixtureGateway' if pid == 500 else None)
    return stock


def test_a_gateway_run_by_a_service_blocks_and_names_the_service_and_the_command(hidden):
    gateway_record(hidden, 500)
    [blocker] = ops.gateway_setting_blockers()
    assert 'runs as the Windows service FixtureGateway' in blocker
    assert 'setup.cmd service FixtureGateway' in blocker


def test_a_hidden_gateway_nobody_owns_still_blocks(hidden):
    gateway_record(hidden, 501)
    [blocker] = ops.gateway_setting_blockers()
    assert 'cannot inspect' in blocker and 'refused' in blocker


def test_a_hidden_gateway_stops_the_preflight_before_anything_else(hidden, monkeypatch):
    gateway_record(hidden, 500)
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {'selected': 'only'}})
    monkeypatch.setattr(ops, 'plugin_profile_blockers', lambda: [])
    monkeypatch.setattr(ops, 'claude_provider_blockers', lambda: [])
    monkeypatch.setattr(ops, 'desktop', lambda: pytest.fail('nothing is inspected after a settings blocker'))
    result = ops.preflight()
    assert result['ok'] is False and 'FixtureGateway' in result['blockers'][0]


def test_a_gateway_the_search_can_see_is_not_blocked(hidden, monkeypatch):
    gateway_record(hidden, 500)
    processes(monkeypatch, (500, GATEWAY, 1.0))
    assert ops.gateway_setting_blockers() == []


@pytest.mark.parametrize('state, age, alive', [('running', 600, True), ('stopped', 0, True), ('running', 0, False)])
def test_an_old_record_a_stopped_gateway_or_a_dead_one_is_not_a_gateway(hidden, monkeypatch, state, age, alive):
    gateway_record(hidden, 500, state, age)
    monkeypatch.setattr(ops.psutil, 'pid_exists', lambda pid: alive)
    assert ops.gateway_setting_blockers() == []


def test_no_record_and_an_unreadable_record_are_not_a_gateway(hidden):
    assert ops.gateway_setting_blockers() == []
    (hidden / 'gateway_state.json').write_text('{not json', encoding='utf-8')
    assert ops.gateway_setting_blockers() == []


def test_a_declared_service_is_never_second_guessed(tmp_path, monkeypatch):
    write_settings(tmp_path, monkeypatch, 'FixtureGateway')
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    gateway_record(tmp_path, 500)
    monkeypatch.setattr(ops, 'stock_gateways', lambda: pytest.fail('a declared service is asked, not searched for'))
    with ops.bound_settings():
        assert ops.gateway_setting_blockers() == []


def test_the_owner_is_the_service_above_the_gateway_in_the_process_tree(monkeypatch):
    service_rows = [{'name': 'Unrelated', 'status': 'running', 'pid': 9}, {'name': 'Stopped', 'status': 'stopped', 'pid': None},
                    {'name': 'FixtureGateway', 'status': 'running', 'pid': 30}]
    monkeypatch.setattr(ops.psutil, 'win_service_iter', lambda: iter(
        [SimpleNamespace(as_dict=lambda row=row: row) for row in service_rows]), raising=False)
    parents = {500: 40, 40: 30, 30: 4, 4: None, 700: 4}

    def process(pid):
        return SimpleNamespace(pid=pid, parent=lambda: process(parents[pid]) if parents[pid] else None)

    monkeypatch.setattr(ops.psutil, 'Process', process)
    assert ops.gateway_owner(500) == 'FixtureGateway'
    assert ops.gateway_owner(700) is None


def test_an_owner_that_cannot_be_read_is_no_owner(monkeypatch):
    def refuse():
        raise OSError('access denied')
    monkeypatch.setattr(ops.psutil, 'win_service_iter', refuse, raising=False)
    assert ops.gateway_owner(500) is None
