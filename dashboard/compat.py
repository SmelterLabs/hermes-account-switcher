"""Hermes integration boundary: the public Hermes names this plugin calls, and nothing else.

Nothing here replaces, wraps or rebinds anything in Hermes, and no underscore-private name is
read. Every unavailable interface refuses switching before any process or credential changes.
"""
from importlib import import_module
from inspect import signature
from time import sleep


class UnsupportedRuntime(RuntimeError):
    pass


INTERFACES = {
    'pm.environments': {'committed_venv': ('project_root',)},
    'gateway.drain_control': {
        'write_drain_request': ('home', 'principal', 'suppress_notification'),
        'drain_requested': ('home',),
        'read_drain_request': ('home',),
        'clear_drain_request': ('home',),
    },
    # Hermes's own answer to "may this Desktop backend stop now": sessions running, starting or
    # queued, background delegations, scheduled jobs mid-run, and prompts waiting on a person.
    'hermes_cli.web_server_idle_proof': {'idle_proof': ()},
    'hermes_cli.auth': {'read_credential_pool': ('provider_id',)},
    'hermes_cli.config': {'save_env_value': ('key', 'value'), 'remove_env_value': ('key',)},
}

IDLE_UNAVAILABLE = "Hermes's idle check is unavailable; account switching is disabled."


def require_switch_interfaces():
    """Check the public signatures before a service stop or credential write.

    This is only the import contract. The gateway drain acknowledgement and a fresh idle
    answer from every Desktop backend are proved by WindowsOps.freeze before any consumer stops.
    """
    try:
        for module_name, names in INTERFACES.items():
            module = import_module(module_name)
            for name, required in names.items():
                function = getattr(module, name)
                parameters = signature(function).parameters
                if not callable(function) or any(key not in parameters for key in required):
                    raise ValueError()
    except Exception:
        raise UnsupportedRuntime('Hermes safety interfaces are unavailable or changed; account switching is disabled.') from None


def committed_venv(source):
    return import_module('pm.environments').committed_venv(source)


def safe_yaml_load(text):
    """Parse profile YAML with the host runtime's own parser.

    Stock Hermes ships ``hermes_yaml`` (ruamel-backed) and carries no PyYAML, and the
    dashboard API mount path installs no Python dependencies, so the parser must come
    from the host environment. Prefer the host shim; fall back to PyYAML for
    development environments that carry it. Missing or broken support refuses
    explicitly instead of letting the backend fail to import.
    """
    unavailable = []
    for module_name in ('hermes_yaml', 'yaml'):
        try:
            module = import_module(module_name)
        except ImportError as exc:
            unavailable.append(exc)
            continue
        try:
            return module.safe_load(text)
        except Exception:
            raise UnsupportedRuntime(
                f'{module_name} could not parse the profile configuration; account switching is disabled.') from None
    raise UnsupportedRuntime(
        'No YAML parser is available in this Hermes runtime (hermes_yaml or PyYAML); '
        'profile plugin configuration cannot be verified and account switching is disabled.') from None


def drain_control():
    return import_module('gateway.drain_control')


def gateway_hosts_desktop():
    """True on Hermes with the unified gateway runtime (v0.22 on), where Hermes Desktop starts no backend
    of its own: it attaches to the gateway, which serves the dashboard and this plugin and runs every turn.
    The gateway is then the only account consumer, and this plugin's routes run inside it."""
    from importlib.util import find_spec
    try:
        return find_spec('hermes_cli.gateway_runtime') is not None
    except Exception:
        return False


def desktop_idle_blockers():
    """Why this Desktop backend may not be closed right now, in plain words; empty when it is idle.

    The answer is Hermes's own ``idle_proof`` for this process, the probe Hermes Desktop uses before
    it retires a backend. An unreadable answer is a blocker, never idle.
    """
    try:
        # Hermes also reports its admission ledger while one admitted request is merely in flight (seen in the
        # first seconds after Desktop opens), so that answer is asked again for about two seconds before it counts.
        for attempt in range(5):
            proof = import_module('hermes_cli.web_server_idle_proof').idle_proof()
            idle, reason, detail = proof.get('idle'), proof.get('reason'), proof.get('detail')
            if detail != 'retirement_admission' or attempt == 4:
                break
            sleep(.5)
    except Exception:
        return [IDLE_UNAVAILABLE]
    if idle is True:
        return []
    if idle is None:
        return ['Hermes could not tell whether Desktop is idle; retry in a moment.']
    if reason == 'awaiting_human_input':
        return ['A conversation is waiting for input.']
    detail = str(detail or '')
    if detail.startswith('session:'):
        return ['A conversation is running, initializing, or waiting for input.']
    if detail == 'delegation':
        return ['A conversation owns running background work.']
    if detail.startswith('cron:'):
        return ['A scheduled job is running.']
    if detail == 'retirement_admission':
        return ['Hermes is handling a request; retry in a moment.']
    return ['Desktop is busy.']


def read_codex_pool():
    return import_module('hermes_cli.auth').read_credential_pool('openai-codex')


def set_env(key, value):
    config = import_module('hermes_cli.config')
    if value == '--remove':
        return config.remove_env_value(key)
    return config.save_env_value(key, value)
