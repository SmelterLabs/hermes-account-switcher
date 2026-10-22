"""Every place that states the plugin's version agrees with plugin.yaml.

The front page said 1.2.3 for three releases (1.3.0, 1.3.2, 1.3.3) because nobody updated it. This runs
in CI on every pull request and in the Release workflow on the exact archive, so a mismatch blocks both."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def text(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def version():
    match = re.search(r'^version:\s*["\']?([\w.+-]+)', text('plugin.yaml'), re.M)
    assert match, 'plugin.yaml has no version line'
    return match.group(1)


def test_metadata_files_match_plugin_yaml():
    v = version()
    assert json.loads(text('dashboard/manifest.json'))['version'] == v, 'dashboard/manifest.json'
    assert json.loads(text('package.json'))['version'] == v, 'package.json'
    lock = json.loads(text('package-lock.json'))
    assert lock['version'] == v, 'package-lock.json "version"'
    assert lock['packages']['']['version'] == v, 'package-lock.json packages[""].version'


def test_documents_name_the_current_version():
    v = version()
    assert re.search(r'version ' + re.escape(v) + r'\)', text('TECHNICAL-GUIDE.md')), \
        'TECHNICAL-GUIDE.md: "(ID `account-switcher`, version X)"'
    readme = text('README.md')
    assert re.search(r'^- Version: \*\*' + re.escape(v) + r'\*\*', readme, re.M), 'README.md "- Version:" line'
    assert '## Status\n\nVersion **' + v + '**' in readme.replace('\r\n', '\n'), \
        'README.md: the Status section must open with "Version **X**"'
    assert re.search(r'^Release: \*\*' + re.escape(v) + r'\*\*', text('docs/acceptance.md'), re.M), \
        'docs/acceptance.md "Release:" line'


def test_release_notes_top_heading_is_this_version_or_unreleased():
    v = version()
    headings = re.findall(r'^## (.+?)\s*$', text('docs/release-notes.md'), re.M)
    if headings and headings[0] == 'Unreleased':
        headings = headings[1:]
    assert headings and headings[0] == v, \
        f'docs/release-notes.md: first version heading is {headings[:1]}, plugin.yaml says {v}'
