"""Switching without a restart (unified gateway runtime). Hermetic: temp homes, recorded commands, fake ledgers.

Why it is safe without a restart (checked against upstream Hermes and the Claude provider 0.3.3):
- Codex: the gateway reloads the login pool from auth.json at the start of every turn and rebuilds a chat's
  cached agent when the login changes, so a new priority order applies from the next turn.
- Claude: provider 0.3.1 on reads its per-profile ``claude_config_dir`` setting from config.yaml on every
  spawn, ahead of the ``.env`` variable, which is only read when the gateway starts.
The drain marker still holds new turns while the stores change, so no turn can write a stale order back."""
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops
from switch_core import SwitchError, switch_transaction

RUNNING = {'status': 'running', 'pid': 50, 'born': None}


def write_yaml(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')  # JSON is valid YAML


def read_yaml(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def set_nested(data, dotted, value):
    parts = dotted.split('.')
    # The provider's folder name holds dots-free hyphens only; split is exact here.
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    monkeypatch.setattr(ops, 'CLAUDE_AUTH', tmp_path / 'claude-auth')
    monkeypatch.setattr(ops, 'runtime_python', lambda: tmp_path / 'fixture-python.exe')
    monkeypatch.setattr(ops, 'ensure_settings_unchanged', lambda: None)
    write_yaml(tmp_path / 'config.yaml', {})
    for name in ('alpha', 'beta'):
        (tmp_path / 'profiles' / name).mkdir(parents=True)
        write_yaml(tmp_path / 'profiles' / name / 'config.yaml', {})
    commands = []

    # Production runs Hermes's own writers in its venv; emulate their file edits here.
    def fake_command(args, timeout=60, home=None):
        commands.append((Path(home).name, args[1:]))
        if args[1] == str(ops.ENV_SET):
            path = Path(home) / '.env'
            lines = [l for l in (path.read_text(encoding='utf-8').splitlines() if path.exists() else [])
                     if not l.startswith(args[2] + '=')]
            if args[3] != '--remove':
                lines.append(f'{args[2]}={args[3]}')
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        elif args[1:5] == ['-m', 'hermes_cli.main', 'config', 'set']:
            config = read_yaml(Path(home) / 'config.yaml')
            set_nested(config, args[5], args[6])
            write_yaml(Path(home) / 'config.yaml', config)
        elif args[1:4] == ['-m', 'hermes_cli.main', 'auth']:
            pass
        else:
            pytest.fail(f'unexpected command {args}')
    monkeypatch.setattr(ops, 'command', fake_command)
    with ops.bound_settings():
        monkeypatch.setattr(ops, 'RECEIPT', tmp_path / 'receipt.json')
        yield tmp_path, commands


def provider(root, version):
    folder = root / 'plugins' / ops.CLAUDE_PROVIDER
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'plugin.yaml').write_text(f'name: {ops.CLAUDE_PROVIDER}\nversion: {version}\n', encoding='utf-8')


# ---------------------------------------------------------------- Claude: the per-profile setting

def test_apply_writes_the_providers_setting_and_the_env_variable_in_every_profile(home):
    root, commands = home
    ops.apply_claude('gmail')
    for config in (root / 'config.yaml', root / 'profiles/alpha/config.yaml', root / 'profiles/beta/config.yaml'):
        value = read_yaml(config)['plugins']['entries'][ops.CLAUDE_PROVIDER]['settings']['claude_config_dir']
        assert Path(value).resolve() == ops.claude_dir('gmail').resolve()
    assert Path(ops.env_values(root)[ops.CLAUDE_VAR]).resolve() == ops.claude_dir('gmail').resolve()
    assert ops.claude_selection()['selected'] == 'gmail'


def test_the_setting_decides_the_selection_over_the_env_variable(home):
    """The provider reads the setting first. A setting someone filled in by hand used to make a .env-only
    switch silently do nothing while the dialog said it worked."""
    root, _ = home
    ops.apply_claude('gmail')
    config = read_yaml(root / 'profiles/alpha/config.yaml')
    set_nested(config, ops.CLAUDE_SETTING, ops.claude_value('work'))
    write_yaml(root / 'profiles/alpha/config.yaml', config)
    state = ops.claude_selection()
    assert state['state'] == 'mixed' and state['stores']['alpha'] == 'work'


def test_an_empty_setting_falls_through_to_the_env_variable(home):
    root, _ = home
    ops.apply_claude('gmail')
    for config_path in (root / 'config.yaml', root / 'profiles/alpha/config.yaml', root / 'profiles/beta/config.yaml'):
        config = read_yaml(config_path)
        set_nested(config, ops.CLAUDE_SETTING, '  ')
        write_yaml(config_path, config)
    assert ops.claude_selection()['selected'] == 'gmail'


def test_unset_clears_the_setting_and_the_variable(home):
    root, _ = home
    ops.apply_claude('work')
    ops.apply_claude('unset')
    assert ops.claude_selection()['state'] == 'unset'
    assert read_yaml(root / 'config.yaml')['plugins']['entries'][ops.CLAUDE_PROVIDER]['settings']['claude_config_dir'] == ''


def test_an_unreadable_profile_config_is_not_guessed(home):
    root, _ = home
    ops.apply_claude('gmail')
    (root / 'profiles/beta/config.yaml').write_text('plugins: [unclosed', encoding='utf-8')
    assert ops.claude_selection()['state'] == 'mixed'


# ---------------------------------------------------------------- when a switch can skip the restart

def test_provider_version_is_read_from_its_manifest(home):
    root, _ = home
    assert ops.claude_provider_version() is None
    provider(root, '0.3.3')
    assert ops.claude_provider_version() == (0, 3, 3)
    provider(root, '"0.3.1-rc1"')
    assert ops.claude_provider_version() == (0, 3, 1)


def test_live_needs_the_unified_runtime_and_a_provider_that_reads_its_setting_per_spawn(home, monkeypatch):
    root, _ = home
    monkeypatch.setattr(ops, 'gateway_hosts_desktop', lambda: True)
    provider(root, '0.3.0')
    assert ops.live_switch_possible({'codex': 'work', 'claude': None}) is True
    assert ops.live_switch_possible({'codex': None, 'claude': 'gmail'}) is False
    provider(root, '0.3.1')
    assert ops.live_switch_possible({'codex': 'work', 'claude': 'gmail'}) is True
    monkeypatch.setattr(ops, 'gateway_hosts_desktop', lambda: False)
    assert ops.live_switch_possible({'codex': 'work', 'claude': None}) is False


# ---------------------------------------------------------------- the live transaction

def held_ledger(root, monkeypatch, active=0, state='draining'):
    def states(require_drain=False):
        if require_drain and state != 'draining':
            raise SwitchError('Gateway admission has not finished closing yet.')
        return [(root, {'pid': 50, 'gateway_state': state, 'active_agents': active})]
    monkeypatch.setattr(ops, 'gateway_states', states)


def live_worker(root, monkeypatch):
    monkeypatch.setattr(ops, 'service', lambda: RUNNING)
    worker = ops.WindowsOps({'started_at': 0})
    worker.hosted = worker.gateway = worker.live = True
    worker.old_gateway = ops.gateway_identity(RUNNING)
    worker.drained = [root]
    return worker


def test_live_stop_and_start_never_touch_the_gateway(home, monkeypatch):
    root, commands = home
    held_ledger(root, monkeypatch)
    cleared = []
    monkeypatch.setattr(ops, 'drain_control', lambda: type('D', (), {
        'read_drain_request': staticmethod(lambda home: {'principal': 'account-switcher'}),
        'clear_drain_request': staticmethod(lambda home: cleared.append(home))}))
    worker = live_worker(root, monkeypatch)
    worker.stop()
    assert commands == [] and not worker.stopped
    assert [step['step'] for step in worker.record['checkpoints']] == ['gateway_held']
    worker.start()
    assert commands == [] and cleared == [root] and worker.drained == []


def test_live_apply_refuses_while_a_turn_is_running(home, monkeypatch):
    root, commands = home
    held_ledger(root, monkeypatch, active=1)
    worker = live_worker(root, monkeypatch)
    with pytest.raises(SwitchError, match='Nothing was switched'):
        worker.apply({'codex': None, 'claude': 'gmail'})
    assert commands == []


def test_live_apply_refuses_when_the_hold_was_released(home, monkeypatch):
    root, commands = home
    held_ledger(root, monkeypatch, state='running')
    worker = live_worker(root, monkeypatch)
    with pytest.raises(SwitchError):
        worker.apply({'codex': None, 'claude': 'gmail'})
    assert commands == []


def test_live_apply_changes_the_stores_with_the_gateway_running(home, monkeypatch):
    root, _ = home
    held_ledger(root, monkeypatch)
    worker = live_worker(root, monkeypatch)
    worker.apply({'codex': None, 'claude': 'gmail'})
    assert ops.claude_selection()['selected'] == 'gmail'


def test_live_verify_needs_no_new_gateway(home, monkeypatch):
    root, _ = home
    worker = live_worker(root, monkeypatch)
    monkeypatch.setattr(ops, 'service_owns_gateway', lambda name: True)
    monkeypatch.setattr(ops, 'codex_stores', lambda: [])
    monkeypatch.setattr(ops, 'common_selection', lambda stores: 'work')
    monkeypatch.setattr(ops, 'usage', lambda account: None)
    monkeypatch.setattr(ops, 'accounts', lambda: {'work': {}})
    result = worker.verify({'codex': 'work', 'claude': None})
    assert result['codex'] == 'work' and result['restarted'] is False and result['gateway_pid'] == 50
    monkeypatch.setattr(ops, 'common_selection', lambda stores: 'personal')
    with pytest.raises(SwitchError, match='does not try the selected Codex account first'):
        worker.verify({'codex': 'work', 'claude': None})


def test_live_recovery_restores_the_previous_choice_while_turns_are_still_held(home, monkeypatch):
    root, _ = home
    held_ledger(root, monkeypatch)
    ops.apply_claude('gmail')
    worker = live_worker(root, monkeypatch)
    monkeypatch.setattr(worker, 'start', lambda: pytest.fail('a running gateway is never started'))
    ops.apply_claude('work')
    worker.recover({'claude': 'gmail'}, changed=True)
    assert worker.record['recovery'] == 'previous_preference_restored'
    assert ops.claude_selection()['selected'] == 'gmail'


def test_live_recovery_keeps_the_new_choice_once_turns_were_released(home, monkeypatch):
    root, _ = home
    worker = live_worker(root, monkeypatch)
    worker.drained = []
    worker.recover({'claude': 'gmail'}, changed=True)
    assert worker.record['recovery'].startswith('new_preference_retained')


def test_the_transaction_runs_the_live_steps_in_the_same_order(home, monkeypatch):
    """switch_transaction is unchanged: stop/start are what the live mode reinterprets."""
    root, commands = home
    steps = []

    class Ops:
        def preflight(self, request): steps.append('preflight'); return {'previous': {'claude': 'unset'}}
        def freeze(self): steps.append('freeze')
        def stop(self): steps.append('stop')
        def apply(self, request): steps.append('apply')
        def start(self): steps.append('start')
        def verify(self, request): steps.append('verify'); return {}
        def recover(self, previous, changed): steps.append('recover')
        def unfreeze(self): steps.append('unfreeze')
    switch_transaction(Ops(), {'codex': None, 'claude': 'gmail'})
    assert steps == ['preflight', 'freeze', 'stop', 'apply', 'start', 'verify', 'unfreeze']


def test_worker_preflight_picks_live_and_records_it(home, monkeypatch):
    root, _ = home
    provider(root, '0.3.3')
    monkeypatch.setattr(ops, 'gateway_hosts_desktop', lambda: True)
    monkeypatch.setattr(ops, 'require_switch_interfaces', lambda: None)
    monkeypatch.setattr(ops, 'service', lambda: RUNNING)
    monkeypatch.setattr(ops, 'preflight', lambda own_state=None: {
        'ok': True, 'blockers': [], 'codex': {'selected': 'personal'}, 'claude': {'selected': None, 'state': 'unset'}})
    monkeypatch.setattr(ops, 'claude_identity', lambda key: {'email': 'x', 'method': 'token'})
    record = {}
    worker = ops.WindowsOps(record)
    worker.preflight({'codex': None, 'claude': 'gmail'})
    assert worker.live is True and record['restarts'] is False
    provider(root, '0.3.0')
    record = {}
    worker = ops.WindowsOps(record)
    worker.preflight({'codex': None, 'claude': 'gmail'})
    assert worker.live is False and record['restarts'] is True


def test_dialog_preflight_says_whether_anything_restarts(home, monkeypatch):
    root, _ = home
    provider(root, '0.3.3')
    monkeypatch.setattr(ops, 'gateway_hosts_desktop', lambda: True)
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {'selected': 'personal'}, 'claude': {'selected': None, 'state': 'unset'}})
    monkeypatch.setattr(ops, 'plugin_profile_blockers', lambda: [])
    monkeypatch.setattr(ops, 'claude_provider_blockers', lambda: [])
    monkeypatch.setattr(ops, 'gateway_setting_blockers', lambda: [])
    monkeypatch.setattr(ops, 'service', lambda: RUNNING)
    monkeypatch.setattr(ops, 'service_owns_gateway', lambda name: True)
    held_ledger(root, monkeypatch, state='running')
    assert ops.preflight(lambda: {'blockers': []})['restarts'] is False
    provider(root, '0.3.0')
    assert ops.preflight(lambda: {'blockers': []})['restarts'] is True


