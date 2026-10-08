# Technical guide — Hermes Account Switcher

Briefing for anyone picking this project up cold: what exists, why it is shaped this way, and the
traps already found. How to use the plugin is in [USER-GUIDE.md](USER-GUIDE.md); what has and has
not been proven is in [docs/acceptance.md](docs/acceptance.md).

## What this is

A Windows-only Hermes Desktop plugin (ID `codex-account-switch`, version 1.2.2)
that switches which Codex login and which Claude subscription account every local Hermes profile
on the PC uses, through one idle-only, verified restart. It began as a private tool tailored to
one machine and is packaged here for anyone. **The working behavior was preserved, not
rewritten.**

Three things are deliberately not part of this project: the third-party
`claude-subscription-directsdk` provider (a dependency, installed and maintained separately); any
change to Hermes itself; and any credential management beyond selection.

## Layout

| Path | Role |
|---|---|
| `dashboard/switch_core.py` | Account identity and the order of the switch transaction. No IO. |
| `dashboard/windows_ops.py` | Store discovery, identity checks, idle gates, stop, apply, start, verify, recovery |
| `dashboard/settings.py` | Validated settings loader; settings live outside the plugin and hold no credentials |
| `dashboard/compat.py` | The one place Hermes is called: public names only, nothing rebound; refuses before any change |
| `dashboard/env_set.py` | One `.env` key per Hermes home, through Hermes's own config writer |
| `dashboard/plugin_api.py` | Loopback routes behind Hermes's sign-in: `/status`, `/preflight`, `/switch`, `/setup` (what a first run found, and saving it), `/local-state` |
| `dashboard/first_run.py` | The one implementation of setup, used by `setup.cmd`, by an agent and by the dialog: finds the installation, the Codex accounts signed in, the Claude logins under `claude-auth` and a Windows service that runs the gateway; writes the settings |
| `dashboard/switch_worker.py`, `worker_launch.py` | Hidden one-shot worker that outlives Hermes Desktop |
| `desktop/plugin.js` | Status-bar button and dialog; imports only the plugin kit and React |
| `claude_login.py` | Guided Claude sign-in; restarts itself under Hermes's Python; a login as the wrong account is refused and its `.credentials.json` deleted |
| `setup.cmd` | What a user runs: install plus settings setup, with the Python Hermes brings; `claude <key> <email>` and `profile <name>` modes |
| `setup_settings.py` | The command line of `first_run.py`: asks for names and writes after a yes; `--show` writes nothing, `--yes --name "email=Name"` asks nothing |
| `install.py` | Offline installer: install, roll back, uninstall. `--home` mandatory; never changes enablement; refuses over files it did not install |
| `build_release.py` | Release ZIP from an allowlist, with a privacy scan, a hash and a manifest |
| `tests/` | Backend suites (fixture logins, temporary homes); `probe_*.py` are manual Windows probes |
| `desktop/*.test.mjs`, `sdk-stub.mjs` | Interface suite under jsdom, against a stand-in for the plugin kit |
| `test-vm/` | Scripts that drive the Windows test machine (not part of the release) |
| `docs/receipts/` | Evidence from live runs |

The release is a "unified package": backend code under
`<home>/plugins/codex-account-switch/` with the Desktop half at `desktop/plugin.js` inside it.

## The switch transaction (safety-critical; do not weaken)

1. **Check** (nothing changed): stores found; the target Codex login confirmed with OpenAI; the
   target Claude login confirmed with Anthropic.
2. **Freeze**: a drain marker per gateway home, then Hermes's own idle proof asked again of every
   Hermes Desktop backend. There is no hold on Desktop requests: work that starts between this
   answer and the window closing is the accepted gap (see the first gotcha).
3. **Stop**: a polite close of the exact Desktop window, wait for its process tree, stop the
   gateway.
4. **Apply**: `hermes auth priority` and `hermes auth reset` per Codex store; `.env` write or
   removal per home for Claude; every store read back.
5. **Start**: the gateway, its health, then Hermes Desktop through the Windows shell so it gets
   the user's environment and not the worker's.
6. **Verify**: new process ids, every reopened backend's own state, every stored order, Claude
   identity again. Only then does the receipt say `state: complete`.

Invariants:

- A lock file prevents two switches at once.
- A failure before anything restarts restores the previous selection; a failure after a restart
  never rewrites logins under running processes.
