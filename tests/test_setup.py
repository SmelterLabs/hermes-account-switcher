"""Guided settings setup: temp homes and fixture tokens only."""
import json
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dashboard'))
import setup_settings as setup
from settings import load_settings
from test_switch import token


@pytest.fixture
def home(tmp_path):
    (tmp_path / 'config.yaml').write_text('model: {}\n', encoding='utf-8')
    (tmp_path / 'hermes-agent/hermes_cli').mkdir(parents=True)
    (tmp_path / 'hermes-agent/hermes_cli/main.py').write_text('', encoding='utf-8')
    desktop = tmp_path / 'hermes-agent' / setup.DESKTOP
    desktop.parent.mkdir(parents=True)
    desktop.write_bytes(b'')
    return tmp_path


def sign_in(home, *rows):
    (home / 'auth.json').write_text(json.dumps({'credential_pool': {'openai-codex': [
        {'id': f'row{n}', 'label': label, 'priority': n, 'access_token': token(email, account),
         'refresh_token': 'fixture-private-refresh'} for n, (label, email, account) in enumerate(rows)]}}),
        encoding='utf-8')


def run(home, *extra, answer='y', names=None):
    """``answer`` is given to the question whether to write; ``names`` maps an email to the name typed for
    it, and an account without one gets Enter, which takes the name offered."""
    said = []

    def ask(prompt):
        if prompt.startswith('Short name for the Codex account '):
            return (names or {}).get(prompt.split()[6], '')
        return answer

    code = setup.main(['--home', str(home), *extra], ask=ask, say=said.append)
    return code, '\n'.join(said)


def settings_file(home):
    return home / 'plugin-data/codex-account-switch/settings.json'


def test_writes_settings_the_plugin_accepts_from_what_is_signed_in(home):
    sign_in(home, ('personal', 'Personal@Example.invalid', 'account-one'), ('work', 'work@example.invalid', 'account-two'))
    code, text = run(home)
    assert code == 0 and 'Settings written.' in text
    loaded = load_settings(settings_file(home))
    assert {key: (row.label, row.email) for key, row in loaded.codex.items()} == {
        'personal': ('Personal', 'personal@example.invalid'), 'work': ('Work', 'work@example.invalid')}
    assert loaded.gateway_service is None and loaded.gateway_health_url is None
    assert loaded.hermes_root == home and loaded.desktop_exe == home / 'hermes-agent' / setup.DESKTOP
    assert 'fixture' not in settings_file(home).read_text() and 'fixture-private' not in text


def test_nothing_is_written_without_a_yes(home):
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'))
    code, text = run(home, answer='')
    assert code == 0 and 'Nothing was written.' in text
    assert not settings_file(home).exists()
    assert 'Only one Codex account is signed in' in text


def test_a_later_run_adds_a_new_account_and_keeps_what_the_user_named(home):
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'))
    assert run(home, '--yes')[0] == 0
    data = json.loads(settings_file(home).read_text())
    data['codex']['personal']['label'] = 'My own'
    data['claude'] = {'main': {'label': 'Main', 'email': 'main@example.invalid', 'directory': str(home / 'claude-auth/main')}}
    settings_file(home).write_text(json.dumps(data))
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'), ('', 'second@example.invalid', 'account-two'),
            ('again', 'second@example.invalid', 'account-two'))
    assert run(home, '--yes')[0] == 0
    after = json.loads(settings_file(home).read_text())
    assert after['codex'] == {'personal': {'label': 'My own', 'email': 'personal@example.invalid'},
                              'second': {'label': 'Second', 'email': 'second@example.invalid'}}
    assert after['claude'] == data['claude']
    code, text = run(home, answer='n')
    assert code == 0 and 'Nothing to change.' in text
    # Seen on the test machine: hand-written settings with backslash paths were rewritten for nothing.
    after['hermes_root'] = str(home).replace('/', '\\')
    settings_file(home).write_text(json.dumps(after))
    before = settings_file(home).read_bytes()
    code, text = run(home, '--yes')
    assert code == 0 and 'Nothing to change.' in text and settings_file(home).read_bytes() == before


def test_the_example_file_is_one_the_plugin_accepts_for_a_stock_install():
    # The example used to pair "no Windows service" with a health address stock Hermes does not serve,
    # which would have failed every switch at the health check.
    loaded = load_settings(ROOT / 'settings.example.json')
    assert loaded.gateway_service is None and loaded.gateway_health_url is None
    assert set(loaded.codex) == {'personal', 'work'} and loaded.claude == {}


