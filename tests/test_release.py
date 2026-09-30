"""Release export: deterministic bytes, explicit contents, fail closed on leaks."""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile
import pytest

ROOT = Path(__file__).resolve().parents[1]


def builder():
    path = ROOT / 'build_release.py'
    assert path.is_file(), 'Release builder has not been implemented'
    spec = importlib.util.spec_from_file_location('release_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source(tmp_path, module):
    root = tmp_path / 'source'
    for rel in module.FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# benign fixture\n', encoding='utf-8')
    (root / 'plugin.yaml').write_text('name: codex-account-switch\nversion: "1.2.0-rc.1"\n', encoding='utf-8')
    (root / 'dashboard/manifest.json').write_text(json.dumps({'name': 'codex-account-switch', 'version': '1.2.0-rc.1'}), encoding='utf-8')
    return root


def test_reproducible_export_contains_only_allowlisted_files(tmp_path):
    m = builder()
    root = source(tmp_path, m)
    (root / 'private-secret.txt').write_text('not for export')
    a = m.build(root, tmp_path / 'a')
    b = m.build(root, tmp_path / 'b')
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as z:
        assert set(z.namelist()) == set(m.FILES)
        hashes = json.loads(a.with_suffix('.manifest.json').read_text())['files']
        assert hashes == {name: hashlib.sha256(z.read(name)).hexdigest() for name in z.namelist()}
    assert a.with_suffix('.sha256').read_text().split()[0] == hashlib.sha256(a.read_bytes()).hexdigest()


@pytest.mark.parametrize('kind', ['email', 'user-path', 'token', 'external-url'])
def test_private_content_refused_without_artifact(tmp_path, kind):
    m = builder()
    root = source(tmp_path, m)
    samples = {
        'email': '{}@{}.com'.format('fixture', 'private-company'),
        'user-path': 'C:/' + '/'.join(['Users', 'actual-owner', 'private']),
        'token': 'sk-' + 'x' * 40,
        'external-url': 'https://' + 'unexpected-host.invalid/path',
    }
    (root / 'README.md').write_text(samples[kind], encoding='utf-8')
    with pytest.raises(ValueError, match='(?i)privacy|secret|external|private'):
        m.build(root, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_missing_required_file_refused(tmp_path):
    m = builder()
    root = source(tmp_path, m)
    (root / 'dashboard/settings.py').unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        m.build(root, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_mismatched_metadata_refused(tmp_path):
    m = builder()
    root = source(tmp_path, m)
    (root / 'dashboard/manifest.json').write_text('{}')
    with pytest.raises(ValueError, match='metadata'):
        m.build(root, tmp_path / 'output')


def test_release_refuses_linked_source(tmp_path):
    m = builder()
    root = source(tmp_path, m)
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside package')
    target = root / 'README.md'
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip('Symlink creation unavailable on this host')
    with pytest.raises(ValueError, match='(?i)link|reparse'):
        m.build(root, tmp_path / 'output')


def test_reviewed_provider_and_local_template_urls_allowed():
    m = builder()
    for text in ('https://api.openai.com/profile', 'https://chatgpt.com/backend-api/wham/usage',
                 'https://api.anthropic.com/api/oauth/profile', "http://127.0.0.1:{backend['port']}/api/status"):
        m.scan('fixture.py', text.encode())
