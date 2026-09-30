"""Launch a hidden worker through an exiting parent, outside tree shutdown."""
import os
from pathlib import Path
import subprocess
import sys


def clean_env(home):
    """This process's environment without the interpreter overrides a Desktop backend runs under. With the
    backend's PYTHONPATH inherited, importing any Hermes module makes Hermes's startup code relaunch the
    process under another interpreter and exit, which ended every switch before its first check."""
    env = {key: value for key, value in os.environ.items() if key.upper() not in ('PYTHONPATH', 'PYTHONHOME')}
    env['HERMES_HOME'] = str(home)
    return env


def launch(pythonw, worker, operation_id, home):
    result = subprocess.run([str(pythonw), str(Path(__file__).resolve()), str(worker), operation_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(Path(worker).parent), env=clean_env(home),
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    if result.returncode:
        raise RuntimeError('Independent switch worker could not be launched.')


def worker_start():
    """How the worker is started: its own console, created hidden.

    Hermes's code, which the worker loads, runs git and other console programs without hiding them. A worker
    with no console (pythonw, or CREATE_NO_WINDOW) makes each of those open a new console, and Windows 11
    hands every new console to Windows Terminal, which shows it: six or seven flashes per switch on a real
    PC. A console created hidden is not handed over, and everything the worker starts shares it."""
    console_python = Path(sys.executable).with_name('python.exe')
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return (str(console_python if console_python.is_file() else sys.executable), info,
            subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)


if __name__ == '__main__':
    # This short-lived parent exits before the worker can close Desktop. Merely
    # DETACHED_PROCESS does not escape taskkill /T; breakaway also avoids an
    # inherited kill-on-close Job Object. Never fall back to a coupled worker.
    python, startupinfo, flags = worker_start()
    subprocess.Popen([python, sys.argv[1], sys.argv[2]],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        startupinfo=startupinfo, creationflags=flags)
