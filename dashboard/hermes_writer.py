"""Write every profile's account choice through Hermes's own functions, in one Hermes Python.

Each `hermes auth priority`, `hermes auth reset` and `hermes config set` starts Hermes afresh, seconds each on
Windows, so a switch across ten profiles took most of a minute. This helper is started once, at the beginning of
a switch, so its Hermes import overlaps the switch's own checks. It then runs the same functions those commands
run, for every profile in turn, with Hermes's own home override (the one its multiplexed gateway uses), so the
locking (auth.lock), atomic saves and caches are Hermes's own. It never writes a file itself.

Protocol, one JSON object per line:
- on start it prints {"ready": true} or {"ready": false, "reason": "..."}; the caller then uses the commands;
- each job line is answered with {"ok": true, "reset": [...], "seconds": n} or {"ok": false, "error": "..."};
- it exits when its input closes.
Nothing else is printed: Hermes's own messages are discarded, and an error carries only its type, never text
that could hold account details.
"""
import contextlib
import io
import json
import sys
import time

CODEX = 'openai-codex'


def interfaces():
    """Hermes's functions this helper calls, imported and checked; raises when any is missing."""
    import hermes_constants
    from agent import credential_pool
    from hermes_cli import config
    pool_class = credential_pool.CredentialPool
    for owner, name in ((hermes_constants, 'set_hermes_home_override'), (hermes_constants, 'reset_hermes_home_override'),
                        (credential_pool, 'load_pool'), (pool_class, 'move_entry'), (pool_class, 'reset_status'),
                        (config, 'save_env_value'), (config, 'remove_env_value'), (config, 'set_config_value')):
        if not callable(getattr(owner, name, None)):
            raise LookupError(name)
    return hermes_constants, credential_pool, config


@contextlib.contextmanager
def home(hermes_constants, path):
    token = hermes_constants.set_hermes_home_override(path)
    try:
        yield
    finally:
        hermes_constants.reset_hermes_home_override(token)


def run(job, hermes_constants, credential_pool, config):
    reset = []
    for store in job.get('codex') or []:
        with home(hermes_constants, store['home']):
            pool = credential_pool.load_pool(CODEX)
            if pool.move_entry(store['id'], 0) is None:
                raise LookupError('codex login')
            if store.get('reset'):
                if pool.reset_status(store['id']) is None:
                    raise LookupError('codex login')
                reset.append(store['home'])
    claude = job.get('claude')
    if claude:
        for path in claude['homes']:
            with home(hermes_constants, path):
                # .env first, then the setting, as the commands did.
                if claude['value'] is None:
                    config.remove_env_value(claude['variable'])
                else:
                    config.save_env_value(claude['variable'], claude['value'])
                config.set_config_value(claude['setting'], claude['value'] or '')
    return reset


def main():
    out = sys.stdout
    # Hermes prints confirmations ("✓ Set ..."); keep the protocol line the only output.
    sys.stdout = io.StringIO()

    def say(message):
        out.write(json.dumps(message) + '\n')
        out.flush()
        sys.stdout = io.StringIO()

    try:
        loaded = interfaces()
    except BaseException as exc:  # an import can end the process with SystemExit
        say({'ready': False, 'reason': type(exc).__name__})
        return
    say({'ready': True})
    for line in sys.stdin:
        began = time.monotonic()
        try:
            reset = run(json.loads(line), *loaded)
            say({'ok': True, 'reset': reset, 'seconds': round(time.monotonic() - began, 2)})
        except BaseException as exc:  # Hermes refuses a config write with SystemExit
            say({'ok': False, 'error': type(exc).__name__})


if __name__ == '__main__':
    main()