def test_a_claude_account_is_added_with_a_login_folder_inside_the_hermes_home(home):
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'))
    assert run(home, '--yes', '--claude', 'Main=Me@Example.invalid')[0] == 0
    loaded = load_settings(settings_file(home))
    assert loaded.claude['main'].email == 'me@example.invalid' and loaded.claude['main'].label == 'Main'
    assert loaded.claude['main'].directory == home / 'claude-auth' / 'main'
    # Adding it again changes nothing; another email under the same key, or the same email twice, is refused.
    code, text = run(home, '--yes', '--claude', 'main=me@example.invalid')
    assert code == 0 and 'Nothing to change.' in text
    before = settings_file(home).read_bytes()
    for bad, words in (('main=other@example.invalid', 'already used for another email'),
                       ('second=me@example.invalid', 'already set up as a Claude account'),
                       ('no-email', 'given as key=email'), ('Bad Key=me2@example.invalid', 'given as key=email')):
        code, text = run(home, '--yes', '--claude', bad)
        assert code == 1 and words in text and settings_file(home).read_bytes() == before


def test_claude_only_setup_needs_no_codex_login(home):
    assert run(home, '--yes', '--claude', 'main=me@example.invalid')[0] == 0
    loaded = load_settings(settings_file(home))
    assert loaded.codex == {} and set(loaded.claude) == {'main'}


def test_two_logins_with_the_same_name_get_different_keys(home):
    sign_in(home, ('main', 'a@example.invalid', 'account-one'), ('main', 'b@example.invalid', 'account-two'),
            ('9 lives!', 'c@example.invalid', 'account-three'))
    assert run(home, '--yes')[0] == 0
    assert list(json.loads(settings_file(home).read_text())['codex']) == ['main', 'main-2', 'account']


def test_a_windows_service_gateway_is_recorded_when_named(home):
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'))
    assert run(home, '--yes', '--gateway-service', 'HermesGateway')[0] == 0
    loaded = load_settings(settings_file(home))
    assert loaded.gateway_service == 'HermesGateway' and loaded.gateway_health_url == 'http://127.0.0.1:8642/health'


@pytest.mark.parametrize('broken, words', [
    ('no_config', 'is not a Hermes home'),
    ('no_source', 'installed by its official installer or install script'),
    ('no_desktop', 'Hermes Desktop was not found'),
    ('no_logins', 'No Codex account is signed in'),
    ('bad_login', 'could not be read'),
    ('bad_settings', 'exists but is not valid'),
])
def test_setup_stops_with_a_plain_reason_and_writes_nothing(home, broken, words):
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'))
    before = None
    if broken == 'no_config':
        (home / 'config.yaml').unlink()
    elif broken == 'no_source':
        (home / 'hermes-agent/hermes_cli/main.py').unlink()
    elif broken == 'no_desktop':
        (home / 'hermes-agent' / setup.DESKTOP).unlink()
    elif broken == 'no_logins':
        (home / 'auth.json').unlink()
    elif broken == 'bad_login':
        (home / 'auth.json').write_text(json.dumps({'credential_pool': {'openai-codex': [
            {'id': 'x', 'access_token': 'fixture-private-token'}]}}))
    elif broken == 'bad_settings':
        settings_file(home).parent.mkdir(parents=True)
        settings_file(home).write_text('{"codex": ')
        before = settings_file(home).read_text()
    code, text = run(home, '--yes')
    assert code == 1 and words in text
    assert 'fixture-private' not in text
    assert (settings_file(home).read_text() if settings_file(home).exists() else None) == before


# A gateway run by a Windows service: setup finds the service by itself while that gateway is running.

def gateway_record(home, pid=500, state='running', age=0):
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    (home / 'gateway_state.json').write_text(json.dumps(
        {'pid': pid, 'gateway_state': state, 'updated_at': stamp}), encoding='utf-8')


def answering(text, asked=None):
    from types import SimpleNamespace

    def run(args, **kw):
        if asked is not None:
            asked.append(args)
        return SimpleNamespace(stdout=text)
    return run


def test_the_service_is_read_from_the_process_tree_above_the_gateway(home):
    gateway_record(home)
    asked = []
    assert setup.gateway_service_found(home, run=answering('HermesGateway\n', asked)) == 'HermesGateway'
    assert asked[0][0] == 'powershell.exe' and '$p=500;' in asked[0][-1]


@pytest.mark.parametrize('state, age', [('running', 600), ('stopped', 0)])
def test_an_old_record_or_a_stopped_gateway_asks_nothing(home, state, age):
    gateway_record(home, state=state, age=age)
    assert setup.gateway_service_found(home, run=lambda *a, **k: pytest.fail('nothing to ask about')) is None


def test_no_gateway_record_asks_nothing(home):
    assert setup.gateway_service_found(home, run=lambda *a, **k: pytest.fail('nothing to ask about')) is None


@pytest.mark.parametrize('answer', ['', '\n', 'two words\n', 'Get-CimInstance : Access denied\n'])
def test_an_answer_that_is_not_a_service_name_is_no_service(home, answer):
    gateway_record(home)
    assert setup.gateway_service_found(home, run=answering(answer)) is None


