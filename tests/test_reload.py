"""Unit tests for the bounded Desktop-only activation helper."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest


spec = importlib.util.spec_from_file_location(
    'reload_backend', Path(__file__).parents[1] / 'reload_backend.py'
)
reload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reload)


def test_unmounted_profile_refuses_without_native_bypass(monkeypatch):
    backend = {'pid': 7, 'profile': 'candidate', 'port': 50007, 'token': 'not-recorded'}
    monkeypatch.setattr(reload.ops, 'call', lambda *_args, **_kwargs: (_ for _ in ()).throw(reload.ops.SwitchError('404')))
    with pytest.raises(reload.ops.SwitchError, match='idle check is unavailable in candidate'):
        reload.backend_snapshot(backend)


def test_missing_route_on_any_other_profile_is_not_silently_skipped(monkeypatch):
    backend = {'pid': 8, 'profile': 'default', 'port': 50008, 'token': 'not-recorded'}
    monkeypatch.setattr(reload.ops, 'call', lambda *_args, **_kwargs: (_ for _ in ()).throw(reload.ops.SwitchError('404')))

    with pytest.raises(reload.ops.SwitchError, match='idle check is unavailable in default'):
        reload.backend_snapshot(backend)


def test_backend_409_cannot_bypass_the_idle_check(monkeypatch):
    backend = {'pid': 10, 'profile': 'alpha', 'port': 50010, 'token': 'not-recorded'}
    monkeypatch.setattr(reload.ops, 'call', lambda *_args, **_kwargs: (_ for _ in ()).throw(reload.ops.SwitchError('409')))
    with pytest.raises(reload.ops.SwitchError, match='idle check is unavailable in alpha'):
        reload.backend_snapshot(backend)


def test_refused_freeze_clears_own_gateway_drain(monkeypatch, tmp_path):
    gateway = ModuleType('gateway')
    drain = ModuleType('gateway.drain_control')
    gateway.drain_control = drain
    monkeypatch.setitem(sys.modules, 'gateway', gateway)
    monkeypatch.setitem(sys.modules, 'gateway.drain_control', drain)

    home = tmp_path / 'hermes'
    marker = {}
    monkeypatch.setattr(reload.ops, 'gateway_states', lambda require_drain=False: [
        (home, {'active_agents': 0, 'gateway_state': 'draining' if require_drain else 'running'})])
    monkeypatch.setattr(reload.ops, 'wait_for', lambda predicate, *_args: predicate())
    monkeypatch.setattr(reload.ops, 'atomic', lambda *_args: None)
    monkeypatch.setattr(drain, 'drain_requested', lambda home: bool(marker), raising=False)
    monkeypatch.setattr(drain, 'read_drain_request', lambda home: marker or None, raising=False)
    monkeypatch.setattr(drain, 'write_drain_request', lambda home, principal, suppress_notification: marker.update(principal=principal), raising=False)
    monkeypatch.setattr(drain, 'clear_drain_request', lambda home: marker.clear(), raising=False)
    monkeypatch.setattr(reload.ops, 'call', lambda *_args, **_kwargs: {'blockers': ['active conversation']})

    with pytest.raises(reload.ops.SwitchError, match='Desktop work started'):
        reload.acquire_admission_boundary([{'profile': 'alpha'}], {})
    assert marker == {}


def test_loaded_backend_without_an_idle_check_stays_blocked(monkeypatch):
    backend = {'pid': 11, 'profile': 'alpha', 'port': 50011, 'token': 'not-recorded'}
    monkeypatch.setattr(reload.ops, 'call', lambda *_args, **_kwargs: {'activity_check': None, 'blockers': []})
    result = reload.backend_snapshot(backend)
    assert result['blockers'] == ["Hermes's idle check is unavailable in this backend."]


def test_loaded_backend_preserves_busy_blockers(monkeypatch):
    backend = {'pid': 9, 'profile': 'candidate', 'port': 50009, 'token': 'not-recorded'}
    monkeypatch.setattr(reload.ops, 'call', lambda *_args, **_kwargs: {
        'activity_check': 'hermes_cli.web_server_idle_proof.idle_proof', 'blockers': ['running turn']})
    result = reload.backend_snapshot(backend)
    assert result['blockers'] == ['running turn']