- Recovery always reopens Hermes, even when the restore itself fails.
- A switch whose worker is **proved gone** (lock older than 30 seconds, owner process gone, no
  worker process, lock and receipt naming the same switch) is cleared on the next status read, at
  any stage. The clearing removes the lock and this plugin's own drain markers, and rewrites the
  receipt to say where the switch stopped. It reorders nothing. Anything short of proof stays
  blocked.

## Configuration model

Settings live at `<hermes-root>/plugin-data/codex-account-switch/settings.json`, shared by every
profile, overridable with `HERMES_SWITCH_SETTINGS`. Keys: `hermes_root`, `hermes_source`,
`desktop_exe`, `gateway_service`, `codex: {key: {label, email}}`,
`claude: {key: {label, email, directory}}`, and optionally `gateway_health_url`. No Python path
is stored: Hermes's current Python is resolved each time. Labels are display only; **identity is
checked against the login, never inferred from a label**.

## Decisions already made

1. Keep the plugin ID `codex-account-switch`.
2. First release: Windows only, Codex and Claude selection. No macOS or Linux, no remote
   machines, no automatic rotation, no usage dashboards, no credential manager.
3. Preserve the working implementation; package, do not rewrite.
4. Anything specific to one user lives in the settings file, never in the release.
5. No self-updater.
6. Compatibility is proven by checking at run time and by live receipts, not by files surviving.
7. The release is built from an allowlist with a privacy scan; development history is never
   published.
8. The plugin extends Hermes only through public surfaces (Hermes catalog policy, upstream pull
   request 130795): no Hermes function is replaced, wrapped or rebound, and no underscore-private
   name is read. Where that costs safety, the cost is documented rather than patched around.

## Gotchas

- **"Is Desktop idle" is Hermes's own answer, and there is no admission hold.** `/local-state`
  calls `hermes_cli.web_server_idle_proof.idle_proof()`, the probe Hermes Desktop itself uses
  before retiring a backend: sessions running, starting or queued, live run or build threads,
  background delegations, scheduled jobs mid-run, and prompts waiting on a person. `None`
  (unreadable) is a blocker, never idle; a Hermes without the function refuses switching. Up to
  1.2.1 the plugin also rebound the Desktop RPC dispatcher to hold new requests back for three
  minutes; the catalog policy forbids rebinding core functions, so the hold is gone and
  `WindowsOps.freeze` asks the idle proof a second time just before the close instead. The window
  between that answer and the close is open; the user guide says so.
- **Hermes already authenticates plugin routes.** Its web server requires the session token on
  every `/api/` path outside its public list; `authorize()` only adds the loopback check.
- **The dashboard siblings are imported as submodules of `codex_account_switch_dashboard`** when
  Hermes loads `plugin_api.py` by file path, never by putting `dashboard/` on the shared web
  server's `sys.path`, where `settings` and `compat` would shadow other modules. The helper
  scripts and the test suite still import the plain names. Thanks to teknium1 for the fix.
- **Hermes's venv Python re-executes a script under another interpreter** when run with site
  processing on; the real-host import check runs it with `-I -S` and adds the venv's
  `site-packages` by hand (see Testing).
- **Never import Hermes from a live checkout with a different `HERMES_HOME`:** it republishes that
  checkout's launchers pointing into the other home. Probes run against an exported copy.
- **Two gateway lifecycles, chosen by `gateway_service`:** `null` stops and starts the gateway
  with Hermes's own commands, one call per owning home, and finds a running gateway by scanning
  processes; a name uses the Windows service. Never `gateway stop --all` on Windows (it kills
  without draining); never trust `gateway start`'s exit code; set
  `HERMES_GATEWAY_INSTALL_START_ON_LOGIN=0` so a bare start cannot install a login task; identity
  is (pid, birth time), never pid alone.
- **Gateway commands go through Hermes's launcher (`bin/hermes.exe`)**, never
  `python -m hermes_cli.main`: that form starts a gateway that exits at once.
- **Stock Hermes serves no health address.** With `gateway_service: null`, health is the
  gateway's own fresh status record naming a live process.
- **A fresh install has no gateway at all** until the user sets up messaging. The switch then
  restarts Hermes Desktop alone; a gateway that appears mid-switch aborts it before any change.
- **Stock Hermes ships no PyYAML.** Use `compat.safe_yaml_load`.
- **Never install or update Hermes from an administrator session:** its package manager writes
  tool folders only the owner can read, and an elevated installer makes the owner the
  Administrators group. An SSH session into Windows is an administrator session.