def test_an_abandoned_live_switch_does_not_claim_hermes_was_closed(home, monkeypatch):
    root, _ = home
    monkeypatch.setattr(ops, 'LOCK', root / 'lock')
    ops.LOCK.write_text(json.dumps({'operation_id': 'op', 'pid': 999999}), encoding='utf-8')
    ops.atomic(ops.RECEIPT, {'operation_id': 'op', 'state': 'applying', 'restarts': False})
    monkeypatch.setattr(ops.time, 'time', lambda: 10**10)
    monkeypatch.setattr(ops.psutil, 'pid_exists', lambda pid: False)
    monkeypatch.setattr(ops.psutil, 'process_iter', lambda attrs: [])
    monkeypatch.setattr(ops, 'clear_own_drains', lambda: True)
    ops.recover_abandoned_check()
    record = json.loads(ops.RECEIPT.read_text())
    assert record['state'] == 'failed' and 'closed' not in record['message']
    assert 'Hermes was not restarted' in record['message']


# ---------------------------------------------------------------- running work: block only what must wait

def dialog_preflight(root, monkeypatch, version, own=(), active=0):
    provider(root, version)
    monkeypatch.setattr(ops, 'gateway_hosts_desktop', lambda: True)
    monkeypatch.setattr(ops, 'selection_summary', lambda: {'codex': {'selected': 'personal'}, 'claude': {'selected': None, 'state': 'unset'}})
    for name in ('plugin_profile_blockers', 'claude_provider_blockers', 'gateway_setting_blockers', 'stale_desktop_blockers'):
        monkeypatch.setattr(ops, name, lambda: [])
    monkeypatch.setattr(ops, 'service', lambda: RUNNING)
    monkeypatch.setattr(ops, 'service_owns_gateway', lambda name: True)
    held_ledger(root, monkeypatch, active=active, state='running')
    return ops.preflight(lambda: {'blockers': list(own)})


