"""Bounded Windows operations. Never returns credential/token values to callers."""
import json
import os
import queue
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import psutil
if __package__:  # imported by the dashboard API inside Hermes's web server
    from .switch_core import (CLAUDE, EMAILS, PROVIDERS, SwitchError, WrongAccount, effective, health_warning, identify,
                              lapsed_note, selected, summaries)
    from .settings import SettingsError, load_settings, settings_snapshot, ensure_settings_unchanged
    from .compat import (UnsupportedRuntime, require_switch_interfaces, committed_venv, drain_control, safe_yaml_load,
                         gateway_hosts_desktop, read_gateway_record)
    from .worker_launch import clean_env
else:  # the helper scripts and the test suite, with dashboard/ on their own sys.path
    from switch_core import (CLAUDE, EMAILS, PROVIDERS, SwitchError, WrongAccount, effective, health_warning, identify, lapsed_note,
                             selected, summaries)
    from settings import SettingsError, load_settings, settings_snapshot, ensure_settings_unchanged
    from compat import (UnsupportedRuntime, require_switch_interfaces, committed_venv, drain_control, safe_yaml_load,
                        gateway_hosts_desktop, read_gateway_record)
    from worker_launch import clean_env

ROOT = Path(os.environ.get('HERMES_HOME') or Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'hermes')
EXE = ROOT / 'hermes-agent/apps/desktop/release/win-unpacked/Hermes.exe'


def runtime_python():
    """Resolve the active package-managed environment only at switch time."""
    try:
        environment = committed_venv(load_settings().hermes_source)
        python = environment / 'Scripts/python.exe' if environment else None
        if python is None or not python.is_file():
            raise RuntimeError()
        return python
    except Exception:
        raise SwitchError('The active Hermes Python runtime is unavailable; switching is unsupported on this installation.') from None


RECEIPT = ROOT / 'logs/account-switcher-last.json'
LOCK = ROOT / 'logs/account-switcher.lock'
NS = '/api/plugins/account-switcher'
ENV_SET = Path(__file__).resolve().with_name('env_set.py')
WRITER = Path(__file__).resolve().with_name('hermes_writer.py')

# Claude: the DirectSDK plugin hands the `claude` CLI a login folder as CLAUDE_CONFIG_DIR on every spawn. It takes
# the folder from its per-profile `claude_config_dir` setting in config.yaml (provider 0.3.1 on, re-read on every
# spawn), else from this variable in the process environment (read from .env only when the gateway starts).
# A switch writes both, to the same Hermes-owned folder.
CLAUDE_VAR = 'CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR'
CLAUDE_AUTH = ROOT / 'claude-auth'
CLAUDE_CLI = 'claude'
# The provider's folder and manifest name; Hermes's catalog lists it without the suffix.
CLAUDE_PROVIDER = 'claude-subscription-directsdk-experimental'
CLAUDE_PROVIDER_NAMES = (CLAUDE_PROVIDER, 'claude-subscription-directsdk')
CLAUDE_SETTING_PATH = ('plugins', 'entries', CLAUDE_PROVIDER, 'settings', 'claude_config_dir')
CLAUDE_SETTING = '.'.join(CLAUDE_SETTING_PATH)
# The first provider version that reads `claude_config_dir` on every spawn, so a switch needs no restart.
LIVE_CLAUDE_PROVIDER = (0, 3, 1)
_PATH_LOCK = threading.RLock()


def configure_paths(settings=None):
    """Bind every mutable runtime path from the same validated settings object."""
    global ROOT, EXE, RECEIPT, LOCK, CLAUDE_AUTH
    settings = settings or load_settings()
    ROOT = settings.hermes_root
    EXE = settings.desktop_exe
    RECEIPT = ROOT / 'logs/account-switcher-last.json'
    LOCK = ROOT / 'logs/account-switcher.lock'
    CLAUDE_AUTH = ROOT / 'claude-auth'
    return settings


@contextmanager
def bound_settings():
    """Serialize path rebinding within an API process and pin all reads in this operation."""
    with _PATH_LOCK:
        with settings_snapshot() as settings:
            configure_paths(settings)
            yield settings


try:
    configure_paths()
except SettingsError:
    pass  # A fresh install imports and displays setup guidance without side effects.


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    # Windows readers do not grant delete sharing. A concurrent status poll can
    # briefly prevent replacement even though both files are writable. Keep the
    # old receipt intact and retry only the replacement, with a one-second cap.
    for attempt in range(21):
        try:
            temp.replace(path)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 20:
                raise
            time.sleep(.05)


def accounts():
    try:
        data = json.loads((ROOT / 'auth.json').read_text(encoding='utf-8-sig'))
        return identify(data.get('credential_pool', {}).get('openai-codex'))
    except SwitchError:
        raise
    except Exception:
        raise SwitchError('The root Codex account store could not be read.') from None


class LocalOperationFailed(SwitchError):
    """One of Hermes's own commands failed. Its output is never shown, so the receipt names the stage."""


BEFORE_CLOSE = ('accepted', 'checking')
AFTER_CLOSE = ('restarting', 'applying', 'reopening', 'verifying')
PRINCIPAL = 'account-switcher'


def clear_own_drains():
    """Remove this plugin's gateway drain markers and nobody else's. False when they could not be checked."""
    try:
        drain = drain_control()
        for home in profile_homes():
            if (drain.read_drain_request(home=home) or {}).get('principal') == PRINCIPAL:
                drain.clear_drain_request(home=home)
        return True
    except Exception:
        return False


def recover_abandoned_check():
    """Clear only a switch whose worker is proved dead; uncertainty stays blocked.

    A worker that died before Hermes was closed changed nothing. One that died afterwards (a crash, a power
    cut) leaves Hermes closed and may have reordered some stores; nothing here guesses at a repair. The lock
    and this plugin's own drain markers go, and the receipt says where the switch stopped, so the stores'
    real order is shown and the next switch can run.
    """
    try:
        raw = LOCK.read_bytes()
        lock = json.loads(raw)
        record = json.loads(RECEIPT.read_text())
        if (time.time() - LOCK.stat().st_mtime < 30
                or record.get('operation_id') != lock.get('operation_id')
                or not lock.get('operation_id')
                or record.get('state') not in BEFORE_CLOSE + AFTER_CLOSE
                or type(lock.get('pid')) is not int
                or lock['pid'] <= 0
                or psutil.pid_exists(lock['pid'])):
            return
        # The launcher can die after handing off to a worker. The switch endpoint
        # launches pythonw.exe exclusively; unrelated service python.exe processes
        # often hide their command lines and must not strand this interactive check.
        for process in psutil.process_iter(['name', 'cmdline']):
            if (process.info['name'] or '').lower() != 'pythonw.exe':
                continue
            args = process.info['cmdline']
            if args is None or any(str(arg).endswith('switch_worker.py') for arg in args):
                return
        if LOCK.read_bytes() != raw:
            return
        # Exclusive per-operation claim across all Desktop backend processes.
        # A second recovery cannot race this one and remove a newly-created lock.
        archive = LOCK.with_name(LOCK.name + '.' + lock['operation_id'] + '.abandoned')
        os.link(LOCK, archive)
        if LOCK.read_bytes() != raw:
            return
        stage = record['state']
        markers_checked = clear_own_drains()
        if stage in BEFORE_CLOSE:
            message = 'An interrupted account check was cleared. No switch was completed; recheck before trying again.'
        elif record.get('restarts') is False:
            message = (f'The last account switch was interrupted during {stage}; Hermes was not restarted. '
                       'Check which account is selected before switching again.')
            record['interrupted_during'] = stage
        else:
            message = (f'The last account switch was interrupted during {stage}, after Hermes had been closed. '
                       'Check which account is selected before switching again.')
            record['interrupted_during'] = stage
            if record.get('old_gateway_pid'):
                try:
                    stopped = service()['status'] != 'running'
                except (SwitchError, SettingsError):
                    stopped = False
                if stopped:
                    message += ' The Hermes gateway is not running; start it again.'
        if not markers_checked:
            message += ' The gateway restart markers could not be checked.'
        record.update(state='failed', finished_at=time.time(), message=message)
        atomic(RECEIPT, record)
        LOCK.unlink()
    except (OSError, ValueError, TypeError, psutil.Error):
        return


def home_name(home):
    """The name a person knows a store by: Hermes's first profile is "default", not its folder's name."""
    return 'default' if Path(home) == ROOT else Path(home).name


