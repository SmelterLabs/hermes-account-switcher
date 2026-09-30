"""Export only reviewed candidate files; never publish this repository's history."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile
from urllib.parse import urlsplit
from install import safe_path

FILES = tuple(sorted('''
.gitattributes LICENSE plugin.yaml install.py build_release.py claude_login.py setup_settings.py setup.cmd
complete_approved_switch.py reload_backend.py verify_switch_result.py
README.md USER-GUIDE.md TECHNICAL-GUIDE.md settings.example.json
package.json package-lock.json requirements-test.in requirements-test.lock
dashboard/manifest.json dashboard/compat.py dashboard/settings.py
dashboard/desktop_gate.py dashboard/env_set.py dashboard/first_run.py dashboard/plugin_api.py
dashboard/switch_core.py dashboard/switch_worker.py dashboard/windows_ops.py dashboard/worker_launch.py
desktop/plugin.js desktop/plugin.test.mjs desktop/fixtures.mjs desktop/sdk-stub.mjs
desktop/test-setup.mjs desktop/vitest.config.mjs
tests/conftest.py tests/test_api.py tests/test_approved_switch.py
tests/test_backend_discovery.py tests/test_claude.py tests/test_compat.py tests/test_gate.py
tests/test_per_profile.py tests/test_process_liveness.py tests/test_recovery.py
tests/test_reload.py tests/test_settings.py tests/test_switch.py tests/test_worker.py
tests/test_install.py tests/test_release.py tests/test_stock_runtime.py tests/test_stock_gateway.py
tests/test_setup.py tests/test_first_run.py
docs/acceptance.md docs/compatibility.md docs/license-decision.md docs/release-notes.md
docs/receipts/2026-09-28-vm-real-switch.json docs/receipts/2026-09-28-vm-stock-gateway-lifecycle.json
docs/receipts/2026-09-28-vm-billing-proof.json docs/receipts/2026-09-28-vm-refusals-and-failures.json
docs/receipts/2026-09-28-vm-hermes-update.json docs/receipts/2026-09-29-vm-first-install.json
docs/receipts/2026-09-29-vm-claude.json
'''.split()))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def scan(rel, data):
    text = data.decode('utf-8')
    for domain in re.findall(r'[\w.%+-]+@([\w.-]+\.[a-zA-Z]{2,})', text):
        if not (domain.endswith(('.invalid', '.test', '.example'))
                or domain in ('example.com', 'example.org', 'example.net')):
            raise ValueError('Privacy scan: non-example email in ' + rel)
    for user in re.findall(r'[a-zA-Z]:[\\/]+Users[\\/]+([^\\/\s"\']+)', text, re.I):
        # "tester" is the test machine's throwaway account; it appears in the live-run receipts.
        if user.lower() not in ('<user>', '<you>', 'example-user', 'public', 'default', 'tester'):
            raise ValueError('Privacy scan: private user path in ' + rel)
    if re.search(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}', text):
        raise ValueError('Secret-shaped content in ' + rel)
    approved = {'github.com', 'raw.githubusercontent.com', 'registry.npmjs.org',
                'pypi.org', 'files.pythonhosted.org', 'docs.python.org',
                'hermes-agent.nousresearch.com', 'opencollective.com',
                'tidelift.com', 'www.npmjs.com', '127.0.0.1', 'localhost',
                'chatgpt.com', 'api.openai.com', 'api.anthropic.com'}
    for url in re.findall(r'https?://[A-Za-z0-9][^\s"\'<>`]*', text):
        host = urlsplit(url.split('{', 1)[0]).hostname
        if host not in approved and host not in ('example.com', 'example.org', 'example.invalid'):
            raise ValueError('Unreviewed external URL in ' + rel)


def build(source, output):
    source, output = safe_path(source), safe_path(output)
    contents = {rel: safe_path(source / rel).read_bytes() for rel in FILES}
    for rel, data in contents.items():
        scan(rel, data)
    match = re.search(r'^version:\s*[\"\']?([\w.+-]+)', contents['plugin.yaml'].decode(), re.M)
    if not match:
        raise ValueError('Missing package version.')
    version = match.group(1)
    manifest = json.loads(contents['dashboard/manifest.json'])
    if manifest.get('name') != 'codex-account-switch' or manifest.get('version') != version:
        raise ValueError('Plugin and dashboard metadata must agree.')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'codex-account-switch-{version}.zip'
    for target in (archive, archive.with_suffix('.manifest.json'), archive.with_suffix('.sha256')):
        safe_path(target)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
        for rel, data in contents.items():
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zipped.writestr(info, data)
    archive.with_suffix('.manifest.json').write_text(json.dumps({
        'plugin': 'codex-account-switch', 'version': version,
        'files': {rel: sha(data) for rel, data in contents.items()},
    }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    archive.with_suffix('.sha256').write_text(sha(archive.read_bytes()) + '  ' + archive.name + '\n', encoding='utf-8')
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(build(args.source, args.output))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
