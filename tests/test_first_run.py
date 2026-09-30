"""The dialog's own setup: what it finds, what it writes, and its two routes. Temp homes and fixture logins only."""
import json
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'dashboard'))
import first_run
import plugin_api as api
import setup_settings as setup
from settings import load_settings
from test_setup import gateway_record, home, sign_in  # noqa: F401  (home is a fixture)

OWNERS = {'token-anthropic': 'first@example.invalid', 'token-gmail': 'second@example.invalid'}


def owner(token):
    return OWNERS[token]


def claude_login(home, folder, token, remembered='', expires_in=3600, refresh='fixture-private-refresh'):
    import time
    path = home / 'claude-auth' / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
        'accessToken': token, 'refreshToken': refresh, 'expiresAt': (time.time() + expires_in) * 1000}}), encoding='utf-8')
    if remembered:
        (path / '.claude.json').write_text(json.dumps({'oauthAccount': {'emailAddress': remembered}}), encoding='utf-8')
    return path


def saved(home):
    return load_settings(first_run.settings_file(home))


def test_found_lists_accounts_names_and_the_gateway_without_writing(home, monkeypatch):
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'), ('work', 'team@example.invalid', 'account-two'))
    claude_login(home, 'anthropic', 'token-anthropic')
    monkeypatch.setattr(first_run, 'gateway_service_found', lambda found_home: 'FixtureGateway')
    seen = first_run.found(home, owner)
    assert seen['settings_present'] is False and seen['gateway_service'] == 'FixtureGateway'
    assert seen['codex'] == [{'email': 'jo.smith@example.invalid', 'name': 'Jo Smith', 'set_up': False},
                             {'email': 'team@example.invalid', 'name': 'Work', 'set_up': False}]
    assert seen['claude'] == [{'key': 'anthropic', 'email': 'first@example.invalid', 'name': 'Anthropic',
                               'verified': True, 'set_up': False}]
    assert not first_run.settings_file(home).exists()
    assert 'token-' not in json.dumps(seen) and 'fixture-private' not in json.dumps(seen)


def test_a_claude_login_is_listed_under_the_owner_anthropic_names_not_the_one_the_folder_remembers(home):
    claude_login(home, 'gmail', 'token-gmail', remembered='someone-else@example.invalid')
    [row] = first_run.claude_logins(home, owner)
    assert row['email'] == 'second@example.invalid' and row['verified'] is True
    assert row['directory'] == (home / 'claude-auth' / 'gmail').as_posix()


def test_a_login_too_old_to_ask_with_is_listed_as_remembered_and_not_verified(home):
    claude_login(home, 'gmail', 'token-gmail', remembered='Second@Example.invalid', expires_in=-60)
    [row] = first_run.claude_logins(home, lambda token: pytest.fail('an expired login is not sent anywhere'))
    assert row['email'] == 'second@example.invalid' and row['verified'] is False


def test_when_anthropic_cannot_be_reached_the_remembered_owner_is_listed(home):
    def unreachable(token):
        raise OSError('no network')
    claude_login(home, 'gmail', 'token-gmail', remembered='second@example.invalid')
    [row] = first_run.claude_logins(home, unreachable)
    assert row['email'] == 'second@example.invalid' and row['verified'] is False


def test_folders_without_a_whole_login_or_a_known_owner_are_not_accounts(home):
    claude_login(home, 'half', 'token-gmail', refresh='')
    claude_login(home, 'nobody', 'token-unknown', expires_in=-60)
    claude_login(home, 'Bad Name', 'token-gmail')
    (home / 'claude-auth' / 'empty').mkdir()
    (home / 'claude-auth' / 'a-file').write_text('x', encoding='utf-8')
    assert first_run.claude_logins(home, owner) == []


