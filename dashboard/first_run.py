"""What a first run needs: find this Hermes installation and the accounts already signed in, and write the
Account Switcher's settings. One implementation for setup.cmd, for an agent, and for the dialog's own setup.

It never signs anyone in, never changes which account is selected, never enables a plugin, and never returns
or stores a token: the only thing taken from a login is the email address of its owner.
"""
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

if __package__:  # imported by the dashboard API inside Hermes's web server
    from .settings import SettingsError, _parse_settings, settings_path
else:  # the helper scripts and the test suite
    from settings import SettingsError, _parse_settings, settings_path

PLUGIN = 'codex-account-switch'
DESKTOP = Path('apps/desktop/release/win-unpacked/Hermes.exe')
NAME = r'[A-Za-z0-9][A-Za-z0-9 _-]{0,23}'
KEY = r'[a-z][a-z0-9_-]{0,39}'
EMAIL = r'[^\s@]+@[^\s@]+\.[^\s@]+'
NAME_RULE = 'use up to 24 letters, digits and spaces'


class SetupError(RuntimeError):
    """A plain-language reason setup cannot continue."""


def hermes_home():
    """The Hermes home the settings belong to: the first profile's, also when asked from inside another."""
    return settings_path().parents[2]


def settings_file(home):
    return Path(home) / 'plugin-data' / PLUGIN / 'settings.json'


def usable_name(text):
    """The name as it will be shown, or None when the text cannot be one. A name is what the button shows, so
    only a short plain one is taken: text meant for a terminal is not a name."""
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return text if re.fullmatch(NAME, text) else None


def find_installation(home):
    home = Path(home)
    if not (home / 'config.yaml').is_file():
        raise SetupError(f'{home} is not a Hermes home: it holds no config.yaml. Pass the right folder with --home.')
    source = home / 'hermes-agent'
    if not (source / 'hermes_cli' / 'main.py').is_file():
        raise SetupError(f'Hermes\'s own folder was not found inside {home} (no hermes-agent folder). The Account '
                         'Switcher supports Hermes installed by its official installer or install script.')
    desktop = source / DESKTOP
    if not desktop.is_file():
        raise SetupError(f'Hermes Desktop was not found at {desktop}. Install or build Hermes Desktop first.')
    return source, desktop


def existing_settings(home):
    """The settings already written, as plain data, or None. Settings that are there but not valid stop setup:
    they are never overwritten blindly."""
    target = settings_file(home)
    if not target.is_file():
        return None
    try:
        existing = json.loads(target.read_text(encoding='utf-8'))
        _parse_settings(json.dumps(existing).encode('utf-8'))
    except (OSError, ValueError, SettingsError):
        raise SetupError(f'{target} exists but is not valid. Fix or remove it, then run this again; '
                         'it is never overwritten blindly.') from None
    return existing


SERVICE_OF = ('$p={pid}; $s=@{{}}; Get-CimInstance Win32_Service -Filter "State=\'Running\'" | '
              'Where-Object {{ $_.ProcessId }} | ForEach-Object {{ $s[[int]$_.ProcessId]=$_.Name }}; '
              'for ($i=0; $i -lt 8 -and $p; $i++) {{ if ($s.ContainsKey([int]$p)) {{ $s[[int]$p]; break }}; '
              '$p=(Get-CimInstance Win32_Process -Filter "ProcessId=$p").ParentProcessId }}')


def gateway_service_found(home, run=subprocess.run, now=time.time):
    """Name of the Windows service that runs this Hermes's gateway, or None when Hermes starts its own.

    Read from the gateway's own status record and the process tree above it. A service hides its command
    line from an ordinary process, but not who its parent is. Nothing is changed."""
    try:
        record = json.loads((Path(home) / 'gateway_state.json').read_text(encoding='utf-8-sig'))
        pid = int(record['pid'])
        if record.get('gateway_state') not in ('running', 'draining'):
            return None
        # An old record's PID may belong to an unrelated process by now; a live gateway keeps its record fresh.
        stamp = datetime.fromisoformat(str(record['updated_at']).replace('Z', '+00:00'))
        if stamp.tzinfo is None or abs(now() - stamp.timestamp()) > 120:
            return None
        done = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', SERVICE_OF.format(pid=pid)],
                   capture_output=True, text=True, timeout=40,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        lines = [line.strip() for line in (done.stdout or '').splitlines() if line.strip()]
    except Exception:
        return None
    name = lines[-1] if lines else ''
    return name if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', name) else None


