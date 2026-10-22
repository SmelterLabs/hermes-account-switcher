"""The in-process writer: Hermes's own write functions for every profile in one Hermes Python, and the fallback
to the per-profile commands. Hermetic: fake Hermes modules and a fake helper process."""
import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import hermes_writer
import windows_ops as ops


# ---------------------------------------------------------------- the helper, against fake Hermes modules

class FakeHermes:
    def __init__(self, fail=None):
        self.calls = []
        self.home = None
        self.fail = fail
        fake = self

        class Constants:
            @staticmethod
            def set_hermes_home_override(path):
                fake.home = path
                return path

            @staticmethod
            def reset_hermes_home_override(token):
                fake.home = None

        class Pool:
            def move_entry(self, credential_id, priority):
                fake.calls.append((fake.home, 'priority', credential_id, priority))
                return None if credential_id == 'missing' else object()

            def reset_status(self, credential_id):
                fake.calls.append((fake.home, 'reset', credential_id))
                return object()

        class Pools:
            @staticmethod
            def load_pool(provider):
                assert provider == 'openai-codex'
                return Pool()

        class Config:
            @staticmethod
            def save_env_value(key, value):
                fake.calls.append((fake.home, 'env', key, value))

            @staticmethod
            def remove_env_value(key):
                fake.calls.append((fake.home, 'env-remove', key))

            @staticmethod
            def set_config_value(key, value):
                if fake.fail == 'config':
                    raise SystemExit('✗ refused: private text')
                fake.calls.append((fake.home, 'config', key, value))

        self.modules = (Constants, Pools, Config)


def test_codex_is_reordered_in_every_home_and_reset_only_where_asked():
    fake = FakeHermes()
    job = {'codex': [{'home': 'A', 'id': 'one', 'reset': False}, {'home': 'B', 'id': 'two', 'reset': True}]}
    assert hermes_writer.run(job, *fake.modules) == ['B']
    assert fake.calls == [('A', 'priority', 'one', 0), ('B', 'priority', 'two', 0), ('B', 'reset', 'two')]
    assert fake.home is None  # every override was undone


def test_claude_writes_the_env_variable_then_the_setting_per_home():
    fake = FakeHermes()
    job = {'claude': {'homes': ['A', 'B'], 'variable': 'VAR', 'setting': 'plugins.x', 'value': 'C:/x'}}
    hermes_writer.run(job, *fake.modules)
    assert fake.calls == [('A', 'env', 'VAR', 'C:/x'), ('A', 'config', 'plugins.x', 'C:/x'),
                          ('B', 'env', 'VAR', 'C:/x'), ('B', 'config', 'plugins.x', 'C:/x')]
    fake.calls.clear()
    hermes_writer.run({'claude': {'homes': ['A'], 'variable': 'VAR', 'setting': 'plugins.x', 'value': None}}, *fake.modules)
    assert fake.calls == [('A', 'env-remove', 'VAR'), ('A', 'config', 'plugins.x', '')]


def test_a_missing_login_is_an_error_not_a_silent_skip():
    with pytest.raises(LookupError):
        hermes_writer.run({'codex': [{'home': 'A', 'id': 'missing', 'reset': False}]}, *FakeHermes().modules)


def test_the_protocol_says_only_ready_and_error_types(monkeypatch, capsys):
    fake = FakeHermes(fail='config')
    monkeypatch.setattr(hermes_writer, 'interfaces', lambda: fake.modules)
    job = {'claude': {'homes': ['A'], 'variable': 'VAR', 'setting': 'plugins.x', 'value': 'v'}}
    monkeypatch.setattr(sys, 'stdin', [json.dumps({'codex': [{'home': 'A', 'id': 'one', 'reset': False}]}) + '\n',
                                       json.dumps(job) + '\n'])
    hermes_writer.main()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [{'ready': True}, {'ok': True, 'reset': []}, {'ok': False, 'error': 'SystemExit'}]


def test_a_hermes_without_the_functions_is_not_ready(monkeypatch, capsys):
    def missing():
        raise ImportError('no hermes here')
    monkeypatch.setattr(hermes_writer, 'interfaces', missing)
    hermes_writer.main()
    assert json.loads(capsys.readouterr().out) == {'ready': False, 'reason': 'ImportError'}


# ---------------------------------------------------------------- the switch's side of it

