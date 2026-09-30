"""Hermetic tests. All account tokens and operating-system actions are fixtures."""
import base64
import json
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from switch_core import (describe, effective, health_warning, identify, normalize_request, selected,
                         switch_transaction, SwitchError)


def token(email, aid):
    payload = {'https://api.openai.com/profile': {'email': email},
               'https://api.openai.com/auth': {'chatgpt_account_id': aid}}
    return 'fixture.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') + '.fixture'


def entries():
    return [{'id': 'one', 'label': 'wrong', 'priority': 0, 'access_token': token('personal@example.invalid', 'account-one')},
            {'id': 'two', 'label': 'wrong', 'priority': 1, 'access_token': token('work@example.invalid', 'account-two')}]


def test_identity_is_not_label():
    assert selected(identify(entries())) == 'personal'


def test_duplicate_identity_rejected():
    rows = entries()
    rows[1]['access_token'] = token('work@example.invalid', 'account-one')
    with pytest.raises(SwitchError): identify(rows)


def test_duplicate_grant_for_same_account_preserves_preferred_account():
    rows = entries()
    duplicate = dict(rows[0], id='third', priority=2)
    accounts = identify(rows + [duplicate])
    assert selected(accounts) == 'personal'
    assert {row['id'] for row in accounts['personal']['entries']} == {'one', 'third'}
    assert accounts['personal']['entry']['id'] == 'one'


def test_missing_account_rejected():
    with pytest.raises(SwitchError): identify(entries()[:1])


def test_duplicate_email_with_different_account_id_rejected():
    rows = entries()
    rows.append(dict(rows[0], id='third', priority=2,
                     access_token=token('personal@example.invalid', 'someone-else')))
    with pytest.raises(SwitchError): identify(rows)


def test_malformed_token_does_not_leak():
    rows = entries()
    rows[0]['access_token'] = 'private-bad-token'
    with pytest.raises(SwitchError) as e: identify(rows)
    assert 'private-bad-token' not in str(e.value)


def token_with(email, aid, **claims):
    payload = {'https://api.openai.com/profile': {'email': email},
               'https://api.openai.com/auth': {'chatgpt_account_id': aid}, **claims}
    return 'fixture.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') + '.fixture'


def test_a_lapsed_login_is_still_read_and_only_noted():
    # Seen on a real stock install: Hermes renewed the expired login by itself within seconds, while the
    # refusal read "missing, duplicated, or unrecognized" and named nothing.
    # Seen on the author's own PC: two profiles had stood idle for two days, their access tokens had lapsed
    # as a matter of course, and the plugin refused to show anything at all.
    from switch_core import lapsed_note
    rows = entries()
    rows[1]['access_token'] = token_with('work@example.invalid', 'account-two', exp=1000)
    accounts = identify(rows)
    assert selected(accounts) == 'personal'
    assert accounts['work']['expired'] is True and accounts['personal']['expired'] is False
    assert lapsed_note(accounts['personal']) is None
    note = lapsed_note(accounts['work'])
    assert note.startswith("The Work login's access token had lapsed") and 'renews' in note and 'fixture.' not in note
    rows[1]['access_token'] = token_with('work@example.invalid', 'account-two', exp=4102444800)
    assert identify(rows)['work']['expired'] is False


def test_with_two_logins_for_one_account_the_one_hermes_tries_first_decides_whether_it_has_lapsed():
    rows = entries()
    rows.append(dict(rows[1], id='older', priority=5, access_token=token_with('work@example.invalid', 'account-two', exp=1000)))
    assert identify(rows)['work']['expired'] is False
    rows[2]['priority'] = -1
    assert identify(rows)['work']['expired'] is True


def test_a_login_for_an_account_outside_the_settings_is_named():
    rows = entries()
    rows[1]['access_token'] = token('stranger@example.invalid', 'account-two')
    with pytest.raises(SwitchError) as e: identify(rows)
    assert str(e.value) == ('Hermes holds a Codex login for stranger@example.invalid, which is not an account '
                            'in the account-switch settings.')


def test_dead_preferred_login_warns_and_reports_the_paying_account():
    # Preferred login first but dead: the fallback account silently serves and is billed.
    rows = entries()
    rows[0]['last_status'] = 'dead'
    rows.append(dict(entries()[0], id='third', priority=2))
    acc = identify(rows)
    assert selected(acc) == 'personal'
    assert effective(acc, 1000) == 'work'
    assert health_warning('alpha', acc, 1000) == \
        'alpha: Personal login is dead, so Codex is billing Work. Sign Personal in again.'


