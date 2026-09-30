"""One-shot Desktop-only plugin activation; never changes accounts or services.

This is deliberately separate from the account-switch worker. It uses the
gateway's existing admission drain and freezes every plugin gate. A missing
guard is never treated as idle.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent / 'dashboard'))
import windows_ops as ops

RESULT = ops.ROOT / 'logs/codex-switch-repair-activation.json'
PRINCIPAL = 'codex-switch-repair-activation'


def close_window(main):
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    def close(hwnd, unused):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == main.pid:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
        return True
    user32.EnumWindows(callback_type(close), 0)


def inventory(backends):
    return sorted((b['profile'], b['pid'], b['born']) for b in backends)


def backend_snapshot(backend):
    """Return an allow-listed safety snapshot for one live backend."""
    result = {'profile': backend['profile'], 'pid': backend['pid'], 'port': backend['port']}
    try:
        local = ops.call(backend, '/local-state')
        result.update(
            plugin_route='ready',
            guard_installed=bool(local.get('guard_installed')),
            selected=local.get('selected'),
            uses_codex=local.get('uses_codex'),
            live_pools=local.get('live_pools', []),
            blockers=list(local.get('blockers', [])),
        )
        if not result['guard_installed']:
            result['blockers'].append('Desktop admission guard is not installed.')
        return result
    except ops.SwitchError:
        raise ops.SwitchError(f"The account-switch guard is unavailable in {backend['profile']}.") from None


def safety_snapshot(main, expected_inventory):
    """Check idle state without mutating anything."""
    if not ops.process_alive(main):
        raise ops.SwitchError('Hermes Desktop exited during the safety check.')
    backends = ops.backends(main)
    if inventory(backends) != expected_inventory:
        raise ops.SwitchError('Desktop backend inventory changed during the safety check.')
    service = ops.service()
    blockers = []
    if service['status'] != 'running':
        blockers.append('The Hermes gateway is not running.')
    states = ops.gateway_states()
    for home, state in states:
        if state['gateway_state'] != 'running':
            blockers.append(f'{home.name}: gateway is not accepting normally.')
        if state['active_agents']:
            blockers.append(f'{home.name}: gateway or scheduled work is active.')
    profiles = []
    for backend in backends:
        snapshot = backend_snapshot(backend)
        profiles.append(snapshot)
        blockers.extend(f"{backend['profile']}: {item}" for item in snapshot['blockers'])
    return {'profiles': profiles, 'blockers': blockers}


def wait_for_idle(main, expected_inventory, record, timeout=600):
    """Wait a bounded time; never convert unknown activity into permission."""
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        try:
            latest = safety_snapshot(main, expected_inventory)
            record['last_safety_check'] = latest
            if not latest['blockers']:
                ops.atomic(RESULT, record)
                return latest
            ops.atomic(RESULT, record)
        except (ops.SwitchError, psutil.Error, OSError) as exc:
            latest = {'blockers': [str(exc) if isinstance(exc, ops.SwitchError) else 'Safety state was temporarily unavailable.']}
            record['last_safety_check'] = latest
            ops.atomic(RESULT, record)
        time.sleep(1)
    raise ops.SwitchError('Desktop remained busy or unverifiable; no restart performed.')


def acquire_admission_boundary(backends, record):
    """Close shared gateway admission, then freeze every mounted plugin gate."""
    drain = ops.drain_control()

    drained = []
    frozen = []
    try:
        for home, _ in ops.gateway_states():
            if drain.drain_requested(home=home):
                raise ops.SwitchError('Another gateway maintenance operation owns the drain.')
            drain.write_drain_request(home=home, principal=PRINCIPAL, suppress_notification=False)
            drained.append(home)
        record['drain'] = {
            'principal': PRINCIPAL,
            'suppress_notification': False,
            'homes': [str(home) for home in drained],
        }
        ops.atomic(RESULT, record)
        ops.wait_for(lambda: ops.gateway_states(require_drain=True), 15,
                     'Gateway admission did not close.')
        if any(state['active_agents'] for _, state in ops.gateway_states(require_drain=True)):
            raise ops.SwitchError('Gateway work started while admission was closing.')

        for backend in backends:
            result = ops.call(backend, '/freeze', {})
            if result.get('ok') is not True:
                raise ops.SwitchError('Desktop work started during the admission boundary.')
            frozen.append(backend)
        record['admission'] = {
            'gateway_drain': True,
            'plugin_gates_frozen': [backend['profile'] for backend in frozen],
            'inventory_unchanged': True,
        }
        ops.atomic(RESULT, record)
        return drained, frozen
    except Exception:
        for backend in frozen:
            try:
                ops.call(backend, '/release', {})
            except ops.SwitchError:
                pass
        clear_our_drains(drained)
        raise


def launch_desktop():
    # Through the shell, so Desktop inherits the user's login environment rather than this worker's.
    ops.launch_desktop()


def clear_our_drains(drained):
    drain = ops.drain_control()
    for home in drained:
        if (drain.read_drain_request(home=home) or {}).get('principal') == PRINCIPAL:
            drain.clear_drain_request(home=home)
        remaining = drain.read_drain_request(home=home)
        if remaining is not None and remaining.get('principal') == PRINCIPAL:
            raise ops.SwitchError('The Desktop-only activation drain marker did not clear.')


def verify_reopened(old_main, old_desktop_created, old_inventory, selected, started_at, drained, record):
    new_main = ops.wait_for(ops.desktop, 60, 'Hermes Desktop did not reopen.')
    if new_main.pid == old_main.pid or new_main.create_time() <= old_desktop_created:
        raise ops.SwitchError('Hermes Desktop did not obtain a new process identity.')

    def ready_backends():
        backends = ops.backends(new_main)
        if sorted(b['profile'] for b in backends) != sorted(p[0] for p in old_inventory):
            return False
        if any(b['born'] <= started_at for b in backends):
            return False
        return backends

    new_backends = ops.wait_for(ready_backends, 90, 'Not every active Desktop profile reopened.')
    clear_our_drains(drained)
    record['drain_cleared'] = True
    ops.atomic(RESULT, record)
    ops.wait_for(
        lambda: all(state['gateway_state'] == 'running' for _, state in ops.gateway_states()),
        20, 'Gateway did not return to the running state after Desktop reopened.',
    )

    def verified():
        details = []
        for backend in ops.backends(new_main):
            try:
                local = ops.call(backend, '/local-state')
                status = ops.call(backend, '/status')
            except ops.SwitchError:
                return False
            if not local.get('guard_installed'):
                return False
            if local.get('uses_codex', True) and local.get('selected') != selected:
                return False
            if any(pool.get('selected') != selected for pool in local.get('live_pools', [])):
                return False
            if (status.get('codex') or {}).get('selected') != selected or status.get('blockers'):
                return False
            details.append({
                'profile': backend['profile'], 'pid': backend['pid'],
                'plugin_status': 'ok', 'plugin_local_state': 'ok',
                'selected': local.get('selected'),
                'uses_codex': local.get('uses_codex'),
                'guard_installed': local.get('guard_installed'),
                'live_pools': local.get('live_pools', []),
                'status_blockers': status.get('blockers', []),
                'status_scope': status.get('scope'),
            })
        return details

    evidence = ops.wait_for(verified, 90, 'Reopened account-switch plugin routes did not verify.')
    if ops.service()['pid'] != record['old_gateway_pid']:
        raise ops.SwitchError('The Hermes gateway changed PID during the Desktop-only relaunch.')
    if ops.selected(ops.accounts()) != selected or ops.common_selection(ops.codex_stores()) != selected:
        raise ops.SwitchError('Codex account priority changed during the Desktop-only relaunch.')
    record.update(
        state='verified', new_desktop_pid=new_main.pid, profiles=evidence,
        gateway_unchanged=True, gateway_restart=False,
        account_unchanged=True, account_change=False,
    )
    return new_main


def launch():
    """Hand this one-shot to the proven independent hidden worker launcher."""
    from worker_launch import launch as launch_worker
    pythonw = ops.runtime_python().with_name('pythonw.exe')
    if not pythonw.is_file():
        raise ops.SwitchError('The hidden Windows Python launcher is unavailable.')
    launch_worker(pythonw, Path(__file__).resolve(), '--run', ops.ROOT)


def run():
    record = {
        'schema': 2,
        'operation': 'desktop-only-codex-plugin-activation',
        'principal': PRINCIPAL,
        'state': 'waiting_for_idle',
        'started_at': time.time(),
        'gateway_restart': False,
        'account_change': False,
    }
    ops.atomic(RESULT, record)
    main = None
    old_inventory = []
    frozen = []
    drained = []
    try:
        main = ops.desktop()
        gateway = ops.service()['pid']
        selected = ops.selected(ops.accounts())
        old_backends = ops.backends(main)
        old_inventory = inventory(old_backends)
        record.update(
            old_desktop_pid=main.pid, old_desktop_created=main.create_time(),
            old_gateway_pid=gateway, selected=selected,
            active_profiles=[b['profile'] for b in old_backends],
        )
        ops.atomic(RESULT, record)
        safety = wait_for_idle(main, old_inventory, record)
        record.update(state='closing_admission', preflight=safety)
        ops.atomic(RESULT, record)
        drained, frozen = acquire_admission_boundary(old_backends, record)
        if inventory(ops.backends(main)) != old_inventory:
            raise ops.SwitchError('Desktop backend inventory changed after admission closed.')
        record['state'] = 'reopening_desktop'
        ops.atomic(RESULT, record)
        old_children = [p for p in main.children(recursive=True) if p.pid != os.getpid()]
        close_window(main)
        ops.wait_for(lambda: not ops.process_alive(main), 45, 'Hermes Desktop did not close gracefully.')
        ops.wait_for(lambda: all(not ops.process_alive(p) for p in old_children), 45,
                     'A Desktop child remained after graceful close; no second Desktop was started.')
        record['checkpoint'] = 'desktop_closed'
        ops.atomic(RESULT, record)
        launch_desktop()
        verify_reopened(main, record['old_desktop_created'], old_inventory, selected,
                        record['started_at'], drained, record)
    except Exception as exc:
        record.update(
            state='failed',
            error=str(exc) if isinstance(exc, ops.SwitchError) else type(exc).__name__,
        )
        if drained:
            try:
                clear_our_drains(drained)
                record['drain_cleared'] = True
            except Exception:
                record['drain_cleared'] = False
        if main is not None and not ops.process_alive(main):
            try:
                ops.desktop()
            except ops.SwitchError:
                launch_desktop()
    finally:
        for backend in frozen:
            try:
                ops.call(backend, '/release', {})
            except ops.SwitchError:
                pass
        record['finished_at'] = time.time()
        ops.atomic(RESULT, record)


if __name__ == '__main__':
    if sys.argv[1:] == ['--check']:
        main = ops.desktop()
        print(json.dumps({'desktop_pid': main.pid, 'backend_count': len(ops.backends(main)),
                          'gateway_pid': ops.service()['pid'], 'result_path': str(RESULT)}))
    elif sys.argv[1:] == ['--start']:
        launch()
        print(json.dumps({'state': 'detached', 'result_path': str(RESULT)}))
    elif sys.argv[1:] == ['--run']:
        run()
    else:
        raise SystemExit('Use --check or --run')
