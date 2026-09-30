"""Enrol a Claude account into its Hermes-owned login folder, then prove who owns the login.

Usage (a visible console, because the CLI opens a browser and waits for you):
    python claude_login.py <configured-key> | all

Each account uses the absolute directory in local settings, independent of Claude Code's
own ~/.claude. After the browser login completes, the token owner is checked at
Anthropic; a login as the wrong account is discarded so the folder can never hold a mismatched identity.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'dashboard'))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def hermes_python():
    """Hermes's own Python environment, which brings what the plugin's backend needs."""
    from settings import load_settings
    source = load_settings().hermes_source
    sys.path.insert(0, str(source))
    from pm.environments import committed_venv
    return Path(committed_venv(source)) / 'Scripts' / 'python.exe'


try:
    import psutil  # noqa: F401
except ImportError:
    # Started with a Python that is not Hermes's (setup.cmd uses the one Hermes ships with): run again under
    # Hermes's environment, once.
    if os.environ.get('ACCOUNT_SWITCH_RESTARTED'):
        raise SystemExit("Hermes's Python environment was not found; open Hermes once, then run this again.")
    try:
        python = hermes_python()
    except Exception:
        raise SystemExit("Hermes's Python environment was not found; open Hermes once, then run this again.") from None
    raise SystemExit(subprocess.call([str(python), str(Path(__file__).resolve()), *sys.argv[1:]],
                                     env={**os.environ, 'ACCOUNT_SWITCH_RESTARTED': '1'}))

import windows_ops as ops
from switch_core import CLAUDE, SwitchError


def enrol(key):
    label, email = CLAUDE[key]
    folder = ops.claude_dir(key)
    folder.mkdir(parents=True, exist_ok=True)
    print(f'\n=== {label} ({email}) ===\nFolder: {folder}')
    try:
        identity = ops.claude_identity(key)
        print(f'Already logged in as {identity["email"]} (verified by {identity["method"]}). Skipping.')
        return True
    except SwitchError:
        pass
    exe = ops.claude_cli()
    if not exe:
        print('The claude command was not found; install Claude Code first.')
        return False
    env = {k: v for k, v in os.environ.items() if not k.startswith('ANTHROPIC_') and not k.startswith('CLAUDE_')}
    env['CLAUDE_CONFIG_DIR'] = str(folder)
    # Never let the CLI open the default browser: an existing claude.com session there completes the login
    # instantly as WHATEVER account is signed in. BROWSER points at an inert program, so the CLI prints the link.
    env['BROWSER'] = str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/where.exe')
    print(f'No browser will open. Copy the link printed below into a PRIVATE/incognito window and sign in as {email}.')
    # The CLI's own raw-mode prompt rejects pasted text in some consoles, so the code is taken by a plain
    # Python prompt and written to the CLI's stdin.
    proc = subprocess.Popen([exe, 'auth', 'login', '--claudeai', '--email', email], env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    import queue
    import threading
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in iter(proc.stdout.readline, '')], daemon=True).start()
    link = None
    while link is None and proc.poll() is None:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            continue
        if 'https://' in line:
            link = line[line.index('https://'):].strip()
    if link is None:
        print('The CLI did not print a sign-in link; nothing was stored.')
        return False
    # Put the link on the clipboard: selecting text in the terminal and pressing Ctrl+C interrupts this script.
    subprocess.run(['clip.exe'], input=link, text=True)
    print('\nThe sign-in link is on your CLIPBOARD. In a private browser window: Ctrl+V into the '
          f'address bar, Enter, sign in as {email}, Authorize.\n')
    try:
        code = input('Then paste the code the page shows here (right-click or Ctrl+V) and press Enter: ').strip()
    except (KeyboardInterrupt, EOFError):
        proc.kill()
        print('\nCancelled; nothing was stored. Run this again when ready.')
        return False
    try:
        proc.stdin.write(code + '\n')
        proc.stdin.flush()
        proc.wait(timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
    # CLI output may contain account or authorization metadata. Do not echo it.
    try:
        identity = ops.claude_identity(key)
    except SwitchError as exc:
        print(f'FAILED: {exc}')
        print('The CLI login remains in the configured folder; switching stays blocked until identity verifies.')
        return False
    print(f'OK: {label} folder holds a login for {identity["email"]}.')
    return True


if __name__ == '__main__':
    choice = sys.argv[1] if len(sys.argv) > 1 else 'all'
    keys = list(CLAUDE) if choice == 'all' else [choice]
    if any(k not in CLAUDE for k in keys):
        raise SystemExit('Choose one configured Claude account key or all.')
    results = {k: enrol(k) for k in keys}
    print('\nSummary: ' + ', '.join(f'{k}: {"ok" if v else "not logged in"}' for k, v in results.items()))