def signed_in_accounts(home):
    """[(email, hermes label)] for each distinct Codex account in the first profile's login store."""
    path = Path(home) / 'auth.json'
    try:
        rows = json.loads(path.read_text(encoding='utf-8-sig')).get('credential_pool', {}).get('openai-codex', [])
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise SetupError('The Hermes login store could not be read.') from None
    found, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        try:
            part = row['access_token'].split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
            email = claims['https://api.openai.com/profile']['email'].lower()
            account = claims['https://api.openai.com/auth']['chatgpt_account_id']
        except Exception:
            raise SetupError('A Codex login in Hermes could not be read. Sign in again with '
                             '"hermes auth add openai-codex", then run this again.') from None
        if account in seen:
            continue
        seen.add(account)
        found.append((email, str(row.get('label') or '')))
    return found


def token_owner(token):
    """The email Anthropic names as the owner of a Claude login."""
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/profile', headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20'})
    with urllib.request.urlopen(request, timeout=20) as answer:
        data = json.load(answer)
    return ((data.get('account') or {}).get('email') or '').lower()


def _json(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def claude_logins(home, owner=None, now=time.time):
    """[{key, name, email, directory, verified}] for each folder under ``claude-auth`` that holds a Claude login.

    ``verified`` says Anthropic named the owner just now. A login too old to ask with is listed under the
    email the folder remembers; the plugin asks Anthropic again before any switch to it."""
    root = Path(home) / 'claude-auth'
    rows = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        key = folder.name.lower()
        if not folder.is_dir() or not re.fullmatch(KEY, key):
            continue
        login = _json(folder / '.credentials.json').get('claudeAiOauth') or {}
        if not (login.get('accessToken') and login.get('refreshToken')):
            continue
        email, expires = '', login.get('expiresAt')
        if isinstance(expires, (int, float)) and expires / 1000 > now() + 60:
            try:
                email = (owner or token_owner)(login['accessToken'])
            except Exception:
                email = ''
        verified = bool(email)
        if not email:
            email = str((_json(folder / '.claude.json').get('oauthAccount') or {}).get('emailAddress') or '').lower()
        if not re.fullmatch(EMAIL, email):
            continue
        rows.append({'key': key, 'name': suggested_name(key, email), 'email': email,
                     'directory': folder.as_posix(), 'verified': verified})
    return rows


def key_for(label, email, taken):
    base = re.sub(r'[^a-z0-9]+', '-', (label or email.split('@')[0]).lower()).strip('-')
    if not re.fullmatch(KEY, base or ''):
        base = 'account'
    key, number = base, 2
    while key in taken:
        key, number = f'{base[:36]}-{number}', number + 1
    return key


def claude_account(text, home, taken):
    """``key=email`` from the command line into a Claude account whose login folder is Hermes's own."""
    key, sep, email = text.partition('=')
    key, email = key.strip().lower(), email.strip().lower()
    if not sep or not re.fullmatch(KEY, key) or not re.fullmatch(EMAIL, email):
        raise SetupError('A Claude account is given as key=email, for example personal=me@example.com; the key '
                         'is a short lowercase name.')
    if key in taken and str(taken[key].get('email', '')).lower() != email:
        raise SetupError(f'The Claude account key "{key}" is already used for another email.')
    if any(str(row.get('email', '')).lower() == email for name, row in taken.items() if name != key):
        raise SetupError(f'{email} is already set up as a Claude account under another key.')
    return key, taken.get(key) or {'label': key.replace('-', ' ').title(), 'email': email,
                                  'directory': (Path(home) / 'claude-auth' / key).as_posix()}


# The names Hermes gives a login that was added without one. They say how the login was made, not whose it is.
UNNAMED = {'', 'device_code', 'device-code', 'oauth', 'default', 'openai-codex'}


def suggested_name(label, email):
    """A name to offer for an account: the login's own name in Hermes, or the email's first part."""
    label = (label or '').strip()
    words = email.split('@')[0] if label.lower() in UNNAMED else label
    words = re.sub(r'[^A-Za-z0-9]+', ' ', words).strip().title()
    return usable_name(words[:24]) or 'Account'


def plan(home, gateway_service=None, existing=None, claude=(), name=None, logins=()):
    """The settings this installation needs. Accounts already in the settings keep their key and name.

    ``name(email, suggestion)`` gives the name for a Codex account that is new to the settings; ``logins`` are
    rows of :func:`claude_logins` to add, each under its ``name``."""
    home = Path(home)
    source, desktop = find_installation(home)
    existing = existing or {}
    codex = dict(existing.get('codex') or {})
    known = {str(row.get('email', '')).lower() for row in codex.values()}
    for email, label in signed_in_accounts(home):
        if email in known:
            continue
        offered = suggested_name(label, email)
        given = name(email, offered) if name else offered
        chosen = usable_name(given if str(given or '').strip() else offered)
        if chosen is None:
            raise SetupError(f'"{str(given)[:40]}" cannot be the name of {email}: {NAME_RULE}.')
        codex[key_for(chosen, email, codex)] = {'label': chosen, 'email': email}

    def written(name, found):
        # A path the user already wrote their own way stays as written when it names the same place.
        old = existing.get(name)
        return old if isinstance(old, str) and Path(old) == found else found.as_posix()

    data = {'hermes_root': written('hermes_root', home), 'hermes_source': written('hermes_source', source),
            'desktop_exe': written('desktop_exe', desktop),
            'gateway_service': gateway_service if gateway_service else existing.get('gateway_service'),
            'codex': codex, 'claude': dict(existing.get('claude') or {})}
    for text in claude:
        key, row = claude_account(text, home, data['claude'])
        data['claude'][key] = row
    for login in logins:
        email = str(login['email']).lower()
        if any(str(row.get('email', '')).lower() == email for row in data['claude'].values()):
            continue
        chosen = usable_name(login.get('name'))
        if chosen is None:
            raise SetupError(f'"{str(login.get("name"))[:40]}" cannot be the name of {email}: {NAME_RULE}.')
        key = login['key'] if login['key'] not in data['claude'] else key_for(login['key'], email, data['claude'])
        data['claude'][key] = {'label': chosen, 'email': email, 'directory': Path(login['directory']).as_posix()}
    if existing.get('gateway_health_url') and data['gateway_service']:
        data['gateway_health_url'] = existing['gateway_health_url']
    if not data['codex'] and not data['claude']:
        raise SetupError('No Codex account is signed in to Hermes yet. Sign in with "hermes auth add openai-codex" '
                         '(once per account), then run this again.')
    try:
        _parse_settings(json.dumps(data).encode('utf-8'))
    except SettingsError as exc:
        raise SetupError(str(exc)) from None
    return data


def write(data, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    temp.replace(target)


def found(home, owner=None):
    """What setup sees on this PC, for a screen to show before anything is written. No tokens, no writes."""
    home = Path(home)
    existing = existing_settings(home)
    find_installation(home)
    settings = existing or {}
    codex_known = {str(row.get('email', '')).lower() for row in (settings.get('codex') or {}).values()}
    claude_known = {str(row.get('email', '')).lower() for row in (settings.get('claude') or {}).values()}
    codex = [{'email': email, 'name': suggested_name(label, email), 'set_up': email in codex_known}
             for email, label in signed_in_accounts(home)]
    claude = [{'key': row['key'], 'email': row['email'], 'name': row['name'], 'verified': row['verified'],
               'set_up': row['email'] in claude_known} for row in claude_logins(home, owner)]
    command = Path(__file__).resolve().parent.parent / 'setup.cmd'
    return {'settings_present': existing is not None, 'settings_file': str(settings_file(home)),
            'setup_command': str(command) if command.is_file() else None,
            'gateway_service': gateway_service_found(home) or settings.get('gateway_service'),
            'codex': codex, 'claude': claude, 'name_rule': NAME_RULE}


def save(home, codex_names=None, claude_names=None, owner=None):
    """Write the settings for what :func:`found` lists, under the names given: ``{email: name}`` for Codex
    accounts and ``{folder key: name}`` for Claude logins. An account without a name gets the one offered."""
    home = Path(home)
    codex_names = {str(k).lower(): v for k, v in (codex_names or {}).items()}
    claude_names = {str(k).lower(): v for k, v in (claude_names or {}).items()}
    existing = existing_settings(home)
    logins = [{**row, 'name': claude_names.get(row['key']) or row['name']} for row in claude_logins(home, owner)]
    data = plan(home, gateway_service_found(home), existing, (),
                lambda email, offered: codex_names.get(email) or offered, logins)
    if existing != data:
        write(data, settings_file(home))
    return data