def fake_helper(tmp_path, monkeypatch, body):
    """A stand-in for hermes_writer.py, run as a real child process with this Python."""
    script = tmp_path / 'helper.py'
    script.write_text(textwrap.dedent(body), encoding='utf-8')
    monkeypatch.setattr(ops, 'WRITER', script)
    monkeypatch.setattr(ops, 'runtime_python', lambda: Path(sys.executable))
    monkeypatch.setattr(ops, 'hidden_console', lambda: {})
    monkeypatch.setattr(ops, 'ROOT', tmp_path)


def test_the_writer_answers_jobs_through_one_process(tmp_path, monkeypatch):
    fake_helper(tmp_path, monkeypatch, '''
        import json, sys
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            job = json.loads(line)
            print(json.dumps({"ok": True, "reset": [s["home"] for s in job.get("codex", []) if s["reset"]]}), flush=True)
    ''')
    writer = ops.HermesWriter()
    writer.start()
    record = {}
    answer = ops.written_by_hermes(writer, {'codex': [{'home': 'A', 'id': 'x', 'reset': True}]}, record)
    assert answer['reset'] == ['A'] and record == {'writer': 'hermes'}
    assert ops.written_by_hermes(writer, {'codex': []}, record)['ok'] is True
    writer.close()
    assert writer.process is None


def test_a_writer_that_is_not_ready_hands_the_job_to_the_commands_and_says_why(tmp_path, monkeypatch):
    fake_helper(tmp_path, monkeypatch, '''
        import json
        print(json.dumps({"ready": False, "reason": "LookupError"}), flush=True)
    ''')
    writer = ops.HermesWriter()
    writer.start()
    record = {}
    assert ops.written_by_hermes(writer, {'codex': []}, record) is False
    assert record['writer'] == 'commands' and 'LookupError' in record['writer_fallback']


def test_a_writer_that_fails_a_job_hands_it_to_the_commands(tmp_path, monkeypatch):
    fake_helper(tmp_path, monkeypatch, '''
        import json, sys
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            print(json.dumps({"ok": False, "error": "TypeError"}), flush=True)
    ''')
    writer = ops.HermesWriter()
    writer.start()
    record = {}
    assert ops.written_by_hermes(writer, {'codex': []}, record) is False
    assert record['writer_fallback'] == 'a write failed (TypeError)'
    assert ops.written_by_hermes(writer, {'codex': []}, record) is False  # it stays on the commands


def test_a_writer_that_cannot_start_hands_the_job_to_the_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'runtime_python', lambda: tmp_path / 'missing-python.exe')
    monkeypatch.setattr(ops, 'hidden_console', lambda: {})
    writer = ops.HermesWriter()
    writer.start()
    record = {}
    assert ops.written_by_hermes(writer, {'codex': []}, record) is False
    assert record['writer_fallback'].startswith('could not start')


def test_codex_apply_uses_the_writer_and_names_the_profiles_it_reset(tmp_path, monkeypatch):
    root, alpha = tmp_path, tmp_path / 'profiles' / 'alpha'
    acc = {'work': {'entry': {'id': 'two'}}}
    cooled = {'work': {'entry': {'id': 'two', 'last_status': 'exhausted'}}}
    monkeypatch.setattr(ops, 'ROOT', root)
    monkeypatch.setattr(ops, 'codex_stores', lambda: [(root, acc), (alpha, cooled)])
    monkeypatch.setattr(ops, 'reorder_codex_stores', lambda target: pytest.fail('the writer did it'))
    jobs = []
    writer = SimpleNamespace(run=lambda job: jobs.append(job) or {'ok': True, 'reset': [str(alpha)]}, reason=None)
    record = {}
    assert ops.apply_codex('work', writer, record) == ['alpha']
    assert jobs == [{'codex': [{'home': str(root), 'id': 'two', 'reset': False},
                               {'home': str(alpha), 'id': 'two', 'reset': True}]}]
    assert record['writer'] == 'hermes'


def test_codex_apply_falls_back_to_the_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'codex_stores', lambda: [(tmp_path, {'work': {'entry': {'id': 'two'}}})])
    monkeypatch.setattr(ops, 'reorder_codex_stores', lambda target: ['default'])
    writer = SimpleNamespace(run=lambda job: None, reason='it did not start in time')
    record = {}
    assert ops.apply_codex('work', writer, record) == ['default']
    assert record == {'writer': 'commands', 'writer_fallback': 'it did not start in time'}
