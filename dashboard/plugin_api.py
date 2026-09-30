"""Authenticated Desktop plugin API. Account secrets never leave this process."""
import json
import os
from pathlib import Path
import sys
import time
import uuid
from fastapi import APIRouter, HTTPException, Request

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from switch_core import SwitchError, describe, identify, normalize_request
from settings import SettingsError, load_settings, settings_path
from settings import settings_digest
from compat import require_token, read_codex_pool, server as gateway_server
import windows_ops as ops
import first_run

router = APIRouter()
gate = None
SCOPE = 'All Hermes profiles on this PC'
# What status answers before the first setup: nothing selected, nothing to choose from, and the reason.
NOT_SET_UP = {'setup_needed': True, 'codex': {'selected': None, 'accounts': [], 'effective': None, 'warnings': []},
              'claude': {'selected': None, 'state': 'unset', 'accounts': []}, 'last_operation': None,
              'in_progress': False, 'blockers': [], 'scope': SCOPE}
IN_PROGRESS = 'An account switch is already in progress.'


def current_gate():
    global gate
    if gate is None:
        from desktop_gate import install
        gate = install()
    return gate


def authorize(request):
    require_token(request)
    if not request.client or request.client.host not in ('127.0.0.1', '::1', 'testclient'):
        raise HTTPException(403, 'This switch is only available locally on this PC.')


def safe_error(exc):
    return str(exc) if isinstance(exc, (SwitchError, SettingsError)) else 'Account switch check failed safely; no secret details are logged.'


def local_state():
    guard = current_gate()
    server = gateway_server()
    # Each profile owns its logins (#111724); a profile without Codex rows does not use Codex.
    rows = read_codex_pool()
    acc = identify(rows) if rows and ops.EMAILS else None
    live = []
    with server._sessions_lock:
        for session in server._sessions.values():
            pool = getattr(session.get('agent'), '_credential_pool', None)
            if pool is None or pool.provider != 'openai-codex':
                continue
            rows = [e.to_dict() for e in pool.entries()]
            if not ops.EMAILS:
                continue
            live_accounts = identify(rows)
            current_entry = pool.peek()
            current = next((key for key, value in live_accounts.items()
                            if current_entry is not None and any(
                                entry['id'] == current_entry.id for entry in value['entries'])), None)
            live.append({'selected': current})
    with guard.lock:
        blockers = guard.busy()
    return {'pid': os.getpid(), 'selected': ops.selected(acc) if acc else None, 'uses_codex': acc is not None,
            'live_pools': live,
            # What the Claude plugin will hand the `claude` CLI from THIS process (the .env it loaded at start).
            'claude_config_dir': os.environ.get(ops.CLAUDE_VAR) or None,
            'blockers': blockers, 'guard_installed': getattr(server, getattr(guard, 'target', 'handle_request'), None) == guard.dispatch}


@router.get('/local-state')
def get_local_state(request: Request):
    authorize(request)
    try:
        with ops.bound_settings():
            return local_state()
    except Exception as exc:
        raise HTTPException(409, safe_error(exc)) from None


@router.post('/freeze')
def freeze(request: Request):
    authorize(request)
    with ops.bound_settings():
        return current_gate().freeze()


@router.post('/release')
def release(request: Request):
    authorize(request)
    with ops.bound_settings():
        current_gate().release()
    return {'ok': True}


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
        from worker_launch import launch
        launch(pythonw, HERE / 'switch_worker.py', operation_id, ops.ROOT)
    except Exception as exc:
        ops.LOCK.unlink(missing_ok=True)
        record.update(state='failed', message=safe_error(exc))
        ops.atomic(ops.RECEIPT, record)
        raise HTTPException(409, safe_error(exc)) from None
    return {'accepted': True, 'operation_id': operation_id}