def test_save_writes_settings_the_plugin_accepts_under_the_names_given(home, monkeypatch):
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'), ('work', 'team@example.invalid', 'account-two'))
    claude_login(home, 'anthropic', 'token-anthropic')
    claude_login(home, 'gmail', 'token-gmail')
    monkeypatch.setattr(first_run, 'gateway_service_found', lambda found_home: 'FixtureGateway')
    first_run.save(home, {'Jo.Smith@example.invalid': 'Personal'}, {'gmail': 'Family'}, owner)
    loaded = saved(home)
    assert {key: (row.label, row.email) for key, row in loaded.codex.items()} == {
        'personal': ('Personal', 'jo.smith@example.invalid'), 'work': ('Work', 'team@example.invalid')}
    assert {key: (row.label, row.email, row.directory) for key, row in loaded.claude.items()} == {
        'anthropic': ('Anthropic', 'first@example.invalid', home / 'claude-auth' / 'anthropic'),
        'gmail': ('Family', 'second@example.invalid', home / 'claude-auth' / 'gmail')}
    assert loaded.gateway_service == 'FixtureGateway'
    text = first_run.settings_file(home).read_text()
    assert 'token-' not in text and 'fixture-private' not in text


@pytest.mark.parametrize('codex, claude', [({'jo.smith@example.invalid': r'.\setup.cmd claude x me@example.invalid'}, {}),
                                           ({}, {'anthropic': 'x' * 25})])
def test_a_name_that_cannot_be_shown_stops_the_save_with_nothing_written(home, codex, claude):
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'))
    claude_login(home, 'anthropic', 'token-anthropic')
    with pytest.raises(first_run.SetupError, match='cannot be the name of'):
        first_run.save(home, codex, claude, owner)
    assert not first_run.settings_file(home).exists()


def test_a_second_save_keeps_names_and_adds_only_what_is_new(home):
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'))
    claude_login(home, 'anthropic', 'token-anthropic')
    first_run.save(home, {'jo.smith@example.invalid': 'Mine'}, {}, owner)
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'), ('', 'team@example.invalid', 'account-two'))
    claude_login(home, 'gmail', 'token-gmail')
    seen = first_run.found(home, owner)
    assert [row['set_up'] for row in seen['codex']] == [True, False] and [row['set_up'] for row in seen['claude']] == [True, False]
    first_run.save(home, {'jo.smith@example.invalid': 'Renamed', 'team@example.invalid': 'Team'}, {}, owner)
    loaded = saved(home)
    assert {key: row.label for key, row in loaded.codex.items()} == {'mine': 'Mine', 'team': 'Team'}
    assert set(loaded.claude) == {'anthropic', 'gmail'}


def test_the_command_line_setup_adds_the_claude_logins_it_finds(home):
    sign_in(home, ('personal', 'personal@example.invalid', 'account-one'))
    claude_login(home, 'anthropic', 'token-anthropic')
    said = []
    assert setup.main(['--home', str(home), '--yes'], ask=lambda prompt: pytest.fail('asked: ' + prompt),
                      say=said.append, owner=owner) == 0
    assert saved(home).claude['anthropic'].email == 'first@example.invalid'
    assert 'anthropic: Anthropic <first@example.invalid>' in '\n'.join(said)


def test_show_writes_nothing_and_asks_nothing(home):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'))
    said = []
    assert setup.main(['--home', str(home), '--show'], ask=lambda prompt: pytest.fail('asked: ' + prompt),
                      say=said.append, owner=owner) == 0
    assert 'first: First <first@example.invalid>' in '\n'.join(said) and said[-1] == 'Nothing was written (--show).'
    assert not first_run.settings_file(home).exists()


def test_an_agent_gives_names_on_the_command_line(home):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'), ('device_code', 'second@example.invalid', 'account-two'))
    assert setup.main(['--home', str(home), '--yes', '--name', 'First@example.invalid=Home PC'],
                      ask=lambda prompt: pytest.fail('asked: ' + prompt), say=lambda line: None, owner=owner) == 0
    assert {key: row.label for key, row in saved(home).codex.items()} == {'home-pc': 'Home PC', 'second': 'Second'}


