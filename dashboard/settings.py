"""Validated, credential-free local settings outside the installed plugin tree."""
import json
import os
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlparse


class SettingsError(RuntimeError):
    pass


_PINNED = ContextVar('account_switch_settings', default=None)


@dataclass(frozen=True)
class Account:
    label: str
    email: str
    directory: Path | None = None


@dataclass(frozen=True)
class Settings:
    hermes_root: Path
    hermes_source: Path
    desktop_exe: Path
    gateway_service: str | None
    gateway_health_url: str | None
    codex: dict[str, Account]
    claude: dict[str, Account]


def settings_path():
    override = os.environ.get('HERMES_SWITCH_SETTINGS')
    if override:
        return Path(override)
    home = os.environ.get('HERMES_HOME')
    if home and Path(home).parent.name.lower() == 'profiles':
        home = str(Path(home).parent.parent)
    if not home:
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            raise SettingsError('Set HERMES_SWITCH_SETTINGS or HERMES_HOME to locate account-switch settings.')
        home = str(Path(local) / 'hermes')
    return Path(home) / 'plugin-data' / 'codex-account-switch' / 'settings.json'


def _absolute(value, name):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise SettingsError(f'{name} must be an absolute path in account-switch settings.')
    return Path(value)


def _accounts(value, provider):
    if not isinstance(value, dict):
        raise SettingsError(f'{provider} must be an object in account-switch settings.')
    result = {}
    emails = set()
    for key, row in value.items():
        if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', key):
            raise SettingsError(f'{provider} account keys must be simple lowercase names.')
        if not isinstance(row, dict) or set(row) != ({'label', 'email', 'directory'} if provider == 'claude' else {'label', 'email'}):
            raise SettingsError(f'{provider}.{key} has missing or unexpected fields.')
        label, email = row['label'], row['email']
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise SettingsError(f'{provider}.{key} needs a short label.')
        if not isinstance(email, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            raise SettingsError(f'{provider}.{key} needs an expected email identity.')
        email = email.lower()
        if email in emails:
            raise SettingsError(f'{provider} expected identities must be distinct.')
        emails.add(email)
        directory = _absolute(row['directory'], f'{provider}.{key}.directory') if provider == 'claude' else None
        result[key] = Account(label, email, directory)
    return result


def load_settings(path=None):
    pinned = _PINNED.get()
    if pinned is not None and path is None:
        return pinned[0]
    path = Path(path) if path is not None else settings_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise SettingsError(f'Account-switch settings are missing. Run setup.cmd from the plugin\'s folder to create them, or copy settings.example.json to {path} and fill it in.') from None
    except OSError:
        raise SettingsError('Account-switch settings could not be read.') from None
    return _parse_settings(raw)


def _parse_settings(raw):
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise SettingsError('Account-switch settings could not be read or parsed.') from None
    required = {'hermes_root', 'hermes_source', 'desktop_exe', 'gateway_service', 'codex', 'claude'}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - (required | {'gateway_health_url'}):
        raise SettingsError('Account-switch settings have missing or unexpected fields.')
    root = _absolute(data['hermes_root'], 'hermes_root')
    source = _absolute(data['hermes_source'], 'hermes_source')
    desktop = _absolute(data['desktop_exe'], 'desktop_exe')
    service = data['gateway_service']
    # null = stock Hermes, which starts its own gateway; a name = a Windows service that wraps it.
    if service is not None and (not isinstance(service, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', service)):
        raise SettingsError('gateway_service must be a Windows service name, or null when Hermes starts its own gateway.')
    # A service-wrapped gateway is expected to serve the API health endpoint; stock Hermes serves none
    # unless the user turned the API server on, so there the gateway's own ledger is the health record.
    health = data.get('gateway_health_url', 'http://127.0.0.1:8642/health' if service is not None else None)
    try:
        parsed = urlparse(health) if health is not None else None
        if parsed is None:
            pass
        elif parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or not parsed.port or parsed.path != '/health' or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError()
    except (TypeError, ValueError):
        raise SettingsError('gateway_health_url must be a loopback HTTP /health endpoint.') from None
    codex = _accounts(data['codex'], 'codex')
    claude = _accounts(data['claude'], 'claude')
    directories = [str(row.directory.resolve()).casefold() for row in claude.values()]
    if len(directories) != len(set(directories)):
        raise SettingsError('Claude login directories must be distinct.')
    if not codex and not claude:
        raise SettingsError('Account-switch settings contain no accounts. Add Codex or Claude accounts before switching.')
    return Settings(root, source, desktop, service, health, codex, claude)


@contextmanager
def settings_snapshot():
    """Hold one verified settings object for an API request or switch worker."""
    path = settings_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise SettingsError(f'Account-switch settings are missing. Run setup.cmd from the plugin\'s folder to create them, or copy settings.example.json to {path} and fill it in.') from None
    except OSError:
        raise SettingsError('Account-switch settings could not be read.') from None
    settings = _parse_settings(raw)
    token = _PINNED.set((settings, path.resolve(), hashlib.sha256(raw).digest()))
    try:
        yield settings
    finally:
        _PINNED.reset(token)


def ensure_settings_unchanged():
    pinned = _PINNED.get()
    if pinned is None:
        raise SettingsError('Switch settings are not pinned; account writes refused.')
    _, path, digest = pinned
    try:
        if settings_path().resolve() != path or hashlib.sha256(path.read_bytes()).digest() != digest:
            raise SettingsError('Account-switch settings changed during this operation; account writes refused.')
    except OSError:
        raise SettingsError('Account-switch settings changed during this operation; account writes refused.') from None


def settings_digest():
    pinned = _PINNED.get()
    if pinned is None:
        raise SettingsError('Switch settings are not pinned.')
    return pinned[2].hex()
