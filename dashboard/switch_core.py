"""Account identity and restart transaction. No live IO in this module."""
import base64
import json
import time
from collections.abc import Mapping
if __package__:  # imported by the dashboard API inside Hermes's web server
    from .settings import load_settings
else:  # the helper scripts and the test suite
    from settings import load_settings

class AccountView(Mapping):
    """Read current local settings on use; imports never touch machine state."""
    def __init__(self, provider, field):
        self.provider, self.field = provider, field

    def _data(self):
        accounts = getattr(load_settings(), self.provider)
        return {key: (row.email if self.field == 'email' else row.label if self.field == 'label'
                      else (row.label, row.email)) for key, row in accounts.items()}

    def __getitem__(self, key):
        return self._data()[key]

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())


EMAILS = AccountView('codex', 'email')
CODEX_LABELS = AccountView('codex', 'label')
CLAUDE = AccountView('claude', 'pair')

PROVIDERS = ('codex', 'claude')


class SwitchError(RuntimeError):
    """A deliberately non-secret, user-readable failure."""


class WrongAccount(SwitchError):
    """A Claude login folder holds a login for an account other than the one configured for it."""


def identify(rows):
    expected = dict(EMAILS)
    if not expected:
        raise SwitchError('No Codex accounts are configured; no changes made.')
    if not isinstance(rows, list) or len(rows) < len(expected):
        raise SwitchError('Configured Codex accounts are missing; no changes made.')
    result = {}
    for row in rows:
        try:
            part = row['access_token'].split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
            email = claims['https://api.openai.com/profile']['email'].lower()
            account_id = claims['https://api.openai.com/auth']['chatgpt_account_id']
            if not account_id or not row['id']:
                raise ValueError()
        except Exception:
            raise SwitchError('Codex account identity is missing, duplicated, or unrecognized.') from None
        # A lapsed access token still says whose login it is. Hermes renews one from its refresh token the
        # first time it is used, so a login that stood idle holds a lapsed one as a matter of course: it is
        # noted here, and never stops anything.
        expired = isinstance(claims.get('exp'), (int, float)) and claims['exp'] <= time.time()
        # The email is the owner's own address, already shown beside each account; naming it is what makes
        # this refusal fixable.
        key = next((k for k, v in expected.items() if v == email), None)
        if key is None:
            raise SwitchError(f'Hermes holds a Codex login for {email}, which is not an account in the '
                              'account-switch settings.')
        if key in result:
            if result[key]['account_id'] != account_id or any(
                    entry['id'] == row['id'] for entry in result[key]['entries']):
                raise SwitchError('Codex account identity is missing, duplicated, or unrecognized.')
            result[key]['entries'].append(row)
            if row.get('priority', float('inf')) < result[key]['entry'].get('priority', float('inf')):
                result[key]['entry'] = row
                result[key]['expired'] = expired
        else:
            result[key] = {'key': key, 'email': email, 'account_id': account_id,
                           'entry': row, 'entries': [row], 'expired': expired}
    if len(result) != len(expected) or len({x['account_id'] for x in result.values()}) != len(expected):
        raise SwitchError('Configured Codex accounts are missing or share one identity.')
    return result


def lapsed_note(item):
    """Why a lapsed login's availability was not asked about, for the receipt; None when it was."""
    if not item.get('expired'):
        return None
    return (f"The {CODEX_LABELS[item['key']]} login's access token had lapsed when the switch began, so its "
            'availability was not checked with OpenAI. Hermes renews the token at first use; if it cannot, the '
            'button will warn that Codex is billing another account.')


def selected(accounts):
    # fill_first tries the LOWEST priority first; stores written by `auth add` can
    # legitimately start at 2/3 rather than 0/1, so compare, never test for 0.
    prios = {key: value['entry'].get('priority') for key, value in accounts.items()}
    if any(type(p) is not int for p in prios.values()) or len(set(prios.values())) != len(prios):
        return None
    return min(prios, key=prios.get)


def _usable(row, now):
    """Mirror Hermes's pool: a DEAD login never serves; an EXHAUSTED one waits for its reset."""
    status = row.get('last_status')
    if status == 'dead':
        return False
    if status == 'exhausted':
        reset = row.get('last_error_reset_at')
        return isinstance(reset, (int, float)) and reset <= now
    return True


def effective(accounts, now):
    """The account Hermes actually tries first once dead/exhausted logins are skipped (None = no usable login)."""
    rows = sorted(((row.get('priority', float('inf')), key, row)
                   for key, item in accounts.items() for row in item['entries']), key=lambda t: t[0])
    return next((key for _, key, row in rows if _usable(row, now)), None)


def health_warning(name, accounts, now):
    """Plain-English warning when a store's preferred login cannot serve and another account (or none) pays."""
    preferred = selected(accounts)
    actual = effective(accounts, now)
    if preferred is None or actual == preferred:
        return None
    dead = accounts[preferred]['entry'].get('last_status') or 'unusable'
    who = CODEX_LABELS[preferred]
    if dead == 'exhausted':
        # A usage limit passes by itself; a new sign-in would change nothing.
        if actual is None:
            return (f'{name}: no Codex login is usable ({who} has reached its usage limit). It comes back by '
                    'itself when the limit resets.')
        return (f'{name}: {who} has reached its usage limit, so Codex is billing {CODEX_LABELS[actual]}. '
                f'{who} comes back by itself when the limit resets.')
    if actual is None:
        return f'{name}: no Codex login is usable ({who} login is {dead}). Sign in again.'
    return f'{name}: {who} login is {dead}, so Codex is billing {CODEX_LABELS[actual]}. Sign {who} in again.'


def summaries(accounts):
    return [{'key': key, 'label': CODEX_LABELS[key], 'email': item['email'], 'priority': item['entry'].get('priority')}
            for key, item in accounts.items()]


def normalize_request(body):
    """``{'codex': key|None, 'claude': key|None}`` from a confirmed POST body; None = leave that provider alone."""
    bad = SwitchError('Choose a Codex and/or Claude account and explicitly confirm the restart.')
    if not isinstance(body, dict) or body.get('confirmed') is not True or set(body) - {'confirmed', *PROVIDERS}:
        raise bad
    request = {name: body.get(name) for name in PROVIDERS}
    if request['codex'] is not None and request['codex'] not in EMAILS:
        raise bad
    if request['claude'] is not None and request['claude'] not in CLAUDE:
        raise bad
    if all(value is None for value in request.values()):
        raise bad
    return request


def describe(request):
    parts = []
    if request.get('codex'):
        parts.append('Codex ' + CODEX_LABELS[request['codex']])
    if request.get('claude'):
        parts.append('Claude ' + CLAUDE[request['claude']][0])
    return ' and '.join(parts)


def switch_transaction(ops, request):
    if not any(request.get(name) for name in PROVIDERS):
        raise SwitchError('Choose a Codex and/or Claude account.')
    previous = None
    changed = False
    try:
        previous = ops.preflight(request)['previous']
        ops.freeze()
        ops.stop()
        changed = True  # apply may partially succeed; recovery must restore ordering.
        ops.apply(request)
        ops.start()
        return ops.verify(request)
    except Exception:
        if previous is not None:
            ops.recover(previous, changed)
        raise
    finally:
        ops.unfreeze()
