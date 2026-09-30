"""Installer contract tests. Fixture homes under tmp_path only; never production."""
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INSTALL = ROOT / "install.py"
PLUGIN = "codex-account-switch"

sys.path.insert(0, str(ROOT))


def make_source(path, version="1.2.0-rc.1", extra_py=("plugin_api.py", "switch_core.py")):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "plugin.yaml").write_text(
        f'name: {PLUGIN}\nversion: "{version}"\ndescription: fixture release\n', encoding="utf-8")
    (path / "dashboard").mkdir(parents=True, exist_ok=True)
    (path / "dashboard" / "manifest.json").write_text(
        json.dumps({"name": PLUGIN, "version": version, "api": "plugin_api.py"}), encoding="utf-8")
    for name in extra_py:
        (path / "dashboard" / name).write_text(f"# fixture {name} {version}\n", encoding="utf-8")
    (path / "desktop").mkdir(parents=True, exist_ok=True)
    (path / "desktop" / "plugin.js").write_text(f"// fixture {version}\n", encoding="utf-8")
    return path


def run_cli(*args):
    return subprocess.run([sys.executable, str(INSTALL), *args],
                          capture_output=True, text=True, timeout=60)


def target_of(home):
    return Path(home) / "plugins" / PLUGIN


def test_installer_is_stdlib_only():
    tree = ast.parse(INSTALL.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported - sys.stdlib_module_names == set()


def test_install_requires_home(tmp_path):
    src = make_source(tmp_path / "src")
    proc = run_cli("install", "--source", str(src))
    assert proc.returncode != 0


def test_rollback_and_uninstall_require_home(tmp_path):
    assert run_cli("rollback").returncode != 0
    assert run_cli("uninstall").returncode != 0


def test_install_copies_release_files_and_stays_disabled(tmp_path):
    src = make_source(tmp_path / "src")
    home = tmp_path / "home"
    home.mkdir()
    proc = run_cli("install", "--home", str(home), "--source", str(src))
    assert proc.returncode == 0, proc.stderr
    target = target_of(home)
    for rel in ("plugin.yaml", "dashboard/manifest.json", "dashboard/plugin_api.py",
                "dashboard/switch_core.py", "desktop/plugin.js"):
        assert (target / rel).read_bytes() == (src / rel).read_bytes()
    receipt = json.loads(proc.stdout)
    assert receipt["action"] == "install"
    # Stays disabled: no profile enablement, no account dirs, no credential writes.
    assert not (home / "desktop-plugins").exists()
    assert not (home / "claude-auth").exists()
    assert not (home / "auth.json").exists()
    assert not (home / ".env").exists()
    # The installer keeps its own working folder there; it never writes settings.
    assert [p.name for p in (home / "plugin-data" / PLUGIN).iterdir()] == ["installer"]
    assert list((home / "plugins").iterdir()) == [target]


def test_install_refuses_missing_home(tmp_path):
    src = make_source(tmp_path / "src")
    proc = run_cli("install", "--home", str(tmp_path / "nope"), "--source", str(src))
    assert proc.returncode != 0


def test_install_refuses_switch_lock(tmp_path):
    src = make_source(tmp_path / "src")
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "codex-account-switch.lock").write_text("{}", encoding="utf-8")
    proc = run_cli("install", "--home", str(home), "--source", str(src))
    assert proc.returncode != 0
    assert not (home / "plugins").exists()


