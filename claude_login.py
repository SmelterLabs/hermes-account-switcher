"""Enrol a Claude account into its Hermes-owned login folder, then prove who owns the login.

Usage (a visible console, because the CLI opens a browser and waits for you):
    python claude_login.py <configured-key> | all

Each account uses the absolute directory in local settings, independent of Claude Code's
own ~/.claude. After the browser login completes, the token owner is checked at
Anthropic; a login as the wrong account is discarded so the folder can never hold a mismatched identity.
A verified login is then followed by the account's year-long sign-in (`claude setup-token`), kept only when
Anthropic places it in the same account; an account that already holds a good one is left alone.
"""
import os
import re
import shutil
import subprocess
import sys
import time
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
from switch_core import CLAUDE, SwitchError, WrongAccount


def discard_login(folder):
    """Remove the login the CLI just wrote, and nothing else in the folder."""
    (Path(folder) / '.credentials.json').unlink(missing_ok=True)


def discard_year_token(folder):
    """Remove a year-long sign-in that ran out, is refused or belongs to someone else, and its record."""
    for name in (ops.YEAR_TOKEN_FILE, ops.YEAR_TOKEN_RECORD):
        (Path(folder) / name).unlink(missing_ok=True)


def cli_env(folder):
    env = {k: v for k, v in os.environ.items() if not k.startswith('ANTHROPIC_') and not k.startswith('CLAUDE_')}
    env['CLAUDE_CONFIG_DIR'] = str(folder)
    # Never let the CLI open the default browser: an existing claude.com session there completes the login
    # instantly as WHATEVER account is signed in. BROWSER points at an inert program, so the CLI prints the link.
    env['BROWSER'] = str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/where.exe')
    return env


class HiddenTerminal:
    """A Windows pseudo console (ConPTY) the user never sees. `claude setup-token` prints nothing unless it
    runs in a terminal, and it prints the token there; inside this one the token is read off its output
    and goes straight into the login folder, never onto the screen."""

    def __init__(self, argv, env, cols=2000, rows=50):
        import ctypes
        import threading
        from ctypes import wintypes as w
        self._c, self._w = ctypes, w
        k32 = self._k32 = ctypes.WinDLL('kernel32', use_last_error=True)

        class COORD(ctypes.Structure):
            _fields_ = [('X', w.SHORT), ('Y', w.SHORT)]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR), ('lpTitle', w.LPWSTR),
                        ('dwX', w.DWORD), ('dwY', w.DWORD), ('dwXSize', w.DWORD), ('dwYSize', w.DWORD),
                        ('dwXCountChars', w.DWORD), ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD),
                        ('dwFlags', w.DWORD), ('wShowWindow', w.WORD), ('cbReserved2', w.WORD),
                        ('lpReserved2', ctypes.c_void_p), ('hStdInput', w.HANDLE), ('hStdOutput', w.HANDLE),
                        ('hStdError', w.HANDLE)]

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [('StartupInfo', STARTUPINFOW), ('lpAttributeList', ctypes.c_void_p)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE), ('dwProcessId', w.DWORD),
                        ('dwThreadId', w.DWORD)]

        k32.CreatePseudoConsole.argtypes = [COORD, w.HANDLE, w.HANDLE, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        k32.CreatePseudoConsole.restype = ctypes.c_long
        k32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
        k32.CreatePipe.argtypes = [ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE), ctypes.c_void_p, w.DWORD]
        k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD,
                                                          ctypes.POINTER(ctypes.c_size_t)]
        k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                  ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        k32.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                                       ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(PROCESS_INFORMATION)]
        k32.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        k32.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        k32.CloseHandle.argtypes = [w.HANDLE]
        k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]

        in_r, in_w, out_r, out_w = w.HANDLE(), w.HANDLE(), w.HANDLE(), w.HANDLE()
        if not (k32.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0)
                and k32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._hpc = ctypes.c_void_p()
        if k32.CreatePseudoConsole(COORD(cols, rows), in_r, out_w, 0, ctypes.byref(self._hpc)):
            raise OSError('Windows could not create a pseudo console')
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        self._attrs = ctypes.create_string_buffer(size.value)
        if not (k32.InitializeProcThreadAttributeList(self._attrs, 1, 0, ctypes.byref(size))
                and k32.UpdateProcThreadAttribute(self._attrs, 0, 0x00020016, self._hpc,  # ..._PSEUDOCONSOLE
                                                  ctypes.sizeof(ctypes.c_void_p), None, None)):
            raise ctypes.WinError(ctypes.get_last_error())
        info = STARTUPINFOEXW()
        info.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # STARTF_USESTDHANDLES with no handles: otherwise the child takes this script's own redirected
        # handles instead of the pseudo console's.
        info.StartupInfo.dwFlags = 0x100
        info.lpAttributeList = ctypes.cast(self._attrs, ctypes.c_void_p)
        block = ctypes.create_unicode_buffer(''.join(f'{k}={v}\0' for k, v in sorted(env.items())) + '\0')
        self._pi = PROCESS_INFORMATION()
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
        if not k32.CreateProcessW(None, line, None, None, False, 0x00080000 | 0x00000400, block, None,
                                  ctypes.byref(info), ctypes.byref(self._pi)):
            raise ctypes.WinError(ctypes.get_last_error())
        k32.CloseHandle(in_r)
        k32.CloseHandle(out_w)
        self._in, self._out, self._data = in_w, out_r, b''
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        buf, got = self._c.create_string_buffer(8192), self._w.DWORD()
        while self._k32.ReadFile(self._out, buf, 8192, self._c.byref(got), None) and got.value:
            self._data += buf.raw[:got.value]

    def text(self):
        """What the terminal showed, without its control sequences."""
        raw = self._data.decode('utf-8', 'replace')
        return re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()=>][0-9A-Za-z]?', ' ', raw)

    def type(self, text):
        data = text.encode('utf-8')
        self._k32.WriteFile(self._in, data, len(data), self._c.byref(self._w.DWORD()), None)

    def running(self):
        return self._k32.WaitForSingleObject(self._pi.hProcess, 0) == 0x102  # WAIT_TIMEOUT

    def close(self):
        if self.running():
            self._k32.TerminateProcess(self._pi.hProcess, 1)
        self._k32.ClosePseudoConsole(self._hpc)
        for handle in (self._in, self._pi.hProcess, self._pi.hThread):
            self._k32.CloseHandle(handle)


