"""Acceptance coordinator: real control flow, fixture-only system effects."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
spec=importlib.util.spec_from_file_location('approved_switch',Path(__file__).parents[1]/'complete_approved_switch.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

@pytest.mark.parametrize('terminal_state',['complete','failed'])
def test_submit_once_and_verify_or_report(tmp_path,monkeypatch,terminal_state):
    monkeypatch.setattr(runner,'RESULT',tmp_path/'result.json')
    monkeypatch.setattr(runner.ops,'RECEIPT',tmp_path/'receipt.json')
    monkeypatch.setattr(runner.ops,'LOCK',tmp_path/'lock')
    monkeypatch.setattr(runner.ops,'preflight',lambda:{'ok':True})
    monkeypatch.setattr(runner.ops,'desktop',lambda:SimpleNamespace(pid=30))
    monkeypatch.setattr(runner.ops,'service',lambda:{'status':'running','pid':40})
    monkeypatch.setattr(runner.ops,'accounts',lambda:{})
    monkeypatch.setattr(runner.ops,'selected',lambda _:'work')
    monkeypatch.setattr(runner.ops,'claude_selection',lambda:{'selected':'gmail','state':'set','stores':{}})
    monkeypatch.setattr(runner.ops,'_same_dir',lambda value,key:value=='fixture-dir')
    monkeypatch.setattr(runner.ops,'backends',lambda _:[{'profile':'default','pid':50}])
    posts=[]
    def call(backend,path,body=None):
        if path=='/switch':
            posts.append(body)
            runner.ops.RECEIPT.write_text(json.dumps({'operation_id':'fixture','state':terminal_state,'message':'fixture failure','new_desktop_pid':30,'new_gateway_pid':40}))
            return {'accepted':True,'operation_id':'fixture'}
        if path=='/status':return {'codex':{'selected':'work'},'claude':{'selected':'gmail'},'last_operation':{'state':terminal_state}}
        return {'selected':'work','live_pools':[{'selected':'work'}],'claude_config_dir':'fixture-dir'}
    monkeypatch.setattr(runner.ops,'call',call)
    runner.run({'codex':'work','claude':'gmail'})
    result=json.loads(runner.RESULT.read_text())
    assert posts==[{'codex':'work','claude':'gmail','confirmed':True}]
    assert result['state']==('verified' if terminal_state=='complete' else 'failed')
