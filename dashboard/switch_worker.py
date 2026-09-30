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
        time.sleep(3)  # Let the confirmed UI response render before any shutdown.
        main_pid = desktop().pid
        if main_pid in {p.pid for p in psutil.Process().parents()}:
            # Also repair callers whose plugin API is still loaded from before
            # this update; they read this worker from disk on each invocation.
            from pathlib import Path
            from worker_launch import launch
            launch(sys.executable, Path(__file__).resolve(), operation_id, ops.ROOT)
            handed_off = True
            return
        wait_for(lambda: main_pid not in {p.pid for p in psutil.Process().parents()},
                 10, 'Worker is still attached to Desktop; no restart or switch was attempted.')
        evidence = switch_transaction(WindowsOps(record), record['target'])
        record.update(evidence)
        record.update(state='complete', message=f"{describe(record['target'])} verified after restart.")
    except BaseException as exc:
        # BaseException, not Exception: a Hermes import can end the process with SystemExit, and a receipt
        # left at "accepted" reads as pending forever. Only this plugin's own plain-language errors are shown;
        # anything else records its type, never its text, except Hermes's own exit reason, which is an
        # operational message.
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


if __name__ == '__main__':
    main(sys.argv[1])