def profile_homes():
    # A profile Hermes marked deleted (profiles/.deleted/<name>) refuses every write while its folder lingers.
    profiles = ROOT / 'profiles'
    if not profiles.is_dir():
        return [ROOT]
    return [ROOT] + sorted(p for p in profiles.iterdir()
                           if (p / 'config.yaml').is_file() and not (profiles / '.deleted' / p.name).exists())


def _plugin_flags(home):
    """(enabled, disabled) name sets from one home's config.yaml, or None when unreadable.

    Mirrors the stock loader: ``plugins.disabled`` is a deny-list that wins over
    ``plugins.enabled``, and non-list values read as empty. Unparseable or non-mapping
    configuration is malformed, not empty.
    """
    try:
        config = safe_yaml_load((home / 'config.yaml').read_text(encoding='utf-8-sig'))
    except UnsupportedRuntime:
        raise
    except Exception:
        return None
    if not isinstance(config, dict):
        return None
    flags = config.get('plugins') or {}
    if not isinstance(flags, dict):
        return None

    def names(value):
        return set(value) if isinstance(value, list) else set()

    return names(flags.get('enabled')), names(flags.get('disabled'))


def _effectively_enabled(flags, name):
    enabled, disabled = flags
    return name in enabled and name not in disabled


# Hermes Desktop does not run desktop/plugin.js from here: it copies it into <home>/desktop-plugins/ when
# Desktop starts, and records the source's modified time beside the copy. An install or update while Desktop
# is open leaves Desktop showing the previous dialog until Desktop is restarted; a gateway restart does not
# refresh it. Seen on the owner's PC on 2026-10-21.
STALE_DESKTOP_TEXT = ('Hermes Desktop is still showing the previous version of this window. Close Hermes Desktop '
                      'completely and open it again, then switch.')
PACKAGE_DIR = Path(__file__).resolve().parent.parent / 'desktop'


def stale_desktop_blockers(root=None, source=None):
    """A blocker when Desktop's own copy of this plugin's dialog is older than the installed one.

    Says so only on proof: Desktop's package record names this install as its source, the copy differs from
    the installed file, and the record's time is older than the installed file. Anything missing or
    unreadable is not proof, so a Desktop that stores its copy differently never blocks a switch."""
    root = Path(root or ROOT)
    source = Path(source or PACKAGE_DIR)
    mirror = root / 'desktop-plugins' / 'account-switcher'
    try:
        record = json.loads((mirror / '.hermes-package.json').read_text(encoding='utf-8-sig'))
        if Path(record['source']).resolve() != source.resolve():
            return []
        recorded = float(record['sourceMtimeMs'])
        installed = source / 'plugin.js'
        if (mirror / 'plugin.js').read_bytes() == installed.read_bytes():
            return []
        newest = min(source.stat().st_mtime, installed.stat().st_mtime) * 1000
    except (OSError, ValueError, KeyError, TypeError):
        return []
    # Two seconds of slack for file systems that round modified times.
    return [STALE_DESKTOP_TEXT] if recorded + 2000 < newest else []


def plugin_profile_blockers():
    """Every discovered profile must opt into the installed backend before a restart."""
    plugin = ROOT / 'plugins/account-switcher/dashboard/manifest.json'
    if not plugin.is_file():
        return ['The Account Switcher backend package is missing from the Hermes root.']
    blockers = []
    try:
        for home in profile_homes():
            flags = _plugin_flags(home)
            if flags is None:
                blockers.append(f'{home_name(home)}: Account Switcher plugin configuration is unreadable.')
            elif not _effectively_enabled(flags, 'account-switcher'):
                # `hermes plugins enable` only knows a plugin that is installed in that profile's own folder.
                flag = '' if Path(home) == ROOT else f'-p {home.name} '
                blockers.append(f'{home_name(home)}: Account Switcher is not enabled in this profile. Install it '
                                f'into this profile\'s folder, run "hermes {flag}plugins enable account-switcher", '
                                'then restart Hermes Desktop.')
    except UnsupportedRuntime as exc:
        return [str(exc)]
    return blockers


def claude_provider_blockers():
    """Claude switching needs the external provider plugin installed in the first profile; a .env readback
    cannot prove a consumer exists. It is a model provider, which Hermes loads without an ``enabled`` entry,
    so only an explicit ``disabled`` entry counts against it. Codex-only configuration is never blocked here."""
    if not CLAUDE:
        return []
    if not any((ROOT / 'plugins' / name).is_dir() for name in CLAUDE_PROVIDER_NAMES):
        return ['The Claude subscription provider plugin is not installed; Claude switching would have no '
                'effect. Install it with "hermes plugins install claude-subscription-directsdk".']
    blockers = []
    try:
        for home in profile_homes():
            flags = _plugin_flags(home)
            if flags is None:
                blockers.append(f'{home_name(home)}: the Claude subscription provider plugin cannot be checked; '
                                'the profile configuration is unreadable.')
            elif flags[1] & set(CLAUDE_PROVIDER_NAMES):
                blockers.append(f'{home_name(home)}: the Claude subscription provider plugin is disabled.')
    except UnsupportedRuntime as exc:
        blockers.append(str(exc))
    return blockers


def codex_stores():
    """(home, accounts) for root and every profile that owns Codex logins.

    Since upstream #111724 a named profile never reads root's store, so each one
    holds its own two logins and a switch must reorder every store. A profile with
    no Codex rows does not run Codex and is skipped. Never copy grants between stores.
    """
    if not EMAILS:
        return []
    root_acc = accounts()
    root_ids = {key: item['account_id'] for key, item in root_acc.items()}
    stores = [(ROOT, root_acc)]
    for home in profile_homes()[1:]:
        path = home / 'auth.json'
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            raise SwitchError(f'Cannot read the Codex logins for {home.name}.') from None
        rows = data.get('credential_pool', {}).get('openai-codex', [])
        if not rows:
            continue
        try:
            acc = identify(rows)
        except SwitchError as exc:
            raise SwitchError(f'{home.name}: {exc}') from None
        if {key: item['account_id'] for key, item in acc.items()} != root_ids:
            raise SwitchError(f'{home.name} holds different Codex accounts than the root profile.')
        stores.append((home, acc))
    return stores


def common_selection(stores):
    """The account every store tries first, or None when any store disagrees."""
    if not stores:
        return None
    picks = {selected(acc) for _, acc in stores}
    return picks.pop() if len(picks) == 1 else None