def test_running_work_blocks_neither_live_part(home, monkeypatch):
    root, _ = home
    result = dialog_preflight(root, monkeypatch, '0.3.3', own=['A conversation is running, initializing, or waiting for input.'], active=1)
    assert result['ok'] is True and result['blockers'] == []
    assert result['blockers_codex'] == [] and result['blockers_claude'] == []
    assert len(result['busy']) == 2 and result['restarts_for'] == {'codex': False, 'claude': False}


def test_a_prompt_waiting_on_the_person_blocks_only_codex(home, monkeypatch):
    root, _ = home
    result = dialog_preflight(root, monkeypatch, '0.3.3', own=['A conversation is waiting for input.'])
    assert result['ok'] is True
    assert result['blockers_codex'] == ['A conversation is waiting for input.'] and result['blockers_claude'] == []


def test_a_claude_switch_that_restarts_still_waits_for_all_work(home, monkeypatch):
    root, _ = home
    result = dialog_preflight(root, monkeypatch, '0.3.0', active=1)
    assert result['restarts_for'] == {'codex': False, 'claude': True}
    assert result['blockers_claude'] == result['busy'] and result['busy'] and result['blockers_codex'] == []


def worker_for(root, monkeypatch, version, request, busy=()):
    provider(root, version)
    monkeypatch.setattr(ops, 'gateway_hosts_desktop', lambda: True)
    monkeypatch.setattr(ops, 'require_switch_interfaces', lambda: None)
    monkeypatch.setattr(ops, 'service', lambda: RUNNING)
    monkeypatch.setattr(ops, 'preflight', lambda own_state=None: {
        'ok': True, 'blockers': [], 'busy': list(busy),
        'codex': {'selected': 'personal'}, 'claude': {'selected': None, 'state': 'unset'}})
    monkeypatch.setattr(ops, 'claude_identity', lambda key: {'email': 'x', 'method': 'token'})
    monkeypatch.setattr(ops, 'accounts', lambda: {'work': {'expired': False}})
    monkeypatch.setattr(ops, 'usage', lambda item: None)
    worker = ops.WindowsOps({})
    worker.preflight(request)
    return worker


