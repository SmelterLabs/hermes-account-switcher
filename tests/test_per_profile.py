"""Per-profile stores (upstream #111724: named profiles never read root's auth.json).
Hermetic: a temporary Hermes home, fixture tokens, and a recorded fake CLI."""
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops
from switch_core import SwitchError, identify, selected
from test_switch import entries, token


def write_store(home, rows):
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text('{}', encoding='utf-8')
    (home / 'auth.json').write_text(json.dumps({'credential_pool': {'openai-codex': rows}}), encoding='utf-8')


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    monkeypatch.setattr(ops, 'runtime_python', lambda: tmp_path / 'fixture-python.exe')
    write_store(tmp_path, entries())
    for name in ('beta', 'alpha'):
        write_store(tmp_path / 'profiles' / name, entries())
    # An xAI-only profile holds no Codex rows and must be skipped, not refused.
    write_store(tmp_path / 'profiles' / 'clyde-voice', [])
    with ops.bound_settings():
        yield tmp_path


def test_every_codex_profile_is_a_store_and_codexless_profiles_are_skipped(hermes_home):
    names = [home.name for home, _ in ops.codex_stores()]
    assert names == [hermes_home.name, 'alpha', 'beta']
    assert ops.common_selection(ops.codex_stores()) == 'personal'


def test_a_profile_hermes_marked_deleted_is_not_a_codex_store(hermes_home):
    write_store(hermes_home / 'profiles' / 'ghost', entries())
    (hermes_home / 'profiles' / '.deleted').mkdir()
    (hermes_home / 'profiles' / '.deleted' / 'ghost').write_text('deleted\n', encoding='utf-8')
    assert [home.name for home, _ in ops.codex_stores()] == [hermes_home.name, 'alpha', 'beta']


def test_summary_names_the_profile_whose_preferred_login_is_dead(hermes_home, monkeypatch):
    monkeypatch.setattr(ops, 'claude_selection', lambda: {'selected': None, 'state': 'unset'})
    monkeypatch.setattr(ops, 'claude_accounts', lambda: [])
    healthy = ops.selection_summary()['codex']
    assert healthy['effective'] == 'personal' and healthy['warnings'] == []
    rows = entries()
    rows[0]['last_status'] = 'dead'
    write_store(hermes_home / 'profiles' / 'alpha', rows)
    codex = ops.selection_summary()['codex']
    # The configured order still says Personal everywhere; only alpha is really paying Work.
    assert codex['selected'] == 'personal' and codex['effective'] is None
    assert codex['warnings'] == ['alpha: Personal login is dead, so Codex is billing Work. Sign Personal in again.']


def test_profiles_that_disagree_have_no_common_selection(hermes_home):
    rows = entries()
    rows[0]['priority'], rows[1]['priority'] = 1, 0
    write_store(hermes_home / 'profiles' / 'alpha', rows)
    assert ops.common_selection(ops.codex_stores()) is None


def test_a_profile_holding_a_different_account_is_refused(hermes_home):
    rows = entries()
    rows[1]['access_token'] = token('work@example.invalid', 'someone-else')
    write_store(hermes_home / 'profiles' / 'beta', rows)
    with pytest.raises(SwitchError, match='beta holds different Codex accounts'):
        ops.codex_stores()


def test_selection_is_lowest_priority_not_literal_zero():
    rows = entries()
    rows[0]['priority'], rows[1]['priority'] = 2, 3
    assert selected(identify(rows)) == 'personal'
    rows[0]['priority'] = 3
    assert selected(identify(rows)) is None  # a tie is ambiguous, never a guess