def usage(item):
    req = urllib.request.Request('https://chatgpt.com/backend-api/wham/usage', headers={
        'Authorization': 'Bearer ' + item['entry']['access_token'],
        'ChatGPT-Account-Id': item['account_id'], 'User-Agent': 'Hermes account switch'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.load(r)
            if r.status != 200 or (body.get('rate_limit') or {}).get('allowed') is not True:
                raise SwitchError('The selected Codex account is not currently available.')
    except SwitchError:
        raise
    except Exception:
        raise SwitchError('Could not confirm the selected Codex account with OpenAI.') from None


# ---------------------------------------------------------------- Claude stores

def claude_dir(key):
    return load_settings().claude[key].directory


def env_values(home):
    """KEY -> value of ``home/.env`` (our key is written unquoted with forward slashes; quotes are tolerated)."""
    path = home / '.env'
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _same_dir(value, key):
    try:
        return Path(value).resolve() == claude_dir(key).resolve()
    except (OSError, ValueError):
        return False


def claude_setting(home):
    """The provider's ``claude_config_dir`` setting in ``home``'s config.yaml, trimmed; '' when not set, None
    when the configuration cannot be read."""
    try:
        value = safe_yaml_load((home / 'config.yaml').read_text(encoding='utf-8-sig'))
    except Exception:  # UnsupportedRuntime included: the status view shows "mixed"; preflight names the cause.
        return None
    if not isinstance(value, dict):
        return None
    for segment in CLAUDE_SETTING_PATH:
        if not isinstance(value, dict) or segment not in value:
            return ''
        value = value[segment]
    return value.strip() if isinstance(value, str) else ''


def claude_folder_named(home):
    """The folder the provider hands `claude` in ``home``: the setting wins, then the .env variable (its own
    precedence). None when neither names one; ``False`` when the configuration is unreadable."""
    setting = claude_setting(home)
    if setting is None:
        return False
    return setting or env_values(home).get(CLAUDE_VAR) or None


def claude_provider_version():
    """The installed Claude provider's version as a tuple of ints, or None when it cannot be read."""
    for name in CLAUDE_PROVIDER_NAMES:
        try:
            manifest = safe_yaml_load((ROOT / 'plugins' / name / 'plugin.yaml').read_text(encoding='utf-8-sig'))
            match = re.match(r'\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?', str(manifest['version']))
            if match:
                return tuple(int(part or 0) for part in match.groups())
        except Exception:  # unreadable: the switch keeps the restart
            continue
    return None


def live_switch_possible(request):
    """True when this switch can apply without restarting anything: the gateway runs every turn (unified
    runtime) and reloads the Codex pool each turn, and, for Claude, the provider reads its setting per spawn."""
    if not gateway_hosts_desktop():
        return False
    if request.get('claude'):
        version = claude_provider_version()
        return version is not None and version >= LIVE_CLAUDE_PROVIDER
    return True


def claude_selection():
    """Which Hermes-owned Claude folder every store names: ``{'selected', 'state', 'stores'}``.

    ``state`` is ``unset`` (no store names one: the plugin falls back to the CLI's own ``~/.claude``),
    ``set`` (every store names the same account) or ``mixed`` (stores disagree or name a foreign folder).
    """
    stores = {}
    for home in profile_homes():
        value = claude_folder_named(home)
        if value is False:
            stores[home.name] = 'foreign'
        elif not value:
            stores[home.name] = None
        else:
            stores[home.name] = next((key for key in CLAUDE if _same_dir(value, key)), 'foreign')
    picks = set(stores.values())
    if picks == {None}:
        return {'selected': None, 'state': 'unset', 'stores': stores}
    if len(picks) == 1 and 'foreign' not in picks:
        return {'selected': picks.pop(), 'state': 'set', 'stores': stores}
    return {'selected': None, 'state': 'mixed', 'stores': stores}


class CredentialsUnreadable(Exception):
    """The login file is there but cannot be read right now: locked or half-written while the CLI refreshes it."""


def _credentials(key, attempts=3, pause=0.2):
    """The folder's OAuth block: ``{}`` when no login file exists, ``CredentialsUnreadable`` when one exists
    but cannot be read. Claude Code rewrites the file when it refreshes a token, and on Windows a reader can
    hit it locked or half-written, so a few short retries come before "unreadable"."""
    path = claude_dir(key) / '.credentials.json'
    for attempt in range(attempts):
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('not a JSON object')
            oauth = data.get('claudeAiOauth') or {}
            return oauth if isinstance(oauth, dict) else {}
        except (OSError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(pause)
    raise CredentialsUnreadable(key)


def _cached_label(key):
    path = claude_dir(key) / '.claude.json'
    try:
        return ((json.loads(path.read_text(encoding='utf-8')).get('oauthAccount') or {}).get('emailAddress') or '').lower()
    except (OSError, ValueError):
        return ''


def sign_in_expired(cred):
    """True when the login's refresh token is past its own expiry (``refreshTokenExpiresAt``, set at sign-in).
    Anthropic then refuses any renewal, and Claude Code empties the login when one is tried, so the account
    needs a new sign-in. A login without the field is not judged here."""
    limit = cred.get('refreshTokenExpiresAt')
    return isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0 and limit / 1000 <= time.time()


def claude_accounts():
    """Cheap, token-free summary for status polling."""
    rows = []
    for key, (label, email) in CLAUDE.items():
        try:
            cred, unreadable = _credentials(key, attempts=2, pause=0.1), False
        except CredentialsUnreadable:
            cred, unreadable = {}, True
        rows.append({'key': key, 'label': label, 'email': email,
                     'logged_in': bool(cred.get('accessToken') and cred.get('refreshToken')) and not sign_in_expired(cred),
                     # True: a login file exists but was locked or mid-write; not the same as signed out.
                     'unreadable': unreadable,
                     'cached_email': _cached_label(key) or None})
    return rows


def _oauth_profile(token):
    req = urllib.request.Request('https://api.anthropic.com/api/oauth/profile', headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20'})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    return ((data.get('account') or {}).get('email') or '').lower()


def claude_cli(path=None):
    """The `claude` command: where the provider plugin was told to find it, then PATH, then the folder
    Anthropic's own installer uses (it does not put that folder on PATH)."""
    told = os.environ.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND')
    if told and Path(told).is_file():
        return told
    usual = Path.home() / '.local' / 'bin' / 'claude.exe'
    return shutil.which(CLAUDE_CLI, path=path) or (str(usual) if usual.is_file() else None)


def _handshake(key, timeout=60):
    """The CLI's free `initialize` handshake against a dead upstream: lets the CLI refresh its own token."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('ANTHROPIC_') and not k.startswith('CLAUDE_')}
    env.update(CLAUDE_CONFIG_DIR=str(claude_dir(key)), ANTHROPIC_BASE_URL='http://127.0.0.1:9',
               DISABLE_TELEMETRY='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    argv = [CLAUDE_CLI, '-p', '--model', 'sonnet', '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--verbose', '--tools', '', '--setting-sources', '', '--strict-mcp-config', '--mcp-config',
            '{"mcpServers":{}}', '--disable-slash-commands', '--no-session-persistence']
    handshake = json.dumps({'type': 'control_request', 'request_id': 'switch', 'request': {'subtype': 'initialize'}}) + '\n'
    exe = claude_cli(env.get('PATH'))
    if exe is None:
        return 'Claude Code was not found on this PC'
    argv[0] = exe
    try:
        with tempfile.TemporaryDirectory(prefix='claude-switch-handshake-') as cwd:
            subprocess.run(argv, input=handshake, env=env, cwd=cwd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout, **hidden_console())
    except subprocess.TimeoutExpired:
        return 'Claude Code did not finish renewing it in time'
    except (OSError, subprocess.SubprocessError) as exc:
        return f'Claude Code could not be started ({type(exc).__name__})'
    return None


def claude_identity(key):
    """Prove which account owns the login in the Hermes folder for ``key``.

    The token owner must be verified with Anthropic. An expired token gets one
    CLI refresh attempt. A cached folder label is never evidence of ownership.
    """
    label, expected = CLAUDE[key]
    busy = SwitchError(f'The {label} Claude login file could not be read just now (Claude Code may be refreshing it); '
                       'wait a moment and recheck.')
    try:
        cred = _credentials(key)
    except CredentialsUnreadable:
        raise busy from None
    if not (cred.get('accessToken') and cred.get('refreshToken')):
        raise SwitchError(f'The {label} Claude login is missing; sign it in with "setup.cmd claude {key}".')

    if sign_in_expired(cred):
        when = time.strftime('%b %d, %Y', time.localtime(cred['refreshTokenExpiresAt'] / 1000))
        raise SwitchError(f'The {label} Claude sign-in expired on {when}; Anthropic asks for a new sign-in about four weeks '
                          f'after the last one. Sign it in again with "setup.cmd claude {key}".')

    def expired():
        exp = cred.get('expiresAt')
        return not isinstance(exp, (int, float)) or exp / 1000 <= time.time() + 60

    # Why it could not be verified, named in the message so a receipt tells an expired sign-in from a
    # network problem. Only an exception's type or an HTTP status is named, never response text.
    reason = None
    if expired():
        problem = _handshake(key)
        try:
            cred = _credentials(key)
        except CredentialsUnreadable:
            raise busy from None
        if not cred.get('refreshToken'):
            # Claude Code empties the login itself when Anthropic answers its renewal with invalid_grant: the
            # saved sign-in was already refused (revoked, expired or used elsewhere). Nothing here wrote it.
            raise SwitchError(f'Anthropic no longer accepts the saved {label} Claude sign-in, so Claude Code signed it '
                              f'out while renewing it. Sign it in again with "setup.cmd claude {key}".')
        if expired():
            reason = f'its sign-in had expired and {problem or "Claude Code did not renew it"}'
    if not expired():
        email = ''
        for attempt in range(2):  # One network blip must not read as an unverifiable login.
            try:
                email = _oauth_profile(cred['accessToken'])
                break
            except Exception as exc:
                code = getattr(exc, 'code', None)
                reason = (f'Anthropic answered HTTP {code}' if isinstance(code, int)
                          else f'Anthropic could not be reached ({type(exc).__name__})')
                if attempt == 0:
                    time.sleep(2)
        if email:
            if email != expected:
                raise WrongAccount(f'The {label} Claude folder is logged in as {email}, not {expected}; sign it in again with "setup.cmd claude {key}".')
            return {'email': email, 'method': 'token'}
        reason = reason or 'Anthropic returned no account'
    raise SwitchError(f'The {label} Claude login could not be verified with Anthropic ({reason}); recheck, and if this '
                      f'stays sign it in again with "setup.cmd claude {key}".')


def claude_value(key):
    # Forward slashes: valid for Windows and Node, and immune to .env quote/backslash handling.
    return claude_dir(key).resolve().as_posix()


def apply_claude(key, allow_changed=False, writer=None, record=None):
    """Point every store at the folder for ``key``, in both places the provider reads: its ``claude_config_dir``
    setting (applies to the next spawn) and the .env variable (applies after a gateway start, and to providers
    before 0.3.1). ``'unset'`` clears both (plugin default)."""
    # Each profile's .env and config.yaml are its own; profiles are written side by side, and within one the
    # .env first, then the setting, as before.
    if not allow_changed:
        ensure_settings_unchanged()
    value = '--remove' if key == 'unset' else claude_value(key)

    def write(home):
        command([str(runtime_python()), str(ENV_SET), CLAUDE_VAR, value], home=home)
        command([str(runtime_python()), '-m', 'hermes_cli.main', 'config', 'set', CLAUDE_SETTING,
                 '' if key == 'unset' else value], home=home)
    homes = profile_homes()
    job = {'claude': {'homes': [str(home) for home in homes], 'variable': CLAUDE_VAR, 'setting': CLAUDE_SETTING,
                      'value': None if key == 'unset' else value}}
    if not written_by_hermes(writer, job, record):
        side_by_side(write, homes)
    if not allow_changed:
        ensure_settings_unchanged()
    state = claude_selection()
    if key == 'unset':
        if state['state'] != 'unset':
            raise SwitchError('Removing the Claude selection did not clear every profile store.')
    elif state['state'] != 'set' or state['selected'] != key:
        raise SwitchError('Saved Claude selection readback does not match the requested account in every profile.')


# ---------------------------------------------------------------- Desktop and gateway

def desktop():
    found = []
    for p in psutil.process_iter(['exe', 'cmdline']):
        try:
            if p.info['exe'] and Path(p.info['exe']).resolve() == EXE.resolve() and not any(
                    arg.startswith('--type=') for arg in (p.info['cmdline'] or [])):
                found.append(p)
        except (psutil.Error, OSError):
            continue
    if len(found) != 1:
        raise SwitchError('Expected exactly one canonical Hermes Desktop instance.')
    return found[0]


def backends(main):
    found = []
    for p in main.children(recursive=True):
        try:
            cmd = p.cmdline()
            # A stock single-profile Desktop starts its backend with no --profile at all.
            if 'serve' not in cmd:
                continue
            profile = cmd[cmd.index('--profile') + 1] if '--profile' in cmd[:-1] else 'default'
            # Desktop now uses the managed entry point, directly or via Python.
            # Match executable positions, not substrings in an agent's shell command.
            managed = (ROOT / 'hermes-agent/.hermes/bin/hermes.exe').resolve()
            python = Path(cmd[0]).name.lower() in ('python.exe', 'pythonw.exe', 'python')
            args = cmd[2:] if python and cmd[1:2] == ['-I'] else cmd[1:]
            module_launch = python and args[:2] == ['-m', 'hermes_cli.main']
            managed_launch = (Path(cmd[0]).resolve() == managed or
                              (python and args and Path(args[0]).resolve() == managed))
            if not (module_launch or managed_launch):
                continue
            token = p.environ().get('HERMES_DASHBOARD_SESSION_TOKEN')
            ports = {c.laddr.port for c in p.net_connections(kind='inet') if c.status == psutil.CONN_LISTEN}
            if not ports:
                continue
            if not token:
                raise SwitchError('A Desktop backend has no verifiable authenticated control channel.')
            # Windows venv launcher and real child can share argv; only the listener is a backend.
            matched = False
            for port in ports:
                try:
                    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/status',
                        headers={'Authorization': 'Bearer ' + token})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        probe = json.load(response)
                    if 'gateway_state' not in probe:
                        continue
                except Exception:
                    continue
                found.append({'pid': p.pid, 'born': p.create_time(), 'port': port, 'token': token,
                              'profile': profile})
                matched = True
                break
            if not matched:
                raise SwitchError('A Desktop backend has no responding dashboard endpoint.')
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise SwitchError('Cannot inspect a Desktop backend safely.') from None
    if not found:
        raise SwitchError('No live Desktop backends were found.')
    return found


def call(backend, route, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{backend['port']}{NS}{route}", data=data,
        headers={'Authorization': 'Bearer ' + backend['token'], 'Content-Type': 'application/json'},
        method='GET' if body is None else 'POST')
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)
    except Exception as exc:
        failure = SwitchError(f"The account-switch plugin is not responding in {backend['profile']}.")
        failure.transport_details = {'type': type(exc).__name__}
        for name in ('code', 'errno', 'winerror'):
            value = getattr(exc, name, None)
            if isinstance(value, int):
                failure.transport_details[name] = value
        raise failure from None


def stock_gateways():
    """Live `hermes ... gateway run` processes. Stock Hermes has no Windows service to ask."""
    found = []
    for process in psutil.process_iter(['pid', 'cmdline', 'create_time']):
        args = [str(arg).lower() for arg in process.info['cmdline'] or []]
        if 'gateway' in args[:-1] and args[args.index('gateway') + 1] == 'run' and any('hermes' in arg for arg in args):
            found.append(process.info)
    return found


def service():
    name = load_settings().gateway_service
    if name is None:
        live = sorted(stock_gateways(), key=lambda info: info['create_time'])
        if not live:
            return {'status': 'stopped', 'pid': None, 'born': None}
        # A venv launcher parent shares the worker's command line; the worker is the younger one.
        return {'status': 'running', 'pid': live[-1]['pid'], 'born': live[-1]['create_time']}
    try:
        return psutil.win_service_get(name).as_dict()
    except Exception:
        raise SwitchError('Cannot inspect the Hermes gateway Windows service.') from None


def gateway_in_use():
    """False only on stock Hermes with no gateway running. A fresh install has none until the user sets up
    messaging, so a Desktop-only switch restarts Desktop alone. A declared service is always in use."""
    return load_settings().gateway_service is not None or service()['status'] == 'running'


def gateway_owner(pid):
    """Name of the running Windows service whose process tree holds `pid`, or None. A process's parent is
    readable even where a service hides its command line from an ordinary process."""
    try:
        services = {}
        for item in psutil.win_service_iter():
            try:
                info = item.as_dict()
            except Exception:
                continue
            if info.get('status') == 'running' and info.get('pid'):
                services[info['pid']] = info['name']
        process = psutil.Process(pid)
        for _ in range(8):
            if process is None:
                return None
            if process.pid in services:
                return services[process.pid]
            process = process.parent()
    except Exception:
        return None
    return None


def service_owns_gateway(name):
    """True only when the declared Windows service is running and the gateway that published the root ledger is
    that service's own process. Hermes Desktop starts a gateway of its own when it finds none, so a gateway that
    answers after a restart may be Desktop's, not the service's; a service that cannot start beside it stops.
    Ownership, not activity, is asked here, so an old heartbeat does not matter; a recycled PID does."""
    try:
        if service()['status'] != 'running':
            return False
        record = gateway_record(ROOT)
        return (record is not None and record.alive and record.state == 'running'
                and gateway_owner(record.pid) == name)
    except Exception:
        return False


def gateway_setting_blockers():
    """Settings that name no Windows service are right only where the command-line search sees every live
    gateway. A gateway run by a service hides its command line; read as absent, a switch would restart Hermes
    Desktop alone and leave the gateway on the previous account."""
    if load_settings().gateway_service is not None:
        return []
    seen = {info['pid'] for info in stock_gateways()}
    try:
        homes = profile_homes()
    except UnsupportedRuntime as exc:
        return [str(exc)]
    for home in homes:
        try:
            record = gateway_record(home)
        except Exception:
            # Unreadable is not a gateway here; a switch that needs the gateway refuses on it in gateway_states.
            continue
        # An old record's PID may belong to an unrelated process by now; a live gateway keeps its record fresh.
        if record is None or not record.alive or not record.fresh or record.state not in ('running', 'draining'):
            continue
        pid = record.pid
        if pid in seen:
            continue
        owner = gateway_owner(pid)
        if owner:
            return [f'The Hermes gateway on this PC runs as the Windows service {owner}, but the account-switch '
                    f'settings say Hermes starts its own gateway. Run "setup.cmd service {owner}", then restart '
                    'Hermes Desktop.']
        return ['A Hermes gateway is running that the Account Switcher cannot inspect: it was started by a '
                'service or by another account. Switching is refused rather than leave that gateway on the '
                'previous account.']
    return []


def gateway_identity(state):
    """(pid, birth time): a recycled PID is a different process. A Windows service reports no birth time."""
    return state['pid'], state.get('born')


def hermes_launcher():
    """Hermes's own command. Running `hermes_cli` through the runtime Python skips the launcher's environment
    setup, and a gateway started that way exits at once ("no dependency environment is committed")."""
    settings = load_settings()
    for path in (settings.hermes_root / 'bin' / 'hermes.exe', settings.hermes_source / '.hermes' / 'bin' / 'hermes.exe'):
        if path.is_file():
            return path
    raise SwitchError('The Hermes command was not found; switching is unsupported on this installation.')


def gateway_healthy():
    url = load_settings().gateway_health_url
    if url:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.load(r).get('status') == 'ok'
    # Stock Hermes serves no health endpoint by default; its own fresh ledger is the health record.
    live = {info['pid'] for info in stock_gateways()}
    return any(home == ROOT and d['pid'] in live and d['gateway_state'] == 'running' for home, d in gateway_states())


def gateway_command(verb, home):
    """Hermes's own lifecycle verb for one home. Its exit code is never trusted: `start` reports failure
    in text only, so callers verify the process. Never `--all`: on Windows that kills without draining."""
    try:
        subprocess.run([str(hermes_launcher()), 'gateway', verb], timeout=120,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       **hidden_console(),
                       env={**clean_env(home), 'HERMES_NONINTERACTIVE': '1',
                            # A bare start must not install a login task on the user's behalf.
                            'HERMES_GATEWAY_INSTALL_START_ON_LOGIN': '0'})
    except (OSError, subprocess.SubprocessError):
        raise SwitchError(f'Hermes could not run its own gateway {verb}.') from None


# Hermes's own drift allowance for its start-time fingerprint (gateway/status.py): 2 s in centiseconds.
START_TIME_TOLERANCE = 200
# Centiseconds since the epoch for a process started between 2001 and 2286. Hermes on Windows writes this scale;
# anything outside it (seconds, milliseconds, Linux clock ticks since boot) is a unit this check cannot compare.
CENTISECOND_STAMPS = range(10 ** 11, 10 ** 12)


def recycled_pid(pid, recorded):
    """True only when there is proof that `pid` now belongs to another program than the gateway that wrote the record.

    A gateway killed without cleanup leaves its record saying "running", and Windows soon hands its PID to
    something unrelated. Hermes stamps `start_time` as the process creation time in centiseconds (its own
    PID-reuse guard), so a different creation time on the same scale proves a dead record.

    This fails closed: a record ruled recycled is ignored, and ignoring a live gateway would leave it on the
    previous account. So a missing stamp, a stamp in another unit, or a process that cannot be inspected is
    never proof; the record stays in the check, where an old heartbeat refuses the switch."""
    if not isinstance(recorded, int) or isinstance(recorded, bool) or recorded not in CENTISECOND_STAMPS:
        return False
    try:
        return abs(round(psutil.Process(pid).create_time() * 100) - recorded) > START_TIME_TOLERANCE
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False


def gateway_record(home):
    """read_gateway_record plus `alive`: the PID is still the process that wrote the record. None when the home has
    no record; raises when it cannot be read. Every gateway_state.json read goes through here, or through
    read_gateway_record where psutil is unavailable (first_run)."""
    record = read_gateway_record(home)
    if record is None:
        return None
    alive = psutil.pid_exists(record.pid) and not recycled_pid(record.pid, record.data.get('start_time'))
    return replace(record, alive=alive)


def gateway_states(require_drain=False):
    states = []
    for home in profile_homes():
        try:
            record = gateway_record(home)
            if record is None or not record.alive:
                continue
            if record.state not in ('running', 'draining'):
                raise ValueError()
            active = record.data.get('active_agents')
            if not isinstance(active, int) or isinstance(active, bool) or active < 0:
                raise ValueError()
            # A live PID with an old ledger cannot prove idle. The drain
            # acknowledgement rechecks the refreshed count before stopping.
            if not record.fresh:
                raise ValueError()
            if require_drain and record.state != 'draining':
                raise SwitchError('Gateway admission has not finished closing yet.')
            states.append((home, record.data))
        except SwitchError:
            raise
        except Exception:
            raise SwitchError('Gateway activity is unknown; restart refused.') from None
    if not states or not any(home == ROOT for home, _ in states):
        raise SwitchError('The root gateway activity ledger is unavailable.')
    return states


def selection_summary():
    """Cheap current-selection view for both providers (no process scans, no network)."""
    stores = codex_stores()
    claude = claude_selection()
    now = time.time()
    # The badge must show which account is really paying, not just the configured order: a dead
    # preferred login silently falls through to the next account.
    warnings = [w for w in (health_warning(home_name(home), acc, now) for home, acc in stores) if w]
    actual = {effective(acc, now) for _, acc in stores}
    return {'codex': {'selected': common_selection(stores), 'accounts': summaries(stores[0][1]) if stores else [],
                      'effective': actual.pop() if len(actual) == 1 else None, 'warnings': warnings},
            'claude': {'selected': claude['selected'], 'state': claude['state'], 'accounts': claude_accounts()}}


def preflight(own_state=None):
    """Full idle check. `own_state` is this process's own `/local-state` answer, given by a Desktop backend
    so it never has to call itself over HTTP; the switch worker, a separate process, leaves it out."""
    summary = selection_summary()
    blockers = list(dict.fromkeys(stale_desktop_blockers() + plugin_profile_blockers() + claude_provider_blockers()
                                  + gateway_setting_blockers()))
    if blockers:
        return {'ok': False, 'blockers': blockers, **summary}
    hosted = gateway_hosts_desktop()
    gateway = hosted or gateway_in_use()
    if gateway and service()['status'] != 'running':
        blockers.append('The Hermes gateway is not running.')
    elif hosted and load_settings().gateway_service and not service_owns_gateway(load_settings().gateway_service):
        blockers.append(f'The Hermes gateway running now is not the Windows service {load_settings().gateway_service}; '
                        'restart that service first.')
    # Work that is running or waiting. It blocks only a switch that restarts something; see `busy` below.
    busy = []
    if hosted:
        # Desktop has no backend to ask: the gateway runs every turn and serves this route, so Hermes's
        # idle proof is this process's own. The worker, a separate process, relies on the drain below.
        if own_state is not None:
            busy.extend(own_state()['blockers'])
    else:
        for b in backends(desktop()):
            local = own_state() if own_state is not None and b['pid'] == os.getpid() else call(b, '/local-state')
            busy.extend(f"{b['profile']}: {x}" for x in local['blockers'])
    for home, state in gateway_states() if gateway else []:
        if state['active_agents']:
            busy.append(f'{home_name(home)}: gateway or scheduled work is active.')
        if state['gateway_state'] == 'draining':
            blockers.append('Another gateway maintenance operation is in progress.')
    if EMAILS and summary['codex']['selected'] is None:
        blockers.append('Hermes profiles disagree on which Codex account comes first.')
    busy = list(dict.fromkeys(busy))
    # Which part of a switch restarts something. Codex never does on the unified runtime; Claude needs
    # provider 0.3.1 or later to skip it.
    restarts_for = {'codex': not live_switch_possible({'codex': True}),
                    'claude': not live_switch_possible({'claude': True})}
    # Running work blocks only a part of a switch that restarts the gateway (a restart would cut replies off).
    # A part without a restart applies at once beside running work: the Claude choice is never written back,
    # and a Codex order a finishing turn writes back is repaired by guard_codex_order.
    if all(restarts_for.values()):
        # Every switch restarts here (Hermes before 0.22): running work blocks it, as it always has.
        blockers, busy_codex, busy_claude = blockers + busy, [], []
    else:
        busy_codex = busy if restarts_for['codex'] else []
        busy_claude = busy if restarts_for['claude'] else []
    return {'ok': not blockers, 'blockers': blockers, 'busy': busy,
            'blockers_codex': busy_codex, 'blockers_claude': busy_claude,
            'desktop_restarts': not hosted, 'restarts': any(restarts_for.values()), 'restarts_for': restarts_for,
            **summary}


def wait_for(fn, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (SwitchError, psutil.Error, OSError):
            pass
        time.sleep(.5)
    raise SwitchError(message)


def process_alive(process):
    """Fresh Windows liveness plus identity; cached is_running can outlive exit."""
    if not psutil.pid_exists(process.pid):
        return False
    try:
        return psutil.Process(process.pid).create_time() == process.create_time()
    except psutil.NoSuchProcess:
        return False


def hidden_console():
    """How a helper command is started so that nothing flashes on the screen.

    CREATE_NO_WINDOW gives the child no console at all, and then every console program the child
    starts in turn (Hermes's own command runs several) opens a new, visible one for a moment. A new
    console that is created hidden is inherited by those grandchildren, so none of them shows."""
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {'startupinfo': info, 'creationflags': subprocess.CREATE_NEW_CONSOLE}


def command(args, timeout=60, home=None):
    # Never surface native stdout/stderr: auth CLI output may include sensitive metadata.
    p = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout, env=clean_env(home or ROOT), **hidden_console())
    if p.returncode:
        raise LocalOperationFailed('A required local Hermes operation failed.')


def launch_desktop():
    """Reopen Desktop through the Windows shell so it inherits the user's login environment, never this
    worker's (which carries the launching profile's loaded .env and HERMES_HOME)."""
    subprocess.Popen(['explorer.exe', str(EXE)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


# Each Hermes command starts a fresh Python, a few seconds each on Windows. Run one after another across ten
# profiles they kept the gateway down for about a minute on the owner's PC (2026-10-21), and a live switch still
# took 76 s (2026-10-22). Every profile keeps its own files, so all of them are written side by side in one
# round; within a profile the commands stay in order.
PROFILE_PARALLEL = 16
CODEX_PARALLEL = PROFILE_PARALLEL


def side_by_side(work, items):
    """Run `work(item)` for every item at once (up to PROFILE_PARALLEL); raise the first error after all finish."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(PROFILE_PARALLEL, len(items))) as pool:
        futures = [pool.submit(work, item) for item in items]
        errors = [future.exception() for future in futures]
    first = next((error for error in errors if error is not None), None)
    if first is not None:
        raise first
    return [future.result() for future in futures]


# The fields Hermes's own `auth reset` looks at to decide a login has a status to clear
# (agent/credential_pool_admin.py reset_statuses); `failure_reason` sits in the row's extra fields.
STATUS_FIELDS = ('last_status', 'last_status_at', 'last_error_code', 'failure_reason', 'model_cooldowns')


def needs_reset(entry):
    return any(entry.get(field) for field in STATUS_FIELDS)


def reorder_codex_stores(target, homes=None, reset=True):
    """Put `target` first in every Codex store, or only in `homes`, and return the profiles whose target login
    also had a cooldown or error cleared. `reset` allows that clearing; it runs only where the login carries a
    status (usually none), and a repair after a revert never needs it."""
    def reorder(store):
        home, acc = store
        target_id = acc[target]['entry']['id']
        command([str(runtime_python()), '-m', 'hermes_cli.main', 'auth', 'priority', 'openai-codex', target_id, '0'], home=home)
        if reset and needs_reset(acc[target]['entry']):
            command([str(runtime_python()), '-m', 'hermes_cli.main', 'auth', 'reset', 'openai-codex', target_id], home=home)
            return home_name(home)
        return None

    stores = [(home, acc) for home, acc in codex_stores() if homes is None or home in homes]
    return [name for name in side_by_side(reorder, stores) if name]


# How long a switch waits for the in-process writer's Hermes import before using the commands instead.
WRITER_READY_WAIT = 30
WRITER_JOB_WAIT = 60


class HermesWriter:
    """The hermes_writer.py helper: one Hermes Python, started at the beginning of a switch, that runs Hermes's
    own write functions for every profile. When it cannot start, is missing a Hermes function, or fails a job,
    the switch uses the per-profile commands instead and the receipt says why (`writer_fallback`)."""

    def __init__(self):
        self.process = None
        self.lines = queue.Queue()
        self.ready = None
        self.reason = None

    def start(self):
        try:
            self.process = subprocess.Popen(
                [str(runtime_python()), str(WRITER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8', cwd=str(WRITER.parent), env=clean_env(ROOT),
                **hidden_console())
        except Exception as exc:
            self.ready, self.reason = False, f'could not start ({type(exc).__name__})'
            return
        threading.Thread(target=self._read_lines, daemon=True).start()

    def _read_lines(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _answer(self, timeout):
        try:
            line = self.lines.get(timeout=timeout)
            return json.loads(line) if line else None
        except (queue.Empty, ValueError):
            return None

    def run(self, job):
        """The answer to `job`, or None when the commands must be used instead."""
        if self.ready is None:
            answer = self._answer(WRITER_READY_WAIT)
            self.ready = bool(answer and answer.get('ready'))
            if not self.ready:
                self.reason = ('a Hermes function it needs is missing or changed ({})'.format(answer.get('reason'))
                               if answer else 'it did not start in time')
        if not self.ready:
            self.close()
            return None
        try:
            self.process.stdin.write(json.dumps(job) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            answer = None
        else:
            answer = self._answer(WRITER_JOB_WAIT)
        if not answer or not answer.get('ok'):
            self.ready = False
            self.reason = f"a write failed ({answer.get('error')})" if answer else 'it stopped answering'
            self.close()
            return None
        return answer

    def close(self):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None


def written_by_hermes(writer, job, record):
    """Run `job` in the writer; False (and the reason in the receipt) when the commands must do it."""
    answer = writer.run(job) if writer is not None else None
    if record is not None:
        if answer is not None:
            record.setdefault('writer', 'hermes')
        elif writer is not None:
            record['writer'] = 'commands'
            record['writer_fallback'] = writer.reason
    return answer if answer is not None else False


def apply_codex(target, writer=None, record=None):
    """Put `target` first in every Codex store through the writer, else the commands; returns the profiles where
    the target login's cooldown or error was also cleared."""
    stores = codex_stores()
    job = {'codex': [{'home': str(home), 'id': acc[target]['entry']['id'], 'reset': needs_reset(acc[target]['entry'])}
                     for home, acc in stores]}
    answer = written_by_hermes(writer, job, record)
    if answer:
        names = {str(home): home_name(home) for home, _ in stores}
        return [names[path] for path in answer['reset']]
    return reorder_codex_stores(target)


# After a live Codex switch, a turn that was already running keeps its own copy of the Codex pool. Hermes writes
# that copy back, old order included, when the turn renews a token, hits a rate limit or finds a login dead
# (agent/credential_pool.py _persist). The switch worker watches for exactly that and puts the new order back.
GUARD_SECONDS = 2 * 60 * 60
GUARD_POLL = 0.25
# Gateway checks (restart, running work) read more files; they run on this slower beat.
GUARD_GATEWAY_EVERY = 8
# More repairs than this means something keeps choosing the old order on purpose; stop fighting it.
GUARD_MAX_REPAIRS = 5


def owns_receipt(operation_id):
    """True while no later switch has started: the receipt and any lock still name this operation."""
    try:
        if json.loads(RECEIPT.read_text()).get('operation_id') != operation_id:
            return False
        if LOCK.exists() and json.loads(LOCK.read_text()).get('operation_id') != operation_id:
            return False
        return True
    except (OSError, ValueError):
        return False


def gateway_pid():
    try:
        record = gateway_record(ROOT)
    except Exception:
        return None
    return record.pid if record is not None and record.alive else None


def gateway_idle():
    try:
        return not any(state['active_agents'] for _, state in gateway_states())
    except SwitchError:
        return False


def reverted_stores(previous):
    """Stores whose first choice is the account the switch moved away from."""
    return [home for home, acc in codex_stores() if selected(acc) == previous]


def guard_codex_order(record, now=None, sleep=None):
    """Watch a finished live Codex switch and repair an exact revert to the previous account.

    Only an order that names the previous account first again is repaired, never another change. Ends when
    the gateway work running at the switch has finished (new turns read the new order), when the gateway
    restarts, when another switch starts, after GUARD_MAX_REPAIRS repairs, or after GUARD_SECONDS."""
    now, sleep = now or time.monotonic, sleep or time.sleep
    guard = record['guard']
    operation_id = record['operation_id']
    target, previous = guard['target'], guard['previous']
    pid = gateway_pid()
    deadline = now() + GUARD_SECONDS
    tick = 0

    def save(**changes):
        guard.update(changes)
        if owns_receipt(operation_id):
            atomic(RECEIPT, record)

    while True:
        if not owns_receipt(operation_id):
            guard.update(state='done', ended='another_switch')
            return guard
        idle = False
        if tick % GUARD_GATEWAY_EVERY == 0:
            if pid is not None and gateway_pid() != pid:
                save(state='done', ended='gateway_restarted')
                return guard
            # Read before the stores: a turn writes its pool before it stops counting as running.
            idle = gateway_idle()
        try:
            reverted = reverted_stores(previous)
        except Exception:
            reverted = []
        if reverted:
            if len(guard['repairs']) >= GUARD_MAX_REPAIRS:
                save(state='done', ended='too_many_reverts')
                return guard
            began = time.time()
            try:
                reorder_codex_stores(target, homes=reverted, reset=False)
                ok = all(selected(acc) == target for home, acc in codex_stores() if home in reverted)
            except Exception:
                ok = False
            guard['repairs'].append({'time': began, 'profiles': [home_name(home) for home in reverted],
                                     'seconds': round(time.time() - began, 1), 'ok': ok})
            save()
            continue
        if idle:
            save(state='done', ended='work_finished')
            return guard
        if now() >= deadline:
            save(state='done', ended='time_limit')
            return guard
        tick += 1
        sleep(GUARD_POLL)


def revert_warning(last, summary):
    """A warning when Codex tries the account the last switch moved away from again."""
    if not isinstance(last, dict) or last.get('state') != 'complete':
        return None
    target = (last.get('target') or {}).get('codex')
    previous = (last.get('previous') or {}).get('codex')
    if not target or not previous or target == previous:
        return None
    if summary['codex']['selected'] != previous:
        return None
    return (f'Codex tries {EMAILS.get(previous, previous)} first again, not {EMAILS.get(target, target)} from the '
            'last switch: a session that was running put the old order back. Switch again to fix it.')


class WindowsOps:
    def __init__(self, record):
        self.record = record
        self.bs = []
        self.drained = []
        self.old_main = None
        self.old_tree = []
        self.stopped = False
        self.old_gateway = None
        self.gateway_homes = []
        self.gateway = True
        # Unified gateway runtime: Desktop holds no account and stays open; it reattaches by itself.
        self.hosted = False
        # Live switch: nothing restarts and nothing is held. The stores change beside running work; each chat
        # reads them on its next turn. A turn already running finishes on the account it started with.
        self.live = False
        # The in-process writer (HermesWriter), started in preflight.
        self.writer = None

    def stage(self, name):
        self.record['state'] = name
        self.record['message'] = name.replace('_', ' ').capitalize()
        atomic(RECEIPT, self.record)

    def checkpoint(self, name, **details):
        self.record.setdefault('checkpoints', []).append({'step': name, 'time': time.time(), **details})
        atomic(RECEIPT, self.record)

    def warn(self, message):
        self.record.setdefault('warnings', []).append(message)
        atomic(RECEIPT, self.record)

    def preflight(self, request):
        began = time.monotonic()
        ensure_settings_unchanged()
        try:
            require_switch_interfaces()
        except UnsupportedRuntime as exc:
            raise SwitchError(str(exc)) from None
        runtime_python()  # Fail before any process or credential mutation.
        if load_settings().gateway_service is None:
            hermes_launcher()
        # Hermes's import takes seconds; let it load while the checks below run.
        self.writer = HermesWriter()
        self.writer.start()
        self.stage('checking')
        p = preflight()
        self.hosted = gateway_hosts_desktop()
        self.gateway = self.hosted or gateway_in_use()
        self.live = self.hosted and self.gateway and live_switch_possible(request)
        # Running work blocks only a switch that restarts.
        if not p['ok'] or (not self.live and p.get('busy')):
            raise SwitchError(' '.join(p['blockers'] or p.get('busy') or []))
        previous = {}
        if request.get('codex'):
            if p['codex']['selected'] not in EMAILS:
                raise SwitchError('Current Codex account priority is ambiguous.')
            target = accounts()[request['codex']]
            previous['codex'] = p['codex']['selected']
        if request.get('claude'):
            previous['claude'] = p['claude']['selected'] if p['claude']['state'] == 'set' else 'unset'
        # The OpenAI and Anthropic checks are independent network calls; run them side by side.
        checks = []
        if request.get('codex'):
            # The other account is the idle one by definition, so its access token has often lapsed; asking
            # OpenAI with it would only read as "could not confirm". Hermes renews it at first use.
            note = lapsed_note(target)
            if note:
                self.warn(note)
            else:
                checks.append(lambda: usage(target))
        if request.get('claude'):
            def identify_claude():
                self.record['claude_identity_before'] = claude_identity(request['claude'])['method']
            checks.append(identify_claude)
        side_by_side(lambda check: check(), checks)
        if not self.hosted:
            self.old_main = desktop()
            self.bs = backends(self.old_main)
        self.old_gateway = gateway_identity(service()) if self.gateway else None
        self.record.update(previous=previous, desktop_restarts=not self.hosted, restarts=not self.live,
                           old_desktop_pid=self.old_main.pid if self.old_main else None,
                           old_gateway_pid=self.old_gateway[0] if self.gateway else None)
        self.checkpoint('checked', seconds=round(time.monotonic() - began, 1))
        return {'previous': previous}

    def freeze(self):
        ensure_settings_unchanged()
        self.stage('checking')
        if self.gateway and not self.live:
            drain = drain_control()
            for home, _ in gateway_states():
                if drain.drain_requested(home=home):
                    raise SwitchError('Another gateway maintenance operation owns the drain.')
                drain.write_drain_request(home=home, principal='account-switcher', suppress_notification=True)
                self.drained.append(home)
            wait_for(lambda: gateway_states(require_drain=True), 12, 'Gateway did not acknowledge the restart safety gate.')
            if any(d['active_agents'] for _, d in gateway_states(require_drain=True)):
                raise SwitchError('Gateway work started during preflight. Nothing was switched; retry when idle.')
        # Hermes's own idle proof, asked again of every Desktop backend just before the close. There is no
        # admission hold: work that starts between this answer and the window closing is the accepted gap.
        # On the unified runtime there is no Desktop backend to ask.
        if self.hosted:
            return
        for b in self.bs:
            if call(b, '/local-state')['blockers']:
                raise SwitchError('Desktop work started during preflight. Nothing was switched; retry when idle.')
        if {b['pid'] for b in backends(self.old_main)} != {b['pid'] for b in self.bs}:
            raise SwitchError('Desktop backend inventory changed during preflight; retry.')

    def stop(self):
        ensure_settings_unchanged()
        if self.live:
            self.stage('applying')
            return
        self.stage('restarting')
        states = gateway_states(require_drain=True) if self.gateway else []
        old_gateway_processes = [psutil.Process(d['pid']) for _, d in states]
        # One multiplexed gateway can publish its ledger into several homes; stop it through its owner.
        self.gateway_homes = list(dict.fromkeys(Path(d.get('hermes_home') or home) for home, d in states))
        if not self.hosted:
            self.close_desktop(old_gateway_processes)
        self.stop_gateway()
        wait_for(lambda: all(not process_alive(p) for p in old_gateway_processes), 20,
                 'An old gateway process is still alive; credentials were not changed.')
        self.checkpoint('gateway_stopped')
        self.stopped = True

    def close_desktop(self, old_gateway_processes):
        self_process = psutil.Process()
        helper_pids = {self_process.pid} | {p.pid for p in self_process.parents() if p.pid != self.old_main.pid}
        self.old_tree = [p for p in self.old_main.children(recursive=True) if p.pid not in helper_pids]
        self.checkpoint('closing_desktop', desktop_pid=self.old_main.pid,
                        gateway_pids=[p.pid for p in old_gateway_processes])
        # With the gateway draining and every backend idle, close the exact Desktop main window gracefully.
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        def close(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self.old_main.pid:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE; no broad process kill.
            return True
        callback = callback_type(close)
        user32.EnumWindows(callback, 0)
        wait_for(lambda: not process_alive(self.old_main), 25, 'Hermes Desktop did not close; no account change was made.')
        wait_for(lambda: all(not process_alive(p) for p in self.old_tree), 25,
                 'A Desktop child is still running; no account change was made.')
        self.checkpoint('desktop_closed')

    def stop_gateway(self):
        service_name = load_settings().gateway_service
        if not self.gateway:
            # Desktop-only install: nothing to stop, but nothing may have started since the check either.
            if service()['status'] != 'stopped':
                raise SwitchError('A Hermes gateway started during the switch; no account change was made.')
            return
        if service_name is None:
            # Hermes drains through its planned-stop marker, then kills only the process it fingerprinted.
            for home in self.gateway_homes or [ROOT]:
                gateway_command('stop', home)
        else:
            command(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                     f"Stop-Service -Name {service_name} -ErrorAction Stop; (Get-Service {service_name}).WaitForStatus('Stopped',[TimeSpan]::FromSeconds(40))"])
        wait_for(lambda: service()['status'] == 'stopped', 45, 'The Hermes gateway did not stop.')

    def start_gateway(self):
        service_name = load_settings().gateway_service
        if service_name is None:
            for home in self.gateway_homes or [ROOT]:
                gateway_command('start', home)
        else:
            command(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                     f"Start-Service -Name {service_name} -ErrorAction Stop; (Get-Service {service_name}).WaitForStatus('Running',[TimeSpan]::FromSeconds(40))"])
        wait_for(lambda: service()['status'] == 'running', 30, 'The Hermes gateway did not start.')

    def apply(self, request, allow_changed=False):
        if not allow_changed:
            ensure_settings_unchanged()
        self.stage('applying')
        if not self.live and (service()['status'] != 'stopped' or any(process_alive(p) for p in self.old_tree)):
            raise SwitchError('An old account consumer is still running; refusing to change credentials.')
        if request.get('codex'):
            target = request['codex']
            if not allow_changed:
                ensure_settings_unchanged()
            began = time.monotonic()
            reset = apply_codex(target, self.writer, self.record)
            if not allow_changed:
                ensure_settings_unchanged()
            # `reset_profiles`: where the target login had a cooldown or error, so `auth reset` ran as well.
            self.checkpoint('codex_applied', seconds=round(time.monotonic() - began, 1), reset_profiles=reset)
            if common_selection(codex_stores()) != target:
                raise SwitchError('Saved account readback does not match the requested account in every profile.')
        if request.get('claude'):
            began = time.monotonic()
            apply_claude(request['claude'], allow_changed=allow_changed, writer=self.writer, record=self.record)
            self.checkpoint('claude_applied', seconds=round(time.monotonic() - began, 1))

    def start(self):
        if self.live:
            # The stores are written and read back; each chat picks the new account up on its next turn.
            return
        self.stage('reopening')
        # Clear only our markers, before allowing the new gateway to accept work.
        self.clear_drains()
        if self.gateway:
            self.start_gateway()
            wait_for(gateway_healthy, 55, 'The restarted Hermes gateway failed its health check.')
        self.stopped = False
        if not self.hosted:
            launch_desktop()

    def verify(self, request):
        self.verify_began = time.monotonic()
        self.stage('verifying')
        codex_target = request.get('codex')
        claude_target = request.get('claude')
        if self.live:
            # Nothing restarted: the stores are the evidence, and the gateway reads them on its next turn.
            result = {'new_desktop_pid': None, 'new_gateway_pid': None, 'gateway_pid': service()['pid'],
                      'restarted': False, 'profiles': [{'profile': 'gateway', 'pid': service()['pid']}]}
            return self.verify_stores(request, result)
        if self.hosted:
            # The gateway is the only account consumer; Desktop was never closed and reattaches by itself.
            if gateway_identity(service()) == self.old_gateway:
                raise SwitchError('A required process did not restart.')
            result = {'new_desktop_pid': None, 'new_gateway_pid': service()['pid'],
                      'profiles': [{'profile': 'gateway', 'pid': service()['pid']}]}
            return self.verify_stores(request, result)
        new_main = wait_for(desktop, 45, 'Hermes Desktop did not reopen.')
        if new_main.pid == self.old_main.pid or (self.gateway and gateway_identity(service()) == self.old_gateway):
            raise SwitchError('A required process did not restart.')
        def verified_backends():
            self.record['verification_attempts'] = self.record.get('verification_attempts', 0) + 1
            self.record['verification_desktop_pid'] = new_main.pid
            try:
                bs = backends(new_main)
            except (SwitchError, psutil.Error, OSError) as exc:
                import traceback
                self.record['verification_last_check'] = {
                    'phase': 'discovering_backends', 'type': type(exc).__name__,
                    'message': str(exc) if isinstance(exc, SwitchError) else None,
                    'errno': getattr(exc, 'errno', None), 'winerror': getattr(exc, 'winerror', None),
                    'frames': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                               for f in traceback.extract_tb(exc.__traceback__)[-8:]],
                }
                raise
            evidence = []
            for b in bs:
                try:
                    state = call(b, '/local-state')
                except (SwitchError, psutil.Error, OSError) as exc:
                    self.record['verification_last_check'] = {
                        'phase': 'reading_backend', 'profile': b['profile'], 'pid': b['pid'],
                        'type': type(exc).__name__,
                        'transport': getattr(exc, 'transport_details', None),
                    }
                    raise
                self.record['verification_last_check'] = {
                    'phase': 'checking_account', 'profile': b['profile'], 'pid': b['pid'],
                    'born': b['born'], 'started_at': self.record['started_at'],
                    'uses_codex': state.get('uses_codex', True), 'selected': state.get('selected'),
                    'codex_target': codex_target,
                    'claude_matches': not claude_target or _same_dir(state.get('claude_config_dir') or '', claude_target),
                }
                if b['born'] <= self.record['started_at']:
                    return False
                if codex_target:
                    # A profile with no Codex logins (e.g. an xAI-only voice profile) has nothing to select.
                    if state.get('uses_codex', True) and state['selected'] != codex_target:
                        return False
                if claude_target and not _same_dir(state.get('claude_config_dir') or '', claude_target):
                    return False
                evidence.append({'profile': b['profile'], 'pid': b['pid'], 'selected': state['selected'],
                                 'claude_config_dir': state.get('claude_config_dir')})
            return evidence
        evidence = wait_for(verified_backends, 75, 'Not every reopened Desktop backend verified the selected account.')
        result = {'new_desktop_pid': new_main.pid, 'new_gateway_pid': service()['pid'], 'profiles': evidence}
        return self.verify_stores(request, result)

    def verify_service_owner(self):
        """A declared service must be the gateway serving afterwards, not merely some gateway that answers."""
        name = load_settings().gateway_service
        if not self.gateway or name is None:
            return
        wait_for(lambda: service_owns_gateway(name), 60,
                 f'The accounts were changed, but the Hermes gateway now running is not the Windows service {name}: '
                 f'Hermes Desktop started a gateway of its own while the service was stopped, and {name} could not '
                 f'take it back. Make {name} start "hermes gateway run --replace" (see "Gateway service" in the '
                 'user guide), then restart it.')

    def verify_stores(self, request, result):
        self.verify_service_owner()
        codex_target = request.get('codex')
        claude_target = request.get('claude')
        if codex_target:
            # Cold profiles have no process; their saved stores must also try the target first.
            if common_selection(codex_stores()) != codex_target:
                raise SwitchError('A saved profile store does not try the selected Codex account first.')
            # Quota availability was proven in preflight; after a restart it is advisory only. A switch without a
            # restart changed nothing OpenAI can see, so it skips the second call.
            if not self.live:
                try:
                    usage(accounts()[codex_target])
                except SwitchError as exc:
                    self.warn(f'Switch verified; OpenAI quota re-check failed afterwards: {exc}')
            result['codex'] = codex_target
            previous = (self.record.get('previous') or {}).get('codex')
            if self.live and previous and previous != codex_target:
                # Watched by the worker once the switch is reported complete (guard_codex_order).
                self.record['guard'] = {'state': 'watching', 'target': codex_target, 'previous': previous,
                                        'started_at': time.time(), 'repairs': []}
        if claude_target:
            state = claude_selection()
            if state['state'] != 'set' or state['selected'] != claude_target:
                raise SwitchError('A saved profile store does not name the selected Claude account.')
            # Preflight proved who owns the target folder's login and a switch without a restart does not touch
            # it; after a restart the provider may have refreshed it, so it is asked again.
            method = self.record.get('claude_identity_before') if self.live else None
            result['claude'] = claude_target
            result['claude_identity'] = method or claude_identity(claude_target)['method']
        if getattr(self, 'verify_began', None) is not None:
            self.checkpoint('verified', seconds=round(time.monotonic() - self.verify_began, 1))
        return result

    def recover(self, previous, changed):
        # Do not restore stale OAuth files. Only restore preference while every old consumer is stopped.
        # Recovery moves the stage on (it reopens Hermes); keep the stage the failure happened in.
        self.record.setdefault('failed_during', self.record.get('state'))
        self.record['recovery'] = 'not_needed'
        try:
            # A live switch restores the old choice the same way it wrote the new one: beside running work.
            if changed and (service()['status'] == 'stopped' or self.live):
                self.apply({name: previous.get(name) for name in PROVIDERS}, allow_changed=True)
                self.record['recovery'] = 'previous_preference_restored'
            elif changed:
                self.record['recovery'] = 'new_preference_retained; live consumers forbid blind rollback'
        except Exception:
            # A restore that fails must never leave Hermes down; reopen and let the badge report the stores.
            self.record['recovery'] = 'restore_failed; profile stores may disagree'
        if self.gateway and service()['status'] == 'stopped':
            self.start()
        elif self.old_main is not None and not process_alive(self.old_main):
            try:
                desktop()
            except SwitchError:
                launch_desktop()

    def clear_drains(self):
        if not self.drained:
            return
        drain = drain_control()
        for home in self.drained:
            if (drain.read_drain_request(home=home) or {}).get('principal') == 'account-switcher':
                drain.clear_drain_request(home=home)
        self.drained.clear()

    def unfreeze(self):
        self.clear_drains()
        if self.writer is not None:
            self.writer.close()