def test_install_refuses_symlink_home(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")
    src = make_source(tmp_path / "src")
    proc = run_cli("install", "--home", str(link), "--source", str(src))
    assert proc.returncode != 0


def test_install_refuses_unknown_nonempty_target(tmp_path):
    src = make_source(tmp_path / "src")
    home = tmp_path / "home"
    home.mkdir()
    target = target_of(home)
    target.mkdir(parents=True)
    (target / "hand-placed.txt").write_text("mine", encoding="utf-8")
    proc = run_cli("install", "--home", str(home), "--source", str(src))
    assert proc.returncode != 0
    assert (target / "hand-placed.txt").read_text(encoding="utf-8") == "mine"
    assert not (target / "plugin.yaml").exists()


def test_install_failure_mid_install_restores_prior(tmp_path, monkeypatch):
    import install as installer
    src = make_source(tmp_path / "src", version="2.0.0")
    home = tmp_path / "home"
    home.mkdir()
    old = make_source(tmp_path / "old", version="1.0.0")
    assert run_cli("install", "--home", str(home), "--source", str(old)).returncode == 0
    before = {p.relative_to(target_of(home)): p.read_bytes()
              for p in sorted((target_of(home)).rglob("*")) if p.is_file()}

    calls = []
    real_copy = installer._copy_file

    def flaky(src_file, dst_file):
        calls.append(str(dst_file))
        if len(calls) == 2:
            raise OSError("fixture disk failure")
        return real_copy(src_file, dst_file)

    monkeypatch.setattr(installer, "_copy_file", flaky)
    with pytest.raises(OSError):
        installer.install_plugin(src, home)
    after = {p.relative_to(target_of(home)): p.read_bytes()
             for p in sorted((target_of(home)).rglob("*")) if p.is_file()}
    assert after == before


def test_upgrade_then_rollback_restores_prior(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    v1 = make_source(tmp_path / "v1", version="1.0.0")
    v2 = make_source(tmp_path / "v2", version="2.0.0")
    assert run_cli("install", "--home", str(home), "--source", str(v1)).returncode == 0
    assert run_cli("install", "--home", str(home), "--source", str(v2)).returncode == 0
    assert (target_of(home) / "dashboard" / "plugin_api.py").read_text() == "# fixture plugin_api.py 2.0.0\n"
    proc = run_cli("rollback", "--home", str(home))
    assert proc.returncode == 0, proc.stderr
    assert (target_of(home) / "dashboard" / "plugin_api.py").read_text() == "# fixture plugin_api.py 1.0.0\n"
    assert (target_of(home) / "plugin.yaml").read_text().find('version: "1.0.0"') >= 0


def test_nothing_but_the_live_plugin_is_left_where_hermes_scans(tmp_path):
    # Seen on a real install: Hermes loads every folder under plugins/, hidden names included, so a rollback
    # copy kept there loaded FIRST and the updated plugin was rejected as a duplicate id.
    home = tmp_path / "home"
    home.mkdir()
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        src = make_source(tmp_path / version, version=version)
        assert run_cli("install", "--home", str(home), "--source", str(src)).returncode == 0
        assert [p.name for p in (home / "plugins").iterdir()] == [PLUGIN]
    assert run_cli("rollback", "--home", str(home)).returncode == 0
    assert [p.name for p in (home / "plugins").iterdir()] == [PLUGIN]
    kept = home / "plugin-data" / PLUGIN / "installer"
    assert [p.name for p in kept.iterdir()] == ["rollback"]


def test_update_and_uninstall_work_after_the_plugin_has_run(tmp_path):
    # Seen on a real install: Hermes imported the plugin, Python wrote __pycache__ beside it, and the next
    # update refused with "Modified or user-added files prevent replacement".
    home = tmp_path / "home"
    home.mkdir()
    v1 = make_source(tmp_path / "v1", version="1.0.0")
    v2 = make_source(tmp_path / "v2", version="2.0.0")
    assert run_cli("install", "--home", str(home), "--source", str(v1)).returncode == 0
    cache = target_of(home) / "dashboard" / "__pycache__"
    cache.mkdir()
    (cache / "plugin_api.cpython-314.pyc").write_bytes(b"compiled")
    proc = run_cli("install", "--home", str(home), "--source", str(v2))
    assert proc.returncode == 0, proc.stderr
    assert (target_of(home) / "dashboard" / "plugin_api.py").read_text() == "# fixture plugin_api.py 2.0.0\n"
    assert not (target_of(home) / "dashboard" / "__pycache__").exists()
    # A real user file is still protected.
    (target_of(home) / "dashboard" / "notes.txt").write_text("mine", encoding="utf-8")
    assert run_cli("install", "--home", str(home), "--source", str(v1)).returncode != 0
    (target_of(home) / "dashboard" / "notes.txt").unlink()
    cache.mkdir()
    (cache / "plugin_api.cpython-314.pyc").write_bytes(b"compiled")
    assert run_cli("uninstall", "--home", str(home)).returncode == 0
    assert not target_of(home).exists()


def test_rollback_requires_backup(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert run_cli("rollback", "--home", str(home)).returncode != 0


def test_rollback_refuses_without_ownership(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    target = target_of(home)
    target.mkdir(parents=True)
    (target / "foreign.txt").write_text("not ours", encoding="utf-8")
    assert run_cli("rollback", "--home", str(home)).returncode != 0
    assert (target / "foreign.txt").read_text(encoding="utf-8") == "not ours"


def test_uninstall_removes_owned_preserves_modified_and_user_added(tmp_path):
    src = make_source(tmp_path / "src")
    home = tmp_path / "home"
    home.mkdir()
    assert run_cli("install", "--home", str(home), "--source", str(src)).returncode == 0
    target = target_of(home)
    (target / "dashboard" / "plugin_api.py").write_text("# operator edit\n", encoding="utf-8")
    (target / "notes.txt").write_text("user file", encoding="utf-8")
    proc = run_cli("uninstall", "--home", str(home))
    assert proc.returncode == 0, proc.stderr
    assert not (target / "dashboard" / "switch_core.py").exists()
    assert not (target / "desktop" / "plugin.js").exists()
    assert not (target / "plugin.yaml").exists()
    assert (target / "dashboard" / "plugin_api.py").read_text(encoding="utf-8") == "# operator edit\n"
    assert (target / "notes.txt").read_text(encoding="utf-8") == "user file"
    assert home.exists()


def test_uninstall_refuses_without_ownership_and_never_deletes_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "keep.txt").write_text("keep", encoding="utf-8")
    assert run_cli("uninstall", "--home", str(home)).returncode != 0
    assert (home / "keep.txt").read_text(encoding="utf-8") == "keep"
    target = target_of(home)
    target.mkdir(parents=True)
    (target / "foreign.txt").write_text("x", encoding="utf-8")
    assert run_cli("uninstall", "--home", str(home)).returncode != 0
    assert (target / "foreign.txt").exists()
    assert home.exists()


def test_uninstall_and_rollback_refuse_switch_lock(tmp_path):
    src = make_source(tmp_path / "src")
    home = tmp_path / "home"
    home.mkdir()
    assert run_cli("install", "--home", str(home), "--source", str(src)).returncode == 0
    (home / "logs").mkdir(exist_ok=True)
    (home / "logs" / "codex-account-switch.lock").write_text("{}", encoding="utf-8")
    assert run_cli("uninstall", "--home", str(home)).returncode != 0
    assert run_cli("rollback", "--home", str(home)).returncode != 0
    assert (target_of(home) / "plugin.yaml").exists()


def test_cycle_never_touches_settings_or_account_stores(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    settings = home / "plugin-data" / PLUGIN / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"codex_labels": {}}', encoding="utf-8")
    auth = home / "auth.json"
    auth.write_text('{"credential_pool": {}}', encoding="utf-8")
    env = home / ".env"
    env.write_text("X=1\n", encoding="utf-8")
    claude = home / "claude-auth" / "gmail" / ".credentials.json"
    claude.parent.mkdir(parents=True)
    claude.write_text('{"tok": 1}', encoding="utf-8")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (settings, auth, env, claude)}
    v1 = make_source(tmp_path / "v1", version="1.0.0")
    v2 = make_source(tmp_path / "v2", version="2.0.0")
    assert run_cli("install", "--home", str(home), "--source", str(v1)).returncode == 0
    assert run_cli("install", "--home", str(home), "--source", str(v2)).returncode == 0
    assert run_cli("rollback", "--home", str(home)).returncode == 0
    assert run_cli("uninstall", "--home", str(home)).returncode == 0
    for p, digest in before.items():
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest


def test_hermes_switch_settings_override_is_ignored(tmp_path, monkeypatch):
    outside = tmp_path / "outside-settings.json"
    monkeypatch.setenv("HERMES_SWITCH_SETTINGS", str(outside))
    src = make_source(tmp_path / "src")
    home = tmp_path / "home"
    home.mkdir()
    assert run_cli("install", "--home", str(home), "--source", str(src)).returncode == 0
    assert not outside.exists()
