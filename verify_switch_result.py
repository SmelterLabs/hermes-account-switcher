"""Read-only post-restart verification of the last complete_approved_switch.py run; prints a plain receipt."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent/'dashboard'))
import windows_ops as o
from switch_core import describe
path=o.ROOT/'logs/codex-switch-acceptance.json'
deadline=time.monotonic()+240
while time.monotonic()<deadline:
    result=json.loads(path.read_text())
    if result['state'] in ('verified','failed'):break
    time.sleep(2)
if result['state']!='verified':
    print('The switch has not passed verification. Recorded result: '+str(result.get('error',result['state'])))
    raise SystemExit(1)
request=result['target']
receipt=json.loads(o.RECEIPT.read_text())
assert receipt['state']=='complete' and receipt['operation_id']==result['operation_id']
assert o.service()['status']=='running' and o.service()['pid']==receipt['new_gateway_pid']
assert o.desktop().pid==receipt['new_desktop_pid'] and not o.LOCK.exists()
if request.get('codex'):assert o.selected(o.accounts())==request['codex']
if request.get('claude'):assert o.claude_selection()['selected']==request['claude']
for b in o.backends(o.desktop()):
    s=o.call(b,'/local-state')
    if request.get('codex'):assert s['selected']==request['codex'] and all(p['selected']==request['codex'] for p in s['live_pools'])
    if request.get('claude'):assert o._same_dir(s.get('claude_config_dir') or '',request['claude'])
print(f"{describe(request)} completed and independently verified: Desktop and the Hermes gateway restarted, every store and live backend reads the selected account, no pending switch lock."+(' Warnings: '+'; '.join(result['warnings']) if result.get('warnings') else ''))
