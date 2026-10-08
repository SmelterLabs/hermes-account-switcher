"""Authenticated Desktop plugin API. Account secrets never leave this process."""
from importlib import import_module
import json
import os
from pathlib import Path
import sys
import time
from types import ModuleType
import uuid
from fastapi import APIRouter, HTTPException, Request

HERE = Path(__file__).resolve().parent
# Hermes's long-lived web server loads this file on its own. The sibling modules are imported there as
# submodules of a package named after this plugin, never by putting dashboard/ on that process's sys.path,
# where generic names like `settings` and `compat` would shadow, or be shadowed by, other modules. The test
# suite imports this file as `plugin_api` from dashboard/ on its own sys.path and keeps the plain names.
PACKAGE = 'codex_account_switch_dashboard'
if __name__ != 'plugin_api' and PACKAGE not in sys.modules:
    sys.modules[PACKAGE] = ModuleType(PACKAGE)
    sys.modules[PACKAGE].__path__ = [str(HERE)]


def sibling(name):
    return import_module(name if __name__ == 'plugin_api' else f'{PACKAGE}.{name}')


_core, _settings, _compat = sibling('switch_core'), sibling('settings'), sibling('compat')
SwitchError, describe, identify, normalize_request = _core.SwitchError, _core.describe, _core.identify, _core.normalize_request
SettingsError, load_settings, settings_path = _settings.SettingsError, _settings.load_settings, _settings.settings_path
settings_digest = _settings.settings_digest
read_codex_pool, desktop_idle_blockers = _compat.read_codex_pool, _compat.desktop_idle_blockers
ops = sibling('windows_ops')
first_run = sibling('first_run')

router = APIRouter()
SCOPE = 'All Hermes profiles on this PC'
# What status answers before the first setup: nothing selected, nothing to choose from, and the reason.
NOT_SET_UP = {'setup_needed': True, 'codex': {'selected': None, 'accounts': [], 'effective': None, 'warnings': []},
              'claude': {'selected': None, 'state': 'unset', 'accounts': []}, 'last_operation': None,
              'in_progress': False, 'blockers': [], 'scope': SCOPE}
IN_PROGRESS = 'An account switch is already in progress.'
# What answers "is this Desktop backend idle": Hermes's own idle proof for this process. There is no
# admission hold of the plugin's own; see TECHNICAL-GUIDE.md, "The switch transaction".
ACTIVITY_CHECK = 'hermes_cli.web_server_idle_proof.idle_proof'


def authorize(request):
    """Hermes's web server already requires its session token on every plugin route (its auth
    middleware; only its public paths are exempt). This adds: the caller is on this PC."""
    if not request.client or request.client.host not in ('127.0.0.1', '::1', 'testclient'):
        raise HTTPException(403, 'This switch is only available locally on this PC.')


def safe_error(exc):
    return str(exc) if isinstance(exc, (SwitchError, SettingsError)) else 'Account switch check failed safely; no secret details are logged.'


def local_state():
    # Each profile owns its logins (#111724); a profile without Codex rows does not use Codex.
    rows = read_codex_pool()
    acc = identify(rows) if rows and ops.EMAILS else None
    blockers = desktop_idle_blockers()
    return {'pid': os.getpid(), 'selected': ops.selected(acc) if acc else None, 'uses_codex': acc is not None,
            # What the Claude plugin will hand the `claude` CLI from THIS process (the .env it loaded at start).
            'claude_config_dir': os.environ.get(ops.CLAUDE_VAR) or None,
            'blockers': blockers,
            'activity_check': None if blockers == [_compat.IDLE_UNAVAILABLE] else ACTIVITY_CHECK}


@router.get('/local-state')
def get_local_state(request: Request):
    authorize(request)
    try:
        with ops.bound_settings():
            return local_state()
    except Exception as exc:
        raise HTTPException(409, safe_error(exc)) from None


