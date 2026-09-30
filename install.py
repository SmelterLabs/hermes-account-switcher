"""Offline code-only installer. Explicit home; no enablement, auth IO or restarts."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import uuid

PLUGIN = 'codex-account-switch'
OWNERSHIP = '.install-ownership.json'
SOURCE = Path(__file__).resolve().parent


def safe_path(path):
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Symlinks and Windows reparse points are not supported.')
    return path


def allowed(rel):
    parts = PurePosixPath(rel).parts
    return (rel in ('plugin.yaml', 'claude_login.py', 'desktop/plugin.js', 'dashboard/manifest.json',
                    'setup.cmd', 'setup_settings.py')
            or (len(parts) == 2 and parts[0] == 'dashboard'
                and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*\.py', parts[1]) is not None))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_files(root):
    safe_path(root)
    result = {}
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            safe_path(Path(base) / name)
        for name in files:
            path = Path(base) / name
            result[path.relative_to(root).as_posix()] = path
    return result


def generated(rel):
    """Bytecode caches Python writes beside the plugin the first time Hermes loads it. They are not user
    files: counting them made every update after the first run refuse with "user-added files"."""
    return '__pycache__' in PurePosixPath(rel).parts and rel.endswith('.pyc')


def owned(root, exact=False):
    files = {rel: path for rel, path in tree_files(root).items() if not generated(rel)}
    if OWNERSHIP not in files:
        raise ValueError('No installer ownership receipt; existing files are preserved.')
    data = json.loads(files[OWNERSHIP].read_text(encoding='utf-8'))
    hashes = data.get('files')
    if (data.get('plugin') != PLUGIN or not isinstance(hashes, dict) or not hashes
            or any(not allowed(k) or not isinstance(v, str)
                   or not re.fullmatch('[0-9a-f]{64}', v) for k, v in hashes.items())):
        raise ValueError('Invalid ownership receipt; refusing mutation.')
    if exact and (set(files) != set(hashes) | {OWNERSHIP}
                  or any(digest(files[k]) != v for k, v in hashes.items())):
        raise ValueError('Modified or user-added files prevent replacement; files preserved.')
    return hashes, files


def workspace(home):
    """Where staging, swap and rollback copies live: same volume as plugins/, never scanned by Hermes."""
    path = safe_path(Path(home) / 'plugin-data' / PLUGIN / 'installer')
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def operation(home):
    home = safe_path(home)
    if not home.is_dir():
        raise ValueError('The explicit Hermes home must already exist.')
    logs = safe_path(home / 'logs')
    lock = safe_path(logs / f'{PLUGIN}.lock')
    logs.mkdir(exist_ok=True)
    payload = json.dumps({'operation_id': 'installer-' + uuid.uuid4().hex, 'pid': os.getpid()})
    try:
        with lock.open('x', encoding='utf-8') as stream:
            stream.write(payload)
    except FileExistsError:
        raise ValueError('A switch or installer lock exists; nothing changed. If no switch is running, open '
                         'Hermes Desktop once so the plugin can clear an abandoned lock, then run this again.') from None
    try:
        target = safe_path(home / 'plugins' / PLUGIN)
        # Hermes loads EVERY folder under plugins/ (hidden names included) and mirrors each into
        # desktop-plugins/. A rollback copy kept there loads first and the real plugin is rejected as a
        # duplicate id. Everything except the live plugin stays in the plugin's own data folder.
        backup = safe_path(workspace(home) / 'rollback')
        yield target, backup
    finally:
        if lock.exists() and lock.read_text(encoding='utf-8') == payload:
            lock.unlink()


def _copy_file(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    if digest(source) != digest(dest):
        raise OSError('Installed file hash mismatch.')


def source_files(source):
    source = safe_path(source)
    required = ['plugin.yaml', 'dashboard/manifest.json', 'desktop/plugin.js']
    names = required + [p.relative_to(source).as_posix() for p in (source / 'dashboard').glob('*.py')]
    # Setup travels with the code, so it is in the same place however the plugin was installed.
    names += [name for name in ('claude_login.py', 'setup.cmd', 'setup_settings.py') if (source / name).exists()]
    if not any(name.endswith('.py') for name in names):
        raise ValueError('Package contains no backend modules.')
    for name in names:
        path = safe_path(source / name)
        if not allowed(name) or not path.is_file():
            raise ValueError('Required package file missing or unsupported: ' + name)
    return [(name, source / name) for name in sorted(names)]


def install_plugin(source, home):
    files = source_files(source)
    with operation(home) as (target, backup):
        if target.exists():
            owned(target, exact=True)
        if backup.exists():
            owned(backup, exact=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='stage-', dir=backup.parent))
        previous_backup = backup.parent / ('previous-' + uuid.uuid4().hex)
        moved_target = False
        moved_backup = False
        promoted = False
        try:
            hashes = {}
            for rel, path in files:
                _copy_file(path, stage / rel)
                hashes[rel] = digest(stage / rel)
            (stage / OWNERSHIP).write_text(json.dumps({'plugin': PLUGIN, 'files': hashes}, sort_keys=True), encoding='utf-8')
            owned(stage, exact=True)
            if backup.exists():
                backup.rename(previous_backup)
                moved_backup = True
            if target.exists():
                target.rename(backup)
                moved_target = True
            stage.rename(target)
            promoted = True
        except Exception:
            if moved_target and not target.exists():
                backup.rename(target)
            if moved_backup and not backup.exists():
                previous_backup.rename(backup)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)  # Only this invocation's private staging directory.
        if promoted and previous_backup.exists():
            owned(previous_backup, exact=True)
            shutil.rmtree(previous_backup)
        return {'action': 'install', 'path': str(target), 'files': len(hashes),
                'account_changed': False, 'profiles_enabled': [], 'processes_restarted': False}


def rollback_plugin(home):
    with operation(home) as (target, backup):
        owned(target, exact=True)
        owned(backup, exact=True)
        temporary = backup.parent / ('swap-' + uuid.uuid4().hex)
        target.rename(temporary)
        try:
            backup.rename(target)
        except Exception:
            temporary.rename(target)
            raise
        try:
            temporary.rename(backup)
        except Exception:
            target.rename(backup)
            temporary.rename(target)
            raise
        return {'action': 'rollback', 'path': str(target), 'account_changed': False}


def uninstall_plugin(home):
    with operation(home) as (target, backup):
        hashes, files = owned(target)
        removed = []
        for rel, expected in hashes.items():
            path = files.get(rel)
            if path and digest(path) == expected:
                path.unlink()
                removed.append(rel)
        (target / OWNERSHIP).unlink()
        for rel, path in tree_files(target).items():
            if generated(rel):
                path.unlink()
        for base, directories, names in os.walk(target, topdown=False):
            path = Path(base)
            if not any(path.iterdir()):
                path.rmdir()
        return {'action': 'uninstall', 'removed': removed,
                'preserved': sorted(set(files) - set(removed) - {OWNERSHIP}),
                'account_changed': False, 'rollback_copy_preserved': backup.exists()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'rollback', 'uninstall'))
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--source', type=Path, default=SOURCE)
    args = parser.parse_args()
    try:
        if args.action == 'install':
            result = install_plugin(args.source, args.home)
        elif args.action == 'rollback':
            result = rollback_plugin(args.home)
        else:
            result = uninstall_plugin(args.home)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