- **Nothing the plugin starts may inherit the Desktop backend's `PYTHONPATH`:** with it, importing
  any Hermes module relaunches the process under another interpreter and exits. Every child goes
  through `worker_launch.clean_env`. The worker catches `BaseException`, so a process exit becomes
  a failed receipt.
- **A backend never calls itself over HTTP:** `/preflight` answers for its own process directly.
- **A single-profile Desktop backend has no `--profile` argument**; discovery names it `default`.
- **The installer keeps everything except the live plugin out of `plugins/`:** Hermes loads every
  folder there. Rollback and staging copies live in `plugin-data/codex-account-switch/installer/`.
- **The plugin's button ships hidden.** The user turns on the switch on the plugin's card under
  Capabilities → Plugins → Installed. `hermes plugins enable` alone does not show the button.
- **A new Windows has no working `python`:** the command is a stub that opens the Microsoft Store.
  Everything a user runs goes through `setup.cmd`, which uses `tools\python-*` inside the Hermes
  home. That Python has no psutil; anything importing `windows_ops` needs Hermes's runtime
  environment, which `claude_login.py` finds by itself.
- **Hermes's official Windows installer (`Hermes-Setup.exe`) builds the same install as the
  install script.** There is no separate packaged layout to support today.
- **The Claude provider installs as `claude-subscription-directsdk-experimental`**, its catalog
  name is `claude-subscription-directsdk`, and as a model provider it needs no `enabled` entry.
  Only an explicit `disabled` entry counts against it.
- **Claude's sign-in lands as whoever the browser is signed in as.** The identity check catches
  it. A private browser window avoids it.
- **The identity check reaches Anthropic over the network and can fail for a moment;** it retries
  once.
- **`hermes -p <profile> plugins enable` only knows a plugin installed in that profile's own
  `plugins` folder.** Every profile needs its own install.
- **A profile without Codex logins of its own uses the first profile's.** The plugin skips such a
  profile's store when writing; the profile follows the first profile's order anyway.
- **Hermes renews an expired Codex login by itself within seconds.** A refusal for an expired
  login is normally gone at the next check, and a test that fakes expiry must expect the login to
  be renewed for real.
- **Hermes keeps its per-login request counter in memory only.** It cannot show which account
  paid; `test-vm/vm-billing-proof.py` reads the login off the wire instead.
- **Tombstoned profiles are not stores:** `profile_homes()` skips any profile with a
  `profiles/.deleted/<name>` marker.
- **Configured order is not the paying account:** `/status` carries `codex.effective` and
  `codex.warnings`.
- **The stage in a failure message is the stage that failed.** Recovery moves the receipt's stage
  on when it reopens Hermes; `failed_during` keeps the original.
- **The active-work blocker has no job names**, on purpose: the lookup that provided them was
  outside the plugin kit.
- **After any Hermes update:** acceptance is the real dialog showing selectors, and a real switch.
- **The interface tests run against a stand-in.** They cannot see how the real app draws a
  control; the solid-blue status button was found only on a real machine.
- **Receipts:** failed receipts are labeled "Last switch attempt" and never block a new switch.
- **Honesty rule for the docs:** a line stays "not run" until it ran, with a receipt.

## Testing

```
python -m pytest tests -q          # backend suites
npm ci                             # interface test dependencies
npm test                           # interface suite
```

Current counts: 280 backend, 34 interface. Live evidence and what is still not run:
[docs/acceptance.md](docs/acceptance.md).

The real-host import check loads `dashboard/plugin_api.py` the way Hermes's web server does
(`spec_from_file_location`, in Hermes's own Python environment with the live checkout
importable), lists the mounted routes, asserts no generic sibling name and no `dashboard/` entry
landed in that interpreter, runs `compat.require_switch_interfaces()` and reads the idle proof.
It reads only. Run it with Hermes's venv `python.exe -I -S`, passing the venv's `site-packages`
(and its `win32`, `win32\lib`) as extra path entries.

## Further reading

[README.md](README.md) · [USER-GUIDE.md](USER-GUIDE.md) ·
[docs/compatibility.md](docs/compatibility.md) · [docs/acceptance.md](docs/acceptance.md) ·
[docs/release-notes.md](docs/release-notes.md) · [docs/license-decision.md](docs/license-decision.md)
