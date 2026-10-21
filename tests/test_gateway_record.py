"""One reader for Hermes's gateway_state.json, and each caller's rule over it, state by state.

gateway_states (may a restart go ahead), gateway_setting_blockers (is a gateway hiding from the settings) and
service_owns_gateway (is the declared service the gateway now serving) used to parse the record three ways; the
dead-record fix in 1.3.2 reached only the first. Every state below is asked of all three."""
from datetime import datetime, timedelta, timezone
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops
from compat import read_gateway_record
from switch_core import SwitchError

ROOT_PID, PID = 100, 500
BORN = 1792000000.25                     # the live process now holding PID, in seconds since the epoch
STAMP = round(BORN * 100)                # what Hermes on Windows writes for it: centiseconds

# name: (start_time written, heartbeat age in seconds, PID exists)
STATES = {
    'fresh': (STAMP, 0, True),
    'stale_but_alive': (STAMP, 600, True),
    'dead': (STAMP, 600, False),
    # A different process that held PID earlier. Fresh, so only the start time can tell it apart.
    'recycled': (178780318776, 0, True),
    # The live gateway, stamped in a unit this check cannot compare: never proof that it is gone.
    'seconds_unit': (round(BORN), 0, True),
    'milliseconds_unit': (round(BORN * 1000), 0, True),
    'linux_clock_ticks': (123456, 0, True),
    'no_stamp': (None, 0, True),
    'cannot_inspect': ('access-denied', 0, True),
    'unreadable': ('unreadable', 0, True),
}