def test_apply_reorders_every_store(hermes_home, monkeypatch):
    calls = []

    def fake_command(args, timeout=60, home=None):
        calls.append((Path(home), args[-3:] if 'priority' in args else args[-2:]))
        if 'priority' in args:  # emulate the CLI: target to 0, the other renumbered
            path = Path(home) / 'auth.json'
            data = json.loads(path.read_text(encoding='utf-8'))
            for row in data['credential_pool']['openai-codex']:
                row['priority'] = 0 if row['id'] == args[-2] else 1
            path.write_text(json.dumps(data), encoding='utf-8')

    monkeypatch.setattr(ops, 'command', fake_command)
    monkeypatch.setattr(ops, 'service', lambda: {'status': 'stopped'})
    worker = ops.WindowsOps({})
    monkeypatch.setattr(worker, 'stage', lambda name: None)
    worker.apply({'codex': 'work', 'claude': None})
    homes = {home.name for home, _ in calls}
    assert homes == {hermes_home.name, 'beta', 'alpha'}
    assert 'clyde-voice' not in homes
    assert ops.common_selection(ops.codex_stores()) == 'work'


def test_stores_are_reordered_side_by_side_not_one_after_another(hermes_home, monkeypatch):
    # On the owner's PC ten profiles took about a minute one after another, with the gateway down throughout,
    # and Hermes Desktop started a gateway of its own in the gap (2026-10-21).
    import threading
    import time
    lock = threading.Lock()
    running = {'now': 0, 'most': 0}
    order = {}

    def slow_command(args, timeout=60, home=None):
        with lock:
            running['now'] += 1
            running['most'] = max(running['most'], running['now'])
            order.setdefault(Path(home).name, []).append('priority' if 'priority' in args else 'reset')
        time.sleep(0.2)
        with lock:
            running['now'] -= 1

    for n in range(7):
        write_store(hermes_home / 'profiles' / f'p{n}', cooled())
    monkeypatch.setattr(ops, 'command', slow_command)
    began = time.monotonic()
    reset = ops.reorder_codex_stores('work')
    elapsed = time.monotonic() - began
    assert len(order) == 10
    # Within a store the order is unchanged: priority first, then reset where the login carries a status.
    assert all(steps == ['priority', 'reset'] for name, steps in order.items() if name.startswith('p'))
    assert all(steps == ['priority'] for name, steps in order.items() if not name.startswith('p'))
    assert sorted(reset) == [f'p{n}' for n in range(7)]
    # Ten stores fit in one round.
    assert running['most'] == 10 <= ops.CODEX_PARALLEL
    assert elapsed < 2 * 0.2 * 2  # two commands deep, not ten


def cooled():
    rows = entries()
    rows[1]['last_status'] = 'exhausted'
    return rows


def test_reset_runs_only_where_the_target_login_has_a_status(hermes_home, monkeypatch):
    seen = []
    monkeypatch.setattr(ops, 'command', lambda args, timeout=60, home=None: seen.append((Path(home).name, args[4])))
    write_store(hermes_home / 'profiles' / 'alpha', cooled())
    assert ops.reorder_codex_stores('work') == ['alpha']
    assert sorted(seen) == sorted([(hermes_home.name, 'priority'), ('alpha', 'priority'), ('alpha', 'reset'),
                                   ('beta', 'priority')])
    for field in ops.STATUS_FIELDS:
        assert ops.needs_reset({field: 'x'}) and not ops.needs_reset({field: None})


def test_a_failed_store_fails_the_reorder_after_the_others_finish(hermes_home, monkeypatch):
    for home in (hermes_home, hermes_home / 'profiles' / 'alpha', hermes_home / 'profiles' / 'beta'):
        write_store(home, cooled())
    done = []

    def command(args, timeout=60, home=None):
        if Path(home).name == 'beta' and 'priority' in args:
            raise ops.LocalOperationFailed('A required local Hermes operation failed.')
        done.append((Path(home).name, 'priority' if 'priority' in args else 'reset'))

    monkeypatch.setattr(ops, 'command', command)
    with pytest.raises(ops.LocalOperationFailed):
        ops.reorder_codex_stores('work')
    # beta stopped before its reset; the others were not abandoned half-way.
    assert ('beta', 'reset') not in done
    assert (hermes_home.name, 'reset') in done and ('alpha', 'reset') in done
