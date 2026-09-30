"""Regression for a stopped Windows process whose cached flag stays True."""
from types import SimpleNamespace
import pytest
import windows_ops as ops

@pytest.mark.parametrize('exists,current_birth,expected',[(False,10,False),(True,10,True),(True,20,False)])
def test_liveness_requires_fresh_pid_and_birth(monkeypatch,exists,current_birth,expected):
    stale=SimpleNamespace(pid=123,create_time=lambda:10,is_running=lambda:True)
    monkeypatch.setattr(ops.psutil,'pid_exists',lambda _:exists)
    monkeypatch.setattr(ops.psutil,'Process',lambda _:SimpleNamespace(create_time=lambda:current_birth))
    assert ops.process_alive(stale) is expected