def test_healthy_or_expired_exhaustion_has_no_warning():
    rows = entries()
    assert health_warning('alpha', identify(rows), 1000) is None
    rows[0].update(last_status='exhausted', last_error_reset_at=500)
    assert effective(identify(rows), 1000) == 'personal'


def test_a_login_at_its_usage_limit_is_waited_for_not_signed_in_again():
    # Seen live: OpenAI answered 429, Hermes marked the login exhausted, and the advice was to sign in again.
    rows = entries()
    rows[0].update(last_status='exhausted', last_error_reset_at=5000)
    assert health_warning('alpha', identify(rows), 1000) == \
        ('alpha: Personal has reached its usage limit, so Codex is billing Work. '
         'Personal comes back by itself when the limit resets.')
    for row in rows:
        row.update(last_status='exhausted', last_error_reset_at=5000)
    warning = health_warning('alpha', identify(rows), 1000)
    assert 'no Codex login is usable' in warning and 'Sign' not in warning


def test_no_usable_login_warns():
    rows = entries()
    for row in rows:
        row['last_status'] = 'dead'
    assert effective(identify(rows), 1000) is None
    assert 'no Codex login is usable' in health_warning('alpha', identify(rows), 1000)


@pytest.mark.parametrize('body', [{}, {'codex': 'work'}, {'codex': 'work', 'confirmed': 'true'},
    {'codex': 'work', 'confirmed': True, 'command': 'ignored'}, {'codex': '../wrong', 'confirmed': True},
    {'confirmed': True}, {'claude': 'nobody', 'confirmed': True}, {'target': 'work', 'confirmed': True}])
def test_request_validation_rejects_unconfirmed_or_unknown(body):
    with pytest.raises(SwitchError): normalize_request(body)


def test_request_normalizes_either_or_both_providers():
    assert normalize_request({'codex': 'work', 'confirmed': True}) == {'codex': 'work', 'claude': None}
    assert normalize_request({'claude': 'gmail', 'confirmed': True}) == {'codex': None, 'claude': 'gmail'}
    both = normalize_request({'codex': 'personal', 'claude': 'work', 'confirmed': True})
    assert both == {'codex': 'personal', 'claude': 'work'}
    assert describe(both) == 'Codex Personal and Claude Work'
    assert describe({'claude': 'anthropic'}) == 'Claude Anthropic'


class Ops:
    def __init__(self, fail=None): self.events = []; self.fail = fail
    def step(self, name):
        self.events.append(name)
        if self.fail == name: raise SwitchError('fixture failure')
    def preflight(self, request): self.step('preflight'); return {'previous': {'codex': 'personal', 'claude': 'unset'}}
    def freeze(self): self.step('freeze')
    def stop(self): self.step('stop')
    def apply(self, request): self.step('apply:' + json.dumps(request, sort_keys=True))
    def start(self): self.step('start')
    def verify(self, request): self.step('verify:' + json.dumps(request, sort_keys=True)); return {'ok': True}
    def unfreeze(self): self.step('unfreeze')
    def recover(self, previous, changed): self.events.append(('recover', previous, changed))


REQ = {'codex': 'work', 'claude': 'gmail'}
APPLY = 'apply:' + json.dumps(REQ, sort_keys=True)
VERIFY = 'verify:' + json.dumps(REQ, sort_keys=True)


def test_no_writes_until_all_processes_stopped():
    o = Ops(); result = switch_transaction(o, REQ)
    assert result == {'ok': True}
    assert o.events == ['preflight', 'freeze', 'stop', APPLY, 'start', VERIFY, 'unfreeze']


@pytest.mark.parametrize('stage', ['preflight', 'freeze', 'stop'])
def test_busy_or_unknown_state_never_changes_credentials(stage):
    o = Ops(stage)
    with pytest.raises(SwitchError): switch_transaction(o, REQ)
    assert not any(str(x).startswith('apply:') for x in o.events)
    assert o.events[-1] == 'unfreeze'


@pytest.mark.parametrize('stage', [APPLY, 'start', VERIFY])
def test_failed_switch_requests_recovery_of_previous_preference(stage):
    o = Ops(stage)
    with pytest.raises(SwitchError): switch_transaction(o, REQ)
    assert ('recover', {'codex': 'personal', 'claude': 'unset'}, True) in o.events
    assert o.events[-1] == 'unfreeze'


def test_empty_request_no_os_calls():
    o = Ops()
    with pytest.raises(SwitchError): switch_transaction(o, {'codex': None, 'claude': None})
    assert o.events == []
