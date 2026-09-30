"""Private Hermes integration boundary. Every unavailable safety seam blocks switching."""
from importlib import import_module
from inspect import signature


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
    'tui_gateway.session_lifecycle': {'_session_has_active_delegations': ('sid', 'session')},
    'hermes_cli.web_server': {'_require_token': ('request',)},
    'hermes_cli.auth': {'read_credential_pool': ('provider_id',)},
    'hermes_cli.config': {'save_env_value': ('key', 'value'), 'remove_env_value': ('key',)},
}


def _referenced_names(code):
    names = set(code.co_names)
    for const in code.co_consts:
        if hasattr(const, 'co_names'):
            names |= _referenced_names(const)
    return names


def gate_target(server):
    """The one function every Desktop request passes through, inline or on a worker thread."""
    return '_handle_admitted_request' if callable(getattr(server, '_handle_admitted_request', None)) else 'handle_request'


def require_gated_admission(server):
    """Refuse a dispatcher that can start work through a function the freeze does not wrap."""
    target = gate_target(server)
    routes = {name for name in _referenced_names(server.dispatch.__code__) if 'handle' in name and 'request' in name}
    if not routes or not routes <= {'handle_request', target}:
        raise ValueError()
    if target != 'handle_request' and target not in _referenced_names(server.handle_request.__code__):
        raise ValueError()


def require_switch_interfaces():
    """Check known private signatures before a service stop or credential write.

    This is only the import contract. The gateway drain acknowledgement and fresh
    activity count are proved by WindowsOps.freeze before any consumer stops.
    """
    try:
        for module_name, names in INTERFACES.items():
            module = import_module(module_name)
            for name, required in names.items():
                function = getattr(module, name)
                parameters = signature(function).parameters
                if not callable(function) or any(key not in parameters for key in required):
                    raise ValueError()
        server = import_module('tui_gateway.server')
        for name in ('handle_request', '_err', '_session_pending_kind'):
            if not callable(getattr(server, name)):
                raise ValueError()
        getattr(server, '_sessions_lock')
        if not isinstance(getattr(server, '_sessions'), dict):
            raise ValueError()
        require_gated_admission(server)
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


def server():
    return import_module('tui_gateway.server')


def has_active_delegations(sid, session):
    return import_module('tui_gateway.session_lifecycle')._session_has_active_delegations(sid, session)


def require_token(request):
    return import_module('hermes_cli.web_server')._require_token(request)


def read_codex_pool():
    return import_module('hermes_cli.auth').read_credential_pool('openai-codex')


def set_env(key, value):
    config = import_module('hermes_cli.config')
    if value == '--remove':
        return config.remove_env_value(key)
    return config.save_env_value(key, value)
