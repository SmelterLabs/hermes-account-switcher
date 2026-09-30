"""Admission tests use fake sessions and fake delegates, never a live Hermes backend."""
import sys
import threading
import time
import types
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
from desktop_gate import DesktopGate


@pytest.fixture
def server(monkeypatch):
    lifecycle = types.ModuleType('tui_gateway.session_lifecycle')
    lifecycle._session_has_active_delegations = lambda sid, session: session.get('delegating', False)
    monkeypatch.setitem(sys.modules, 'tui_gateway.session_lifecycle', lifecycle)
    return types.SimpleNamespace(_sessions={}, _sessions_lock=threading.RLock(),
        _session_pending_kind=lambda sid: '', handle_request=lambda req: {'result': 'ok'},
        _err=lambda rid, code, msg: {'error': {'code': code, 'message': msg}})


def test_freeze_blocks_new_requests_and_release_restores(server):
    gate = DesktopGate(server)
    assert gate.freeze()['ok']
    assert gate.dispatch({'id': 1})['error']['code'] == 5038
    gate.release()
    assert gate.dispatch({}) == {'result': 'ok'}


@pytest.mark.parametrize('session', [{'running': True}, {'delegating': True}, {'_run_thread': threading.current_thread()}])
def test_freeze_refuses_busy_sessions(server, session):
    server._sessions['fixture'] = session
    gate = DesktopGate(server)
    assert not gate.freeze()['ok']
    assert gate.dispatch({}) == {'result': 'ok'}


def test_freeze_refuses_an_inflight_request_atomically(server):
    started = threading.Event(); finish = threading.Event()
    def handler(req): started.set(); finish.wait(2); return {'result': 'ok'}
    server.handle_request = handler
    gate = DesktopGate(server)
    t = threading.Thread(target=lambda: gate.dispatch({}))
    t.start(); assert started.wait(1)
    assert not gate.freeze()['ok']
    finish.set(); t.join(2)
    assert gate.freeze()['ok']


@pytest.fixture
def split_server(server):
    """Current Hermes: long-running methods skip handle_request and enter through
    _handle_admitted_request on a worker thread (tui_gateway/rpc_dispatch.py)."""
    ran = []
    server._handle_admitted_request = lambda req: ran.append(req['method']) or {'result': 'ok'}
    server.handle_request = lambda req: server._handle_admitted_request(req)
    server.dispatch = lambda req: (server._handle_admitted_request(req) if req['method'] == 'shell.exec'
                                   else server.handle_request(req))
    server.ran = ran
    return server


def test_freeze_blocks_a_new_long_running_request(split_server, monkeypatch):
    import desktop_gate
    monkeypatch.setattr(desktop_gate, 'gateway_server', lambda: split_server)
    gate = desktop_gate.install()
    assert gate.installed()
    assert gate.freeze()['ok']
    for method in ('prompt.submit', 'shell.exec'):
        assert split_server.dispatch({'id': 1, 'method': method})['error']['code'] == 5038
    assert split_server.ran == []
    gate.release()
    assert split_server.dispatch({'id': 2, 'method': 'shell.exec'}) == {'result': 'ok'}
    assert split_server.ran == ['shell.exec']


def test_an_inflight_long_running_request_refuses_the_freeze(split_server, monkeypatch):
    import desktop_gate
    started = threading.Event(); finish = threading.Event()
    split_server._handle_admitted_request = lambda req: (started.set(), finish.wait(2), {'result': 'ok'})[2]
    monkeypatch.setattr(desktop_gate, 'gateway_server', lambda: split_server)
    gate = desktop_gate.install()
    t = threading.Thread(target=lambda: split_server.dispatch({'id': 1, 'method': 'shell.exec'}))
    t.start(); assert started.wait(1)
    assert not gate.freeze()['ok']
    finish.set(); t.join(2)
    assert gate.freeze()['ok']


def test_guard_expires_instead_of_stranding_desktop(server):
    gate = DesktopGate(server); gate.freeze(); gate.until = time.monotonic() - 1
    assert gate.dispatch({}) == {'result': 'ok'}


def test_failed_request_releases_inflight_count(server):
    def fail(req): raise ValueError('fixture')
    server.handle_request = fail
    gate = DesktopGate(server)
    with pytest.raises(ValueError): gate.dispatch({})
    assert gate.inflight == 0


def test_a_refusal_names_the_request_that_is_running_and_for_how_long(server, monkeypatch):
    # Seen on the author's PC: "Desktop is handling a request." with no way to tell which one, on every try.
    started = threading.Event(); finish = threading.Event()
    server.handle_request = lambda req: (started.set(), finish.wait(2), {'result': 'ok'})[2]
    gate = DesktopGate(server)
    t = threading.Thread(target=lambda: gate.dispatch({'id': 1, 'method': 'voice.record'}))
    t.start(); assert started.wait(1)
    [reason] = gate.freeze()['blockers']
    assert reason.startswith('Desktop is handling a request (voice.record for ') and reason.endswith(' s).')
    finish.set(); t.join(2)
    assert gate.freeze()['ok'] and gate.running == {}


def test_a_read_only_housekeeping_request_never_blocks_a_switch(server):
    # Seen on the author's PC: Desktop re-reads the project tree all the time (seconds each on OneDrive),
    # so "projects.tree for 7 s" refused every switch.
    started = threading.Event(); finish = threading.Event()
    server.handle_request = lambda req: (started.set(), finish.wait(2), {'result': 'ok'})[2]
    gate = DesktopGate(server)
    t = threading.Thread(target=lambda: gate.dispatch({'id': 1, 'method': 'projects.tree'}))
    t.start(); assert started.wait(1)
    assert gate.freeze()['ok']
    gate.release()
    finish.set(); t.join(2)


@pytest.mark.parametrize('method', ['shell.exec', 'slash.exec', 'llm.oneshot', 'plugins.manage', 'projectsx.tree', 'session.compress'])
def test_a_request_that_can_start_work_blocks_a_switch(server, method):
    started = threading.Event(); finish = threading.Event()
    server.handle_request = lambda req: (started.set(), finish.wait(2), {'result': 'ok'})[2]
    gate = DesktopGate(server)
    t = threading.Thread(target=lambda: gate.dispatch({'id': 1, 'method': method}))
    t.start(); assert started.wait(1)
    [reason] = gate.freeze()['blockers']
    assert method in reason
    finish.set(); t.join(2)


def test_a_request_without_a_method_still_counts_and_is_released(server):
    def fail(req): raise ValueError('fixture')
    server.handle_request = fail
    gate = DesktopGate(server)
    with pytest.raises(ValueError): gate.dispatch(None)
    assert gate.inflight == 0 and gate.running == {}