def test_a_failed_question_is_no_service(home):
    gateway_record(home)

    def run(args, **kw):
        raise OSError('powershell is missing')
    assert setup.gateway_service_found(home, run=run) is None


def test_setup_writes_the_service_it_found_and_says_so(home, monkeypatch):
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'))
    monkeypatch.setattr(setup, 'gateway_service_found', lambda found_home: 'HermesGateway')
    code, said = run(home, '--yes')
    assert code == 0 and 'Windows service HermesGateway' in said
    assert load_settings(settings_file(home)).gateway_service == 'HermesGateway'


def test_a_service_named_by_hand_wins_over_the_one_found(home, monkeypatch):
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'))
    monkeypatch.setattr(setup, 'gateway_service_found', lambda found_home: pytest.fail('a named service is not looked for'))
    assert run(home, '--yes', '--gateway-service', 'MyGateway')[0] == 0
    assert load_settings(settings_file(home)).gateway_service == 'MyGateway'


# Names. Hermes calls a login added without a name "device_code": how it was made, not whose it is. Found by
# installing the plugin the way a user would: the button read "Codex: Device_Code".

def test_the_user_names_each_new_account_and_the_key_follows_the_name(home):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'),
            ('personal-2026-09-27', 'second@example.invalid', 'account-two'))
    code, said = run(home, names={'first@example.invalid': 'Work', 'second@example.invalid': '  Home   PC '})
    assert code == 0
    loaded = load_settings(settings_file(home))
    assert {key: (row.label, row.email) for key, row in loaded.codex.items()} == {
        'work': ('Work', 'first@example.invalid'), 'home-pc': ('Home PC', 'second@example.invalid')}


def test_enter_takes_the_name_offered_and_an_unnamed_login_is_offered_the_email_name(home):
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'), ('', 'other@example.invalid', 'account-two'),
            ('team-login', 'third@example.invalid', 'account-three'))
    assert run(home)[0] == 0
    loaded = load_settings(settings_file(home))
    assert {key: row.label for key, row in loaded.codex.items()} == {
        'jo-smith': 'Jo Smith', 'other': 'Other', 'team-login': 'Team Login'}


def test_with_yes_nobody_is_asked(home):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'))
    said = []
    code = setup.main(['--home', str(home), '--yes'], ask=lambda prompt: pytest.fail('asked: ' + prompt), say=said.append)
    assert code == 0 and load_settings(settings_file(home)).codex['first'].label == 'First'


def test_an_account_already_in_the_settings_is_not_asked_about_again(home):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'))
    assert run(home, names={'first@example.invalid': 'Mine'})[0] == 0
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'), ('device_code', 'second@example.invalid', 'account-two'))
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return 'y' if prompt.startswith('Write') else 'Theirs'
    assert setup.main(['--home', str(home)], ask=ask, say=lambda line: None) == 0
    assert [p for p in asked if 'Short name' in p] == ['Short name for the Codex account second@example.invalid [Second]: ']
    loaded = load_settings(settings_file(home))
    assert {key: row.label for key, row in loaded.codex.items()} == {'mine': 'Mine', 'theirs': 'Theirs'}


def test_two_accounts_given_the_same_name_still_get_different_keys(home):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'), ('device_code', 'second@example.invalid', 'account-two'))
    assert run(home, names={'first@example.invalid': 'Me', 'second@example.invalid': 'Me'})[0] == 0
    assert set(load_settings(settings_file(home)).codex) == {'me', 'me-2'}


# Seen on the author's own PC: the next commands, run while setup was waiting for a name, became the names.

def asking(answers, said):
    answers = list(answers)

    def ask(prompt):
        said.append(prompt)
        return answers.pop(0)
    return ask


def test_a_command_typed_ahead_is_not_taken_as_a_name(home):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'))
    said = []
    ask = asking(['hermes plugins enable codex-account-switch', r'.\setup.cmd claude main me@example.invalid', 'Mine', 'y'], said)
    assert setup.main(['--home', str(home)], ask=ask, say=said.append) == 0
    assert load_settings(settings_file(home)).codex['mine'].label == 'Mine'
    assert sum('cannot be a name' in line for line in said) == 2


def test_three_unusable_names_end_setup_with_nothing_written(home):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'))
    said = []
    ask = asking(['a/b', 'x' * 25, 'me@example.invalid', 'y'], said)
    assert setup.main(['--home', str(home)], ask=ask, say=said.append) == 1
    assert 'No usable name was given' in said[-1] and not settings_file(home).exists()


@pytest.mark.parametrize('typed', ['Work', 'Home PC', 'team-2', 'a', 'X' * 24])
def test_a_short_plain_name_is_taken_as_typed(home, typed):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'))
    assert run(home, names={'first@example.invalid': typed})[0] == 0
    [row] = load_settings(settings_file(home)).codex.values()
    assert row.label == typed
