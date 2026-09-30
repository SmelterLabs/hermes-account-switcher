"""Execute ONE explicitly approved switch after active work finishes, then verify it independently.

Usage: python complete_approved_switch.py --codex work [--claude gmail]   (or --check for a read-only preflight)
Never run without switch/restart approval; this is a one-shot, not a scheduled job."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent/'dashboard'))
import windows_ops as ops
from switch_core import normalize_request

RESULT=ops.ROOT/'logs/codex-switch-acceptance.json'

def run(request):
    result={'state':'waiting_for_idle','target':request,'started_at':time.time()}
    ops.atomic(RESULT,result)
    try:
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            if ops.LOCK.exists():raise ops.SwitchError('Another switch owns the lock; no second switch submitted.')
            check=ops.preflight()
            if check['ok']:break
            result['blockers']=check['blockers'];ops.atomic(RESULT,result)
            time.sleep(2)
        else:raise ops.SwitchError('Active work did not clear; no switch submitted.')
        main=ops.desktop()
        backend=ops.backends(main)[0]
        body={name:value for name,value in request.items() if value}
        accepted=ops.call(backend,'/switch',{**body,'confirmed':True})
        if not accepted.get('accepted'):raise ops.SwitchError('Switch was not accepted.')
        result.update(state='submitted',operation_id=accepted['operation_id'])
        ops.atomic(RESULT,result)
        deadline=time.monotonic()+300
        while time.monotonic()<deadline:
            receipt=json.loads(ops.RECEIPT.read_text())
            if receipt['operation_id']!=accepted['operation_id']:raise ops.SwitchError('Operation changed; refusing to infer success.')
            if receipt['state'] in ('complete','failed'):break
            time.sleep(2)
        else:raise ops.SwitchError('Switch did not reach a terminal receipt within five minutes.')
        if receipt['state']!='complete':raise ops.SwitchError(receipt['message'])
        main=ops.desktop()
        assert main.pid==receipt['new_desktop_pid']
        assert ops.service()['status']=='running'
        assert ops.service()['pid']==receipt['new_gateway_pid']
        assert not ops.LOCK.exists()
        if request.get('codex'):assert ops.selected(ops.accounts())==request['codex']
        if request.get('claude'):assert ops.claude_selection()['selected']==request['claude']
        states=[]
        for b in ops.backends(main):
            status=ops.call(b,'/status')
            local=ops.call(b,'/local-state')
            assert status['last_operation']['state']=='complete'
            if request.get('codex'):
                assert status['codex']['selected']==request['codex'] and local['selected']==request['codex']
                assert all(p['selected']==request['codex'] for p in local['live_pools'])
            if request.get('claude'):
                assert status['claude']['selected']==request['claude']
                assert ops._same_dir(local.get('claude_config_dir') or '',request['claude'])
            states.append({'profile':b['profile'],'pid':b['pid'],'selected':local['selected'],'live_pools':local['live_pools'],'claude_config_dir':local.get('claude_config_dir')})
        assert states
        result.update(state='verified',new_desktop_pid=main.pid,new_gateway_pid=ops.service()['pid'],profiles=states,warnings=receipt.get('warnings',[]))
    except Exception as exc:
        result.update(state='failed',error=str(exc) if isinstance(exc,ops.SwitchError) else type(exc).__name__)
    finally:
        result['finished_at']=time.time();ops.atomic(RESULT,result)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--codex');parser.add_argument('--claude');parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if args.check:
        print(json.dumps({'preflight':ops.preflight(),'result_path':str(RESULT)}))
    else:
        run(normalize_request({'codex':args.codex,'claude':args.claude,'confirmed':True}))
