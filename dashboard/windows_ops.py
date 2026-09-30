"""Bounded Windows operations. Never returns credential/token values to callers."""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import psutil
from switch_core import (CLAUDE, EMAILS, PROVIDERS, SwitchError, effective, health_warning, identify, lapsed_note,
                         selected, summaries)
from settings import SettingsError, load_settings, settings_snapshot, ensure_settings_unchanged
from compat import UnsupportedRuntime, require_switch_interfaces, committed_venv, drain_control, safe_yaml_load
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


RECEIPT = ROOT / 'logs/codex-account-switch-last.json'
LOCK = ROOT / 'logs/codex-account-switch.lock'
NS = '/api/plugins/codex-account-switch'
ENV_SET = Path(__file__).resolve().with_name('env_set.py')

# Claude: the DirectSDK plugin hands the `claude` CLI this folder as CLAUDE_CONFIG_DIR, read from the process
# environment per request, so the account is whichever Hermes-owned login folder every store's .env names.
CLAUDE_VAR = 'CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR'
CLAUDE_AUTH = ROOT / 'claude-auth'
CLAUDE_CLI = 'claude'
# The provider's folder and manifest name; Hermes's catalog lists it without the suffix.
CLAUDE_PROVIDER = 'claude-subscription-directsdk-experimental'
CLAUDE_PROVIDER_NAMES = (CLAUDE_PROVIDER, 'claude-subscription-directsdk')
_PATH_LOCK = threading.RLock()


def configure_paths(settings=None):
    """Bind every mutable runtime path from the same validated settings object."""
    global ROOT, EXE, RECEIPT, LOCK, CLAUDE_AUTH
    settings = settings or load_settings()
    ROOT = settings.hermes_root
    EXE = settings.desktop_exe
    RECEIPT = ROOT / 'logs/codex-account-switch-last.json'
    LOCK = ROOT / 'logs/codex-account-switch.lock'
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
    temp.replace(path)


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
PRINCIPAL = 'codex-account-switch'


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