@router.get('/status')
def status(request: Request):
    """Cheap: current selections and the last receipt. No process scans, no probes — this is polled."""
    authorize(request)
    try:
        with ops.bound_settings():
            ops.recover_abandoned_check()
            last = json.loads(ops.RECEIPT.read_text()) if ops.RECEIPT.is_file() else None
            in_progress = ops.LOCK.exists()
            return {**ops.selection_summary(), 'last_operation': last, 'in_progress': in_progress,
                    'blockers': [IN_PROGRESS] if in_progress else [], 'scope': SCOPE}
    except SettingsError as exc:
        # No settings file yet is not a failure: it is where every install starts.
        try:
            missing = not settings_path().exists()
        except SettingsError:
            missing = False
        if missing:
            return dict(NOT_SET_UP)
        raise HTTPException(409, safe_error(exc)) from None
    except Exception as exc:
        raise HTTPException(409, safe_error(exc)) from None


@router.get('/setup')
def setup_found(request: Request):
    """What a first run found on this PC: accounts signed in, names offered. No tokens, nothing written."""
    authorize(request)
    try:
        return first_run.found(first_run.hermes_home())
    except (first_run.SetupError, SettingsError) as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(409, 'Setup could not read this Hermes installation.') from None


@router.post('/setup')
def setup_save(request: Request, body: dict):
    """Write the settings under the names the user gave. Changes no account and restarts nothing."""
    authorize(request)
    if not isinstance(body, dict) or body.get('confirmed') is not True:
        raise HTTPException(400, 'Setup was not confirmed; nothing was written.')
    codex, claude = body.get('codex') or {}, body.get('claude') or {}
    if not isinstance(codex, dict) or not isinstance(claude, dict):
        raise HTTPException(400, 'Setup names are not in the expected form; nothing was written.')
    if ops.LOCK.exists():
        raise HTTPException(409, IN_PROGRESS)
    try:
        data = first_run.save(first_run.hermes_home(), codex, claude)
    except (first_run.SetupError, SettingsError) as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(409, 'Setup could not write the settings; nothing was changed.') from None
    return {'ok': True, 'codex': len(data['codex']), 'claude': len(data['claude'])}


@router.get('/preflight')
def preflight(request: Request):
    """Full idle check: opened on demand by the dialog, never by the status poll."""
    authorize(request)
    try:
        with ops.bound_settings():
            ops.recover_abandoned_check()
            # This backend answers for itself in-process: on current Hermes a plugin route that calls
            # its own backend over HTTP waits on itself until the call times out.
            result = ops.preflight(local_state)
            if ops.LOCK.exists():
                result['blockers'].append(IN_PROGRESS)
                result['ok'] = False
            result['scope'] = SCOPE
            return result
    except Exception as exc:
        raise HTTPException(409, safe_error(exc)) from None


@router.post('/switch')
def switch(request: Request, body: dict):
    authorize(request)
    try:
        with ops.bound_settings():
            return _switch_bound(body)
    except SettingsError as exc:
        raise HTTPException(409, str(exc)) from None


def _switch_bound(body):
    try:
        target = normalize_request(body)
    except (SwitchError, SettingsError) as exc:
        raise HTTPException(400, str(exc)) from None
    ops.recover_abandoned_check()
    # Only Desktop-owned backends may start the interactive worker. A service/session-0 caller cannot reopen the user's app.
    import psutil
    main = ops.desktop()
    if main.pid not in {p.pid for p in psutil.Process().parents()}:
        raise HTTPException(409, 'Open this control in Hermes Desktop, not a service or remote dashboard.')
    operation_id = uuid.uuid4().hex
    ops.LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ops.LOCK.open('x', encoding='utf-8') as f:
            json.dump({'operation_id': operation_id, 'pid': os.getpid()}, f)
    except FileExistsError:
        raise HTTPException(409, IN_PROGRESS) from None
    record = {'operation_id': operation_id, 'target': target, 'settings_sha256': settings_digest(), 'started_at': time.time(),
              'state': 'accepted', 'message': f'Checking active work before restarting for {describe(target)}.'}
    try:
        ops.atomic(ops.RECEIPT, record)
        pythonw = ops.runtime_python().with_name('pythonw.exe')
        if not pythonw.is_file():
            raise SwitchError('The hidden Windows Python launcher is unavailable.')
        sibling('worker_launch').launch(pythonw, HERE / 'switch_worker.py', operation_id, ops.ROOT)
    except Exception as exc:
        ops.LOCK.unlink(missing_ok=True)
        record.update(state='failed', message=safe_error(exc))
        ops.atomic(ops.RECEIPT, record)
        raise HTTPException(409, safe_error(exc)) from None
    return {'accepted': True, 'operation_id': operation_id}