def write_record(home, start_time, age):
    path = home / 'gateway_state.json'
    if start_time == 'unreadable':
        path.write_text('{not json', encoding='utf-8')
        return
    record = {'pid': PID, 'gateway_state': 'running', 'active_agents': 0,
              'updated_at': (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()}
    if start_time is not None and start_time != 'access-denied':
        record['start_time'] = start_time
    path.write_text(json.dumps(record), encoding='utf-8')


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A root gateway that is plainly healthy, and a fake process table for PID."""
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    (tmp_path / 'gateway_state.json').write_text(json.dumps(
        {'pid': ROOT_PID, 'gateway_state': 'running', 'active_agents': 0,
         'updated_at': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
    alpha = tmp_path / 'profiles' / 'alpha'
    alpha.mkdir(parents=True)
    (alpha / 'config.yaml').write_text('{}', encoding='utf-8')

    def arrange(name, home):
        start_time, age, exists = STATES[name]
        write_record(home, start_time, age)
        monkeypatch.setattr(ops.psutil, 'pid_exists', lambda pid: pid == ROOT_PID or (pid == PID and exists))

        def process(pid):
            if pid == PID and not exists:
                raise ops.psutil.NoSuchProcess(pid)
            if pid == PID and start_time == 'access-denied':
                raise ops.psutil.AccessDenied(pid)
            return SimpleNamespace(create_time=lambda: BORN if pid == PID else 1.0)
        monkeypatch.setattr(ops.psutil, 'Process', process)
        monkeypatch.setattr(ops, 'gateway_owner', lambda pid: 'FixtureGateway' if pid == PID else None)
    return SimpleNamespace(root=tmp_path, alpha=alpha, arrange=arrange)


# gateway_states: a live gateway with an old heartbeat cannot prove it is idle, so the restart is refused.
# A gateway that is gone, or whose PID now belongs to another program, is left out.
GATEWAY_STATES = {
    'fresh': 'counted', 'stale_but_alive': 'refused', 'dead': 'ignored', 'recycled': 'ignored',
    'seconds_unit': 'counted', 'milliseconds_unit': 'counted', 'linux_clock_ticks': 'counted',
    'no_stamp': 'counted', 'cannot_inspect': 'counted', 'unreadable': 'refused',
}


@pytest.mark.parametrize('name', list(STATES))
def test_gateway_states(machine, name):
    machine.arrange(name, machine.alpha)
    expected = GATEWAY_STATES[name]
    if expected == 'refused':
        with pytest.raises(SwitchError, match='activity is unknown'):
            ops.gateway_states()
        return
    homes = [home for home, _ in ops.gateway_states()]
    assert homes == ([machine.root, machine.alpha] if expected == 'counted' else [machine.root])


# gateway_setting_blockers: only a live gateway with a fresh heartbeat is evidence of a hidden gateway. An
# unreadable record is not; a switch that needs the gateway refuses on it in gateway_states.
SETTING_BLOCKERS = {
    'fresh': True, 'stale_but_alive': False, 'dead': False, 'recycled': False,
    'seconds_unit': True, 'milliseconds_unit': True, 'linux_clock_ticks': True,
    'no_stamp': True, 'cannot_inspect': True, 'unreadable': False,
}


@pytest.mark.parametrize('name', list(STATES))
def test_gateway_setting_blockers(machine, monkeypatch, name):
    settings = {'hermes_root': str(machine.root), 'hermes_source': str(machine.root / 'source'),
                'desktop_exe': str(machine.root / 'Hermes.exe'), 'gateway_service': None,
                'codex': {'only': {'label': 'Only', 'email': 'only@example.invalid'}}, 'claude': {}}
    path = machine.root / 'settings.json'
    path.write_text(json.dumps(settings), encoding='utf-8')
    monkeypatch.setenv('HERMES_SWITCH_SETTINGS', str(path))
    # The root gateway is visible by its command line; the one under test hides it, as a service's does.
    root = SimpleNamespace(info={'pid': ROOT_PID, 'cmdline': ['C:/hermes/bin/hermes.exe', 'gateway', 'run'],
                                 'create_time': 1.0})
    monkeypatch.setattr(ops.psutil, 'process_iter', lambda attrs=None: iter([root]))
    machine.arrange(name, machine.alpha)
    with ops.bound_settings():
        blockers = ops.gateway_setting_blockers()
    assert bool(blockers) is SETTING_BLOCKERS[name], blockers
    if blockers:
        assert 'FixtureGateway' in blockers[0]


# service_owns_gateway asks who owns the gateway, not whether it is idle: an old heartbeat does not matter, a
# PID that now belongs to another program does.
SERVICE_OWNS = {
    'fresh': True, 'stale_but_alive': True, 'dead': False, 'recycled': False,
    'seconds_unit': True, 'milliseconds_unit': True, 'linux_clock_ticks': True,
    'no_stamp': True, 'cannot_inspect': True, 'unreadable': False,
}


@pytest.mark.parametrize('name', list(STATES))
def test_service_owns_gateway(machine, monkeypatch, name):
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'running', 'pid': 60, 'born': None})
    machine.arrange(name, machine.root)
    assert ops.service_owns_gateway('FixtureGateway') is SERVICE_OWNS[name]


def test_a_stamp_in_another_unit_never_proves_a_recycled_pid(monkeypatch):
    """Before: any positive int stamp was compared as centiseconds, so a live gateway stamped in seconds read as
    a different process and its record was dropped. Dropping a live gateway leaves it on the previous account."""
    monkeypatch.setattr(ops.psutil, 'Process', lambda pid: SimpleNamespace(create_time=lambda: BORN))
    assert ops.recycled_pid(PID, STAMP) is False
    assert ops.recycled_pid(PID, STAMP + 150) is False           # Hermes's own 2 s drift allowance
    assert ops.recycled_pid(PID, 178780318776) is True           # a different process, same unit
    for other_unit in (round(BORN), round(BORN * 1000), 123456, float(STAMP), True, None, '179200000025'):
        assert ops.recycled_pid(PID, other_unit) is False


def test_the_reader_tells_absent_stale_and_unreadable_apart(tmp_path):
    assert read_gateway_record(tmp_path) is None
    now = datetime.now(timezone.utc)
    for stamp, fresh in ((now.isoformat(), True), ((now - timedelta(seconds=121)).isoformat(), False),
                         ((now + timedelta(seconds=121)).isoformat(), False), (now.replace(tzinfo=None).isoformat(), False),
                         ('not a time', False), (None, False)):
        record = {'pid': '42', 'gateway_state': 'draining'}
        if stamp is not None:
            record['updated_at'] = stamp
        (tmp_path / 'gateway_state.json').write_text(json.dumps(record), encoding='utf-8')
        parsed = read_gateway_record(tmp_path)
        assert (parsed.pid, parsed.state, parsed.fresh) == (42, 'draining', fresh), stamp
    for broken in ('{not json', '[]', '{"pid": null}', '{"pid": 0}', '{"pid": true}', '{"pid": "x"}'):
        (tmp_path / 'gateway_state.json').write_text(broken, encoding='utf-8')
        with pytest.raises(ValueError):
            read_gateway_record(tmp_path)
