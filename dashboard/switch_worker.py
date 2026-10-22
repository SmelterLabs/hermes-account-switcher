"""One-shot hidden worker. No scheduler, daemon, network listener, or model calls."""
import json
import os
import sys
import time
from switch_core import SwitchError, describe, switch_transaction
from settings import SettingsError, settings_digest
from windows_ops import WindowsOps, desktop, wait_for
import windows_ops as ops
import psutil


def failure_details(exc):
    """Record location and OS codes, never exception text, source lines, locals, or credentials."""
    from pathlib import Path
    frames = []
    cursor = exc.__traceback__
    while cursor is not None:
        frames.append({"file": Path(cursor.tb_frame.f_code.co_filename).name,
                       "function": cursor.tb_frame.f_code.co_name, "line": cursor.tb_lineno})
        cursor = cursor.tb_next
    details = {"type": type(exc).__name__, "frames": frames[-8:]}
    for name in ("errno", "winerror"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            details[name] = value
    return details


def main(operation_id):
    with ops.bound_settings():
        _main_bound(operation_id)


def _main_bound(operation_id):
    record = json.loads(ops.RECEIPT.read_text())
    if record['operation_id'] != operation_id or json.loads(ops.LOCK.read_text())['operation_id'] != operation_id:
        return
    if record.get('settings_sha256') != settings_digest():
        record.update(state='failed', message='Account-switch settings changed after the request; nothing was switched.',
                      finished_at=time.time())
        ops.atomic(ops.RECEIPT, record)
        ops.LOCK.unlink(missing_ok=True)
        return
    ops.atomic(ops.LOCK, {'operation_id': operation_id, 'pid': os.getpid()})
    handed_off = False
    try:
        # Let the confirmed UI response render before anything shuts down. A switch without a restart shuts
        # nothing down, so it starts at once.
        if not (ops.gateway_hosts_desktop() and ops.live_switch_possible(record['target'])):
            time.sleep(3)
        record.setdefault('checkpoints', []).append({'step': 'worker_started', 'time': time.time()})
        # On the unified gateway runtime the gateway, not Desktop, started this worker and Desktop stays open.
        main_pid = None if ops.gateway_hosts_desktop() else desktop().pid
        if main_pid is not None and main_pid in {p.pid for p in psutil.Process().parents()}:
            # Also repair callers whose plugin API is still loaded from before
            # this update; they read this worker from disk on each invocation.
            from pathlib import Path
            from worker_launch import launch
            launch(sys.executable, Path(__file__).resolve(), operation_id, ops.ROOT)
            handed_off = True
            return
        wait_for(lambda: main_pid is None or main_pid not in {p.pid for p in psutil.Process().parents()},
                 10, 'Worker is still attached to Desktop; no restart or switch was attempted.')
        evidence = switch_transaction(WindowsOps(record), record['target'])
        record.update(evidence)
        done = 'switched without a restart' if record.get('restarts') is False else 'verified after restart'
        record.update(state='complete', message=f"{describe(record['target'])} {done}.")
    except BaseException as exc:
        # BaseException, not Exception: a Hermes import can end the process with SystemExit, and a receipt
        # left at "accepted" reads as pending forever. Only this plugin's own plain-language errors are shown;
        # anything else records its type, never its text, except Hermes's own exit reason, which is an
        # operational message.
        record["failure_details"] = failure_details(exc)
        stage = record.get('failed_during') or record['state']
        if isinstance(exc, ops.LocalOperationFailed):
            message = f'A required local Hermes operation failed during {stage}.'
        elif isinstance(exc, (SwitchError, SettingsError)):
            message = str(exc)
        else:
            message = f'The switch failed safely during {stage} ({type(exc).__name__}).'
        if str(record.get('recovery', '')).startswith('restore_failed'):
            message += ' The previous account order could not be written back; check which account is selected.'
        record.update(state='failed', failed_during=stage, message=message)
        if isinstance(exc, SystemExit):
            record['exit_reason'] = str(exc.code)[:200]
    finally:
        if handed_off:
            return  # The independent successor owns the receipt and lock.
        record['finished_at'] = time.time()
        ops.atomic(ops.RECEIPT, record)
        if ops.LOCK.exists() and json.loads(ops.LOCK.read_text()).get('operation_id') == operation_id:
            ops.LOCK.unlink()
    # A live Codex switch is reported complete, and the lock is free for the next switch, before this watch:
    # it only puts the new order back if a turn that was already running writes the old one.
    if record.get('state') == 'complete' and (record.get('guard') or {}).get('state') == 'watching':
        try:
            ops.guard_codex_order(record)
        except BaseException as exc:
            record['guard'].update(state='done', ended='error', error=failure_details(exc))
            if ops.owns_receipt(operation_id):
                ops.atomic(ops.RECEIPT, record)


if __name__ == '__main__':
    main(sys.argv[1])