def test_a_claude_only_live_switch_holds_nothing_and_runs_beside_a_reply(home, monkeypatch):
    root, commands = home
    worker = worker_for(root, monkeypatch, '0.3.3', {'codex': None, 'claude': 'gmail'}, busy=['default: gateway or scheduled work is active.'])
    assert worker.live and not worker.hold and worker.record['holds_turns'] is False
    held_ledger(root, monkeypatch, active=1, state='running')
    monkeypatch.setattr(ops, 'drain_control', lambda: pytest.fail('a Claude-only live switch writes no drain marker'))
    worker.freeze()
    worker.stop()
    worker.apply({'codex': None, 'claude': 'gmail'})
    worker.start()
    worker.unfreeze()
    assert ops.claude_selection()['selected'] == 'gmail'


def test_a_switch_that_restarts_still_refuses_while_work_runs(home, monkeypatch):
    root, _ = home
    with pytest.raises(SwitchError, match='gateway or scheduled work is active'):
        worker_for(root, monkeypatch, '0.3.0', {'codex': None, 'claude': 'gmail'}, busy=['default: gateway or scheduled work is active.'])


def test_a_live_codex_switch_holds_new_messages_and_waits_for_running_replies(home, monkeypatch):
    root, _ = home
    worker = worker_for(root, monkeypatch, '0.3.3', {'codex': 'work', 'claude': None}, busy=['default: gateway or scheduled work is active.'])
    assert worker.live and worker.hold
    written = []
    monkeypatch.setattr(ops, 'drain_control', lambda: type('D', (), {
        'drain_requested': staticmethod(lambda home: False),
        'write_drain_request': staticmethod(lambda home, principal, suppress_notification: written.append(home))}))
    counts = iter([1, 1, 1, 0])
    current = {'active': 1}

    def states(require_drain=False):
        if require_drain:
            current['active'] = next(counts, 0)
        return [(root, {'pid': 50, 'gateway_state': 'draining', 'active_agents': current['active']})]
    monkeypatch.setattr(ops, 'gateway_states', states)
    monkeypatch.setattr(ops.time, 'sleep', lambda seconds: None)
    worker.freeze()
    assert written == [root] and 'waiting_for_replies' in [c['step'] for c in worker.record['checkpoints']]


def test_a_live_codex_switch_gives_up_after_the_wait(home, monkeypatch):
    root, _ = home
    worker = worker_for(root, monkeypatch, '0.3.3', {'codex': 'work', 'claude': None})
    monkeypatch.setattr(ops, 'drain_control', lambda: type('D', (), {
        'drain_requested': staticmethod(lambda home: False),
        'write_drain_request': staticmethod(lambda home, principal, suppress_notification: None)}))
    held_ledger(root, monkeypatch, active=1)
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(ops.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(ops.time, 'sleep', lambda seconds: None)
    with pytest.raises(SwitchError, match='still answering after a minute'):
        worker.freeze()


def test_a_claude_only_live_switch_restores_the_previous_choice_without_a_hold(home, monkeypatch):
    root, _ = home
    ops.apply_claude('gmail')
    worker = live_worker(root, monkeypatch)
    worker.hold = False
    worker.drained = []
    ops.apply_claude('work')
    worker.recover({'claude': 'gmail'}, changed=True)
    assert worker.record['recovery'] == 'previous_preference_restored'
    assert ops.claude_selection()['selected'] == 'gmail'