def test_a_codex_and_a_claude_account_with_one_email_are_named_apart(home):
    # Seen on the test machine: one name given by email landed on both.
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'))
    claude_login(home, 'main', 'token-anthropic')
    assert setup.main(['--home', str(home), '--yes', '--name', 'first@example.invalid=Work',
                       '--name', 'Claude:First@example.invalid=Family'],
                      ask=lambda prompt: pytest.fail('asked: ' + prompt), say=lambda line: None, owner=owner) == 0
    assert saved(home).codex['work'].label == 'Work' and saved(home).claude['main'].label == 'Family'
    (first_run.settings_file(home)).unlink()
    assert setup.main(['--home', str(home), '--yes', '--name', 'codex:first@example.invalid=Work'],
                      ask=lambda prompt: pytest.fail('asked: ' + prompt), say=lambda line: None, owner=owner) == 0
    assert saved(home).codex['work'].label == 'Work' and saved(home).claude['main'].label == 'Main'


@pytest.mark.parametrize('bad', ['first@example.invalid', 'Work', 'first@example.invalid=a/b', 'first@example.invalid='])
def test_a_name_argument_that_is_not_email_equals_name_stops_setup(home, bad):
    sign_in(home, ('device_code', 'first@example.invalid', 'account-one'))
    said = []
    assert setup.main(['--home', str(home), '--yes', '--name', bad], ask=lambda prompt: pytest.fail('asked'),
                      say=said.append, owner=owner) == 1
    assert 'inside quotes' in said[-1] and not first_run.settings_file(home).exists()


@pytest.fixture
def client(home, monkeypatch, tmp_path):
    monkeypatch.setattr(api, 'authorize', lambda request: None)
    monkeypatch.setattr(api.first_run, 'hermes_home', lambda: home)
    monkeypatch.setattr(api.first_run, 'token_owner', owner)
    monkeypatch.setattr(api.ops, 'LOCK', tmp_path / 'lock.json')
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_the_setup_route_shows_what_was_found_and_the_save_route_writes_it(client, home):
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'), ('work', 'team@example.invalid', 'account-two'))
    claude_login(home, 'anthropic', 'token-anthropic')
    seen = client.get('/setup')
    assert seen.status_code == 200 and seen.json()['settings_present'] is False
    assert [row['email'] for row in seen.json()['codex']] == ['jo.smith@example.invalid', 'team@example.invalid']
    assert 'token-' not in seen.text and 'fixture-private' not in seen.text
    done = client.post('/setup', json={'confirmed': True, 'codex': {'jo.smith@example.invalid': 'Personal'}, 'claude': {}})
    assert done.status_code == 200 and done.json() == {'ok': True, 'codex': 2, 'claude': 1}
    assert saved(home).codex['personal'].email == 'jo.smith@example.invalid'
    assert client.get('/setup').json()['settings_present'] is True


@pytest.mark.parametrize('body, status', [({}, 400), ({'confirmed': False}, 400), ({'confirmed': 'yes'}, 400),
                                          ({'confirmed': True, 'codex': ['a']}, 400),
                                          ({'confirmed': True, 'codex': {'jo.smith@example.invalid': 'a/b'}}, 409)])
def test_a_save_that_is_not_confirmed_or_not_usable_writes_nothing(client, home, body, status):
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'))
    assert client.post('/setup', json=body).status_code == status
    assert not first_run.settings_file(home).exists()


def test_nothing_is_saved_while_a_switch_is_in_progress(client, home):
    sign_in(home, ('device_code', 'jo.smith@example.invalid', 'account-one'))
    api.ops.LOCK.write_text('{}', encoding='utf-8')
    answer = client.post('/setup', json={'confirmed': True})
    assert answer.status_code == 409 and 'already in progress' in answer.text
    assert not first_run.settings_file(home).exists()


def test_a_home_setup_cannot_read_gives_a_plain_reason(client, home):
    (home / 'auth.json').write_text('{not json', encoding='utf-8')
    answer = client.get('/setup')
    assert answer.status_code == 409 and answer.json()['detail'] == 'The Hermes login store could not be read.'