LINK = re.compile(r'https://claude\.(?:ai|com)/\S+')
YEAR_TOKEN = re.compile(r'sk-ant-oat01-[A-Za-z0-9_-]{40,}')


def wait_for(terminal, pattern, seconds):
    end = time.time() + seconds
    while time.time() < end:
        found = pattern.search(terminal.text())
        if found or not terminal.running():
            return found and found.group(0)
        time.sleep(0.3)
    return None


def make_year_token(key):
    """Mint the account's year-long sign-in with `claude setup-token` and keep it only if it belongs to the
    same Anthropic account as the folder's own login, which must already be verified."""
    label, email = CLAUDE[key]
    folder = ops.claude_dir(key)
    exe = ops.claude_cli()
    if not exe:
        print('The claude command was not found; install Claude Code first.')
        return False
    print(f'\nNow the year-long sign-in, so {label} does not need signing in again for a year.')
    try:
        terminal = HiddenTerminal([exe, 'setup-token'], cli_env(folder))
    except OSError as exc:
        print(f'Claude Code could not be started for it ({type(exc).__name__}); the four-week sign-in stays in use.')
        return False
    try:
        link = wait_for(terminal, LINK, 60)
        if not link:
            print('Claude Code did not print a sign-in link; the four-week sign-in stays in use.')
            return False
        subprocess.run(['clip.exe'], input=link, text=True)
        print('\nA new sign-in link is on your CLIPBOARD. In a private browser window: Ctrl+V into the address bar, '
              f'Enter, sign in as {email}, Authorize.\n')
        try:
            code = input('If the page shows a code, paste it here (right-click or Ctrl+V); either way press Enter: ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\nCancelled; nothing was stored. The four-week sign-in stays in use.')
            return False
        if code:
            terminal.type(code + '\r')
        token = wait_for(terminal, YEAR_TOKEN, 120)
    finally:
        terminal.close()
    if not token:
        print('Claude Code did not hand over a year-long sign-in; nothing was stored. The four-week sign-in stays in use.')
        return False
    # The token cannot name its owner; its organization must match the verified login's.
    try:
        minted = ops._token_organization(token)
        known = ops._token_organization(ops._credentials(key)['accessToken'])
    except Exception as exc:
        code = getattr(exc, 'code', None)
        print(f'The new sign-in could not be checked with Anthropic ({f"HTTP {code}" if isinstance(code, int) else type(exc).__name__}); '
              'nothing was stored. Run this again.')
        return False
    if not minted or minted != known:
        print(f'FAILED: the year-long sign-in was made for another account than {email}; it was not stored. '
              'Run this again in a private browser window.')
        return False
    (folder / ops.YEAR_TOKEN_FILE).write_text(token, encoding='utf-8')
    ops.record_year_token(key, minted)
    print(f'OK: {label} now has a year-long sign-in; it runs out on '
          f'{time.strftime("%b %d, %Y", time.localtime(ops.year_token_ends(key)))}.')
    return True


