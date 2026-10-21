"""Hermes Desktop runs its own copy of the dialog, made when Desktop starts.

Seen on the owner's PC on 2026-10-21: the update was installed at 18:17 while Desktop, started at 17:02, kept
running the copy it took the day before. The switch worked, but the dialog was the old one until Desktop was
restarted. The switch now refuses with a plain sentence until then; any dialog version shows a blocker."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'dashboard'))
import windows_ops as ops

NEW = b'// dialog 1.3.3\n'
OLD = b'// dialog 1.3.2\n'
INSTALLED_AT = 1792624624.5   # 2026-10-21 18:17 local, as on the owner's PC
COPIED_AT = 1792533149.6      # the day before


def layout(tmp_path, copy=OLD, recorded=COPIED_AT, source=None):
    root = tmp_path / 'hermes'
    package = root / 'plugins' / 'account-switcher' / 'desktop'
    package.mkdir(parents=True)
    (package / 'plugin.js').write_bytes(NEW)
    os.utime(package / 'plugin.js', (INSTALLED_AT, INSTALLED_AT))
    os.utime(package, (INSTALLED_AT, INSTALLED_AT))
    mirror = root / 'desktop-plugins' / 'account-switcher'
    mirror.mkdir(parents=True)
    (mirror / 'plugin.js').write_bytes(copy)
    (mirror / '.hermes-package.json').write_text(json.dumps({
        'package': 'account-switcher', 'source': str(source or package), 'sourceMtimeMs': recorded * 1000}), encoding='utf-8')
    return root, package, mirror


def test_desktop_still_running_the_copy_from_before_the_install_blocks_with_a_plain_sentence(tmp_path):
    root, package, _ = layout(tmp_path)
    assert ops.stale_desktop_blockers(root, package) == [ops.STALE_DESKTOP_TEXT]
    assert 'Close Hermes Desktop completely and open it again' in ops.STALE_DESKTOP_TEXT


def test_after_desktop_restarts_and_copies_the_new_dialog_nothing_blocks(tmp_path):
    root, package, _ = layout(tmp_path, copy=NEW, recorded=INSTALLED_AT)
    assert ops.stale_desktop_blockers(root, package) == []


def test_an_identical_copy_never_blocks_whatever_the_recorded_time(tmp_path):
    root, package, _ = layout(tmp_path, copy=NEW, recorded=COPIED_AT)
    assert ops.stale_desktop_blockers(root, package) == []


def test_a_copy_newer_than_the_install_is_not_called_stale(tmp_path):
    # e.g. a rollback: Desktop copied a later build, then older files were put back. Not proof of anything.
    root, package, _ = layout(tmp_path, recorded=INSTALLED_AT + 60)
    assert ops.stale_desktop_blockers(root, package) == []


def test_rounded_file_times_within_two_seconds_do_not_block(tmp_path):
    root, package, _ = layout(tmp_path, recorded=INSTALLED_AT - 1.5)
    assert ops.stale_desktop_blockers(root, package) == []


def test_a_copy_of_another_install_is_left_alone(tmp_path):
    # A profile install: Desktop's copy names the root install as its source, not this one.
    root, package, _ = layout(tmp_path, source=tmp_path / 'elsewhere' / 'desktop')
    assert ops.stale_desktop_blockers(root, package) == []


def test_missing_or_unreadable_desktop_records_never_block(tmp_path):
    root, package, mirror = layout(tmp_path)
    (mirror / '.hermes-package.json').write_text('{not json', encoding='utf-8')
    assert ops.stale_desktop_blockers(root, package) == []
    (mirror / '.hermes-package.json').write_text(json.dumps({'source': str(package)}), encoding='utf-8')
    assert ops.stale_desktop_blockers(root, package) == []
    (mirror / '.hermes-package.json').unlink()
    assert ops.stale_desktop_blockers(root, package) == []
    assert ops.stale_desktop_blockers(tmp_path / 'no-hermes-here', package) == []


def test_the_default_source_is_this_installs_own_dialog():
    assert ops.PACKAGE_DIR == Path(ops.__file__).resolve().parent.parent / 'desktop'