def plugin_profile_blockers():
    """Every discovered profile must opt into the installed backend before a restart."""
    plugin = ROOT / 'plugins/codex-account-switch/dashboard/manifest.json'
    if not plugin.is_file():
        return ['The Account Switcher backend package is missing from the Hermes root.']
    blockers = []
    try:
        for home in profile_homes():
            flags = _plugin_flags(home)
            if flags is None:
                blockers.append(f'{home_name(home)}: Account Switcher plugin configuration is unreadable.')
            elif not _effectively_enabled(flags, 'codex-account-switch'):
                # `hermes plugins enable` only knows a plugin that is installed in that profile's own folder.
                flag = '' if Path(home) == ROOT else f'-p {home.name} '
                blockers.append(f'{home_name(home)}: Account Switcher is not enabled in this profile. Install it '
                                f'into this profile\'s folder, run "hermes {flag}plugins enable codex-account-switch", '
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


def claude_selection():
    """Which Hermes-owned Claude folder every store names: ``{'selected', 'state', 'stores'}``.

    ``state`` is ``unset`` (no store names one: the plugin falls back to the CLI's own ``~/.claude``),
    ``set`` (every store names the same account) or ``mixed`` (stores disagree or name a foreign folder).
    """
    stores = {}
    for home in profile_homes():
        value = env_values(home).get(CLAUDE_VAR)
        if not value:
            stores[home.name] = None
        else:
            stores[home.name] = next((key for key in CLAUDE if _same_dir(value, key)), 'foreign')
    picks = set(stores.values())
    if picks == {None}:
        return {'selected': None, 'state': 'unset', 'stores': stores}
    if len(picks) == 1 and 'foreign' not in picks:
        return {'selected': picks.pop(), 'state': 'set', 'stores': stores}
    return {'selected': None, 'state': 'mixed', 'stores': stores}


def _credentials(key):
    path = claude_dir(key) / '.credentials.json'
    try:
        return (json.loads(path.read_text(encoding='utf-8')).get('claudeAiOauth') or {}) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def _cached_label(key):
    path = claude_dir(key) / '.claude.json'
    try:
        return ((json.loads(path.read_text(encoding='utf-8')).get('oauthAccount') or {}).get('emailAddress') or '').lower()
    except (OSError, ValueError):
        return ''


def claude_accounts():
    """Cheap, token-free summary for status polling."""
    rows = []
    for key, (label, email) in CLAUDE.items():
        cred = _credentials(key)
        rows.append({'key': key, 'label': label, 'email': email,
                     'logged_in': bool(cred.get('accessToken') and cred.get('refreshToken')),
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
        return
    argv[0] = exe
    try:
        with tempfile.TemporaryDirectory(prefix='claude-switch-handshake-') as cwd:
            subprocess.run(argv, input=handshake, env=env, cwd=cwd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout, **hidden_console())
    except (OSError, subprocess.SubprocessError):
        pass


def claude_identity(key):
    """Prove which account owns the login in the Hermes folder for ``key``.

    The token owner must be verified with Anthropic. An expired token gets one
    CLI refresh attempt. A cached folder label is never evidence of ownership.
    """
    label, expected = CLAUDE[key]
    cred = _credentials(key)
    if not (cred.get('accessToken') and cred.get('refreshToken')):
        raise SwitchError(f'The {label} Claude login is missing; sign it in with "setup.cmd claude {key}".')

    def expired():
        exp = cred.get('expiresAt')
        return not isinstance(exp, (int, float)) or exp / 1000 <= time.time() + 60

    if expired():
        _handshake(key)
        cred = _credentials(key)
    if not expired():
        email = ''
        for attempt in range(2):  # One network blip must not read as an unverifiable login.
            try:
                email = _oauth_profile(cred['accessToken'])
                break
            except Exception:
                if attempt == 0:
                    time.sleep(2)
        if email:
            if email != expected:
                raise SwitchError(f'The {label} Claude folder is logged in as {email}, not {expected}; sign it in again with "setup.cmd claude {key}".')
            return {'email': email, 'method': 'token'}
    raise SwitchError(f'The {label} Claude login could not be verified with Anthropic; recheck, and if this stays sign it in again with "setup.cmd claude {key}".')


def claude_value(key):
    # Forward slashes: valid for Windows and Node, and immune to .env quote/backslash handling.
    return claude_dir(key).resolve().as_posix()


def apply_claude(key, allow_changed=False):
    """Point every store's .env at the folder for ``key``; ``'unset'`` removes the variable (plugin default)."""
    for home in profile_homes():
        if not allow_changed:
            ensure_settings_unchanged()
        value = '--remove' if key == 'unset' else claude_value(key)
        command([str(runtime_python()), str(ENV_SET), CLAUDE_VAR, value], home=home)
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
    except Exception:
        raise SwitchError(f"The account-switch plugin is not responding in {backend['profile']}.") from None


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
            record = json.loads((Path(home) / 'gateway_state.json').read_text(encoding='utf-8-sig'))
            pid = int(record['pid'])
            stamp = datetime.fromisoformat(record['updated_at'].replace('Z', '+00:00'))
            # An old record's PID may belong to an unrelated process by now; a live gateway keeps its record fresh.
            fresh = stamp.tzinfo is not None and abs((datetime.now(timezone.utc) - stamp).total_seconds()) <= 120
        except Exception:
            continue
        if not fresh or record.get('gateway_state') not in ('running', 'draining'):
            continue
        if pid in seen or not psutil.pid_exists(pid):
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


def gateway_states(require_drain=False):
    states = []
    for home in profile_homes():
        path = home / 'gateway_state.json'
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text(encoding='utf-8-sig'))
            pid = int(d['pid'])
            if not psutil.pid_exists(pid):
                continue
            state = d.get('gateway_state')
            if state not in ('running', 'draining'):
                raise ValueError()
            active = d['active_agents']
            if not isinstance(active, int) or active < 0:
                raise ValueError()
            # A live PID with an old ledger cannot prove idle. The drain
            # acknowledgement rechecks the refreshed count before stopping.
            stamp = datetime.fromisoformat(d['updated_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None or abs((datetime.now(timezone.utc) - stamp).total_seconds()) > 120:
                raise ValueError()
            if require_drain and state != 'draining':
                raise SwitchError('Gateway admission has not finished closing yet.')
            states.append((home, d))
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
    blockers = list(dict.fromkeys(plugin_profile_blockers() + claude_provider_blockers()
                                  + gateway_setting_blockers()))
    if blockers:
        return {'ok': False, 'blockers': blockers, **summary}
    main = desktop()
    bs = backends(main)
    gateway = gateway_in_use()
    if gateway and service()['status'] != 'running':
        blockers.append('The Hermes gateway is not running.')
    for b in bs:
        local = own_state() if own_state is not None and b['pid'] == os.getpid() else call(b, '/local-state')
        blockers.extend(f"{b['profile']}: {x}" for x in local['blockers'])
    for home, state in gateway_states() if gateway else []:
        if state['active_agents']:
            blockers.append(f'{home_name(home)}: gateway or scheduled work is active.')
        if state['gateway_state'] == 'draining':
            blockers.append('Another gateway maintenance operation is in progress.')
    if EMAILS and summary['codex']['selected'] is None:
        blockers.append('Hermes profiles disagree on which Codex account comes first.')
    return {'ok': not blockers, 'blockers': blockers, **summary}


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
        ensure_settings_unchanged()
        try:
            require_switch_interfaces()
        except UnsupportedRuntime as exc:
            raise SwitchError(str(exc)) from None
        runtime_python()  # Fail before any process or credential mutation.
        if load_settings().gateway_service is None:
            hermes_launcher()
        self.stage('checking')
        p = preflight()
        if not p['ok']:
            raise SwitchError(' '.join(p['blockers']))
        previous = {}
        if request.get('codex'):
            if p['codex']['selected'] not in EMAILS:
                raise SwitchError('Current Codex account priority is ambiguous.')
            target = accounts()[request['codex']]
            # The other account is the idle one by definition, so its access token has often lapsed; asking
            # OpenAI with it would only read as "could not confirm". Hermes renews it at first use.
            note = lapsed_note(target)
            if note:
                self.warn(note)
            else:
                usage(target)
            previous['codex'] = p['codex']['selected']
        if request.get('claude'):
            identity = claude_identity(request['claude'])
            self.record['claude_identity_before'] = identity['method']
            previous['claude'] = p['claude']['selected'] if p['claude']['state'] == 'set' else 'unset'
        self.old_main = desktop()
        self.bs = backends(self.old_main)
        self.gateway = gateway_in_use()
        self.old_gateway = gateway_identity(service()) if self.gateway else None
        self.record.update(previous=previous, old_desktop_pid=self.old_main.pid,
                           old_gateway_pid=self.old_gateway[0] if self.gateway else None)
        return {'previous': previous}

    def freeze(self):
        ensure_settings_unchanged()
        self.stage('checking')
        if self.gateway:
            drain = drain_control()
            for home, _ in gateway_states():
                if drain.drain_requested(home=home):
                    raise SwitchError('Another gateway maintenance operation owns the drain.')
                drain.write_drain_request(home=home, principal='codex-account-switch', suppress_notification=True)
                self.drained.append(home)
            wait_for(lambda: gateway_states(require_drain=True), 12, 'Gateway did not acknowledge the restart safety gate.')
            if any(d['active_agents'] for _, d in gateway_states(require_drain=True)):
                raise SwitchError('Gateway work started during preflight. Nothing was switched; retry when idle.')
        for b in self.bs:
            result = call(b, '/freeze', {})
            if not result.get('ok'):
                raise SwitchError('Desktop work started during preflight. Nothing was switched; retry when idle.')
        if {b['pid'] for b in backends(self.old_main)} != {b['pid'] for b in self.bs}:
            raise SwitchError('Desktop backend inventory changed during preflight; retry.')

    def stop(self):
        ensure_settings_unchanged()
        self.stage('restarting')
        self_process = psutil.Process()
        helper_pids = {self_process.pid} | {p.pid for p in self_process.parents() if p.pid != self.old_main.pid}
        self.old_tree = [p for p in self.old_main.children(recursive=True) if p.pid not in helper_pids]
        states = gateway_states(require_drain=True) if self.gateway else []
        old_gateway_processes = [psutil.Process(d['pid']) for _, d in states]
        # One multiplexed gateway can publish its ledger into several homes; stop it through its owner.
        self.gateway_homes = list(dict.fromkeys(Path(d.get('hermes_home') or home) for home, d in states))
        self.checkpoint('closing_desktop', desktop_pid=self.old_main.pid,
                        gateway_pids=[p.pid for p in old_gateway_processes])
        # With RPC admission closed and work idle, close the exact Desktop main window gracefully.
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
        self.stop_gateway()
        wait_for(lambda: all(not process_alive(p) for p in old_gateway_processes), 20,
                 'An old gateway process is still alive; credentials were not changed.')
        self.checkpoint('gateway_stopped')
        self.stopped = True

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
        if service()['status'] != 'stopped' or any(process_alive(p) for p in self.old_tree):
            raise SwitchError('An old account consumer is still running; refusing to change credentials.')
        if request.get('codex'):
            target = request['codex']
            for home, acc in codex_stores():
                if not allow_changed:
                    ensure_settings_unchanged()
                target_id = acc[target]['entry']['id']
                command([str(runtime_python()), '-m', 'hermes_cli.main', 'auth', 'priority', 'openai-codex', target_id, '0'], home=home)
                if not allow_changed:
                    ensure_settings_unchanged()
                command([str(runtime_python()), '-m', 'hermes_cli.main', 'auth', 'reset', 'openai-codex', target_id], home=home)
            if common_selection(codex_stores()) != target:
                raise SwitchError('Saved account readback does not match the requested account in every profile.')
        if request.get('claude'):
            apply_claude(request['claude'], allow_changed=allow_changed)

    def start(self):
        self.stage('reopening')
        # Clear only our markers, before allowing the new gateway to accept work.
        self.clear_drains()
        if self.gateway:
            self.start_gateway()
            wait_for(gateway_healthy, 55, 'The restarted Hermes gateway failed its health check.')
        self.stopped = False
        launch_desktop()

    def verify(self, request):
        self.stage('verifying')
        new_main = wait_for(desktop, 45, 'Hermes Desktop did not reopen.')
        if new_main.pid == self.old_main.pid or (self.gateway and gateway_identity(service()) == self.old_gateway):
            raise SwitchError('A required process did not restart.')
        codex_target = request.get('codex')
        claude_target = request.get('claude')
        def verified_backends():
            bs = backends(new_main)
            evidence = []
            for b in bs:
                state = call(b, '/local-state')
                if b['born'] <= self.record['started_at']:
                    return False
                if codex_target:
                    # A profile with no Codex logins (e.g. an xAI-only voice profile) has nothing to select.
                    if state.get('uses_codex', True) and state['selected'] != codex_target:
                        return False
                    if any(row['selected'] != codex_target for row in state['live_pools']):
                        return False
                if claude_target and not _same_dir(state.get('claude_config_dir') or '', claude_target):
                    return False
                evidence.append({'profile': b['profile'], 'pid': b['pid'], 'selected': state['selected'],
                                 'live_pools': state['live_pools'], 'claude_config_dir': state.get('claude_config_dir')})
            return evidence
        evidence = wait_for(verified_backends, 75, 'Not every reopened Desktop backend verified the selected account.')
        result = {'new_desktop_pid': new_main.pid, 'new_gateway_pid': service()['pid'], 'profiles': evidence}
        if codex_target:
            # Cold profiles have no process; their saved stores must also try the target first.
            if common_selection(codex_stores()) != codex_target:
                raise SwitchError('A saved profile store does not try the selected Codex account first.')
            # Quota availability was proven in preflight; after the restart it is advisory only.
            try:
                usage(accounts()[codex_target])
            except SwitchError as exc:
                self.warn(f'Switch verified; OpenAI quota re-check failed afterwards: {exc}')
            result['codex'] = codex_target
        if claude_target:
            state = claude_selection()
            if state['state'] != 'set' or state['selected'] != claude_target:
                raise SwitchError('A saved profile store does not name the selected Claude account.')
            identity = claude_identity(claude_target)
            result['claude'] = claude_target
            result['claude_identity'] = identity['method']
        return result

    def recover(self, previous, changed):
        # Do not restore stale OAuth files. Only restore preference while every old consumer is stopped.
        # Recovery moves the stage on (it reopens Hermes); keep the stage the failure happened in.
        self.record.setdefault('failed_during', self.record.get('state'))
        self.record['recovery'] = 'not_needed'
        try:
            if changed and service()['status'] == 'stopped':
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
            if (drain.read_drain_request(home=home) or {}).get('principal') == 'codex-account-switch':
                drain.clear_drain_request(home=home)
        self.drained.clear()

    def unfreeze(self):
        self.clear_drains()
        for b in self.bs:
            try:
                call(b, '/release', {})
            except SwitchError:
                pass