def year_token_state(key):
    """``ok`` (good and recorded), ``none``, ``unchecked`` (only a verified login can vouch for it) or
    ``unknown`` (Anthropic could not be asked). A token made outside this plugin is recorded once the
    folder's own login proves it is the same account; a dead or foreign one is removed."""
    email = CLAUDE[key][1]
    if not ops.year_token(key):
        return 'none'
    try:
        ops.claude_identity(key)
    except (ops.YearTokenDead, WrongAccount) as exc:
        print(f'{exc}\nThat year-long sign-in was removed; a new one is made below.')
        discard_year_token(ops.claude_dir(key))
        return 'none'
    except ops.YearTokenUnchecked:
        return 'unchecked'
    except SwitchError as exc:
        print(f'The year-long sign-in could not be checked: {exc}')
        return 'unknown'
    if not ops.year_token_record(key):
        try:
            ops.record_year_token(key, ops._token_organization(ops.year_token(key)),
                                  created=(ops.claude_dir(key) / ops.YEAR_TOKEN_FILE).stat().st_mtime)
        except Exception as exc:
            print(f'The year-long sign-in could not be recorded just now ({type(exc).__name__}); run this again.')
            return 'unknown'
    when = time.strftime('%b %d, %Y', time.localtime(ops.year_token_ends(key)))
    print(f'Has a year-long sign-in for {email}, good until {when}.')
    return 'ok'


def enrol(key):
    label, email = CLAUDE[key]
    folder = ops.claude_dir(key)
    folder.mkdir(parents=True, exist_ok=True)
    print(f'\n=== {label} ({email}) ===\nFolder: {folder}')
    state = year_token_state(key)
    if state in ('ok', 'unknown'):
        return state == 'ok'
    try:
        identity = ops._login_identity(key)
        print(f'Logged in as {identity["email"]} (verified by {identity["method"]}).')
    except SwitchError:
        if not sign_in(key):
            return False
    if state == 'unchecked' and year_token_state(key) == 'ok':
        return True
    make_year_token(key)
    return True


def sign_in(key):
    """The folder's own (four-week) login, through a browser sign-in."""
    label, email = CLAUDE[key]
    folder = ops.claude_dir(key)
    exe = ops.claude_cli()
    if not exe:
        print('The claude command was not found; install Claude Code first.')
        return False
    env = cli_env(folder)
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
        identity = ops._login_identity(key)
    except WrongAccount as exc:
        # The folder may never hold a login for another account: a switch would hand it to Hermes as this one.
        discard_login(folder)
        print(f'FAILED: {exc}')
        print('That login was discarded; the folder holds no login now. Run this again in a private browser window.')
        return False
    except SwitchError as exc:
        print(f'FAILED: {exc}')
        print('The login could not be verified and stays in the folder; switching stays blocked until it verifies.')
        return False
    print(f'OK: {label} folder holds a login for {identity["email"]}.')
    return True


if __name__ == '__main__':
    choice = sys.argv[1] if len(sys.argv) > 1 else 'all'
    keys = list(CLAUDE) if choice == 'all' else [choice]
    if any(k not in CLAUDE for k in keys):
        raise SystemExit('Choose one configured Claude account key or all.')
    results = {k: enrol(k) for k in keys}
    kinds = {k: ('year-long sign-in' if ops.year_token(k) and ops.year_token_record(k) else 'four-week sign-in') if v else 'not logged in'
             for k, v in results.items()}
    print('\nSummary: ' + ', '.join(f'{k}: {kind}' for k, kind in kinds.items()))
