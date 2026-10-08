# Release notes — Hermes Account Switcher

## 1.2.2

Answers the Hermes plugin catalog review of 2026-10-01 (NousResearch/hermes-agent pull request
129693, reviewer teknium1). Nothing about how a switch is applied changed.

- **The plugin touches Hermes only through public surfaces.** The Hermes catalog lists no plugin
  that replaces, wraps or rebinds Hermes code. The plugin used to swap in its own dispatcher for
  Hermes Desktop's requests so that, once a switch was cleared, new requests were held back for up
  to three minutes while Desktop closed; it also read private names of the Desktop server. All of
  that is gone, with the `/freeze` and `/release` routes and the `guard_installed` field. The
  refusal stays: "is this Desktop backend idle" is now Hermes's own answer
  (`hermes_cli.web_server_idle_proof.idle_proof`, the probe Hermes Desktop uses before it retires
  a backend), asked once for the dialog and once more right before the window closes. It covers
  conversations running, starting or waiting for input, background work a conversation owns, and
  scheduled jobs mid-run; an answer Hermes cannot give is a refusal. **What is lost:** the hold. A
  conversation started in the seconds between the last idle answer and the window closing is
  closed with the window. The user guide says so.
- **Dashboard modules no longer land on the shared web server's `sys.path`.** Hermes loads the
  backend by file path into its long-lived web-server process; the siblings are now imported as
  submodules of a package named after this plugin instead of as top-level `settings`, `compat`,
  … modules that could shadow Hermes or another plugin. The helper scripts and the test suite are
  unchanged. Contributed by teknium1 (SmelterLabs/hermes-account-switcher pull request 1).
- **The one automatic token action is documented.** Before using a Claude login whose token has
  expired, the plugin runs the `claude` CLI once against a closed local port so the CLI renews its
  own token; that renewal goes to Anthropic and rotates the refresh token in that account's
  `claude-auth` folder. The README said "no automatic rotation of any kind"; it, the catalog
  description and this file now say what happens. The renewal itself is kept: without it every
  switch to a Claude login that had gone unused long enough to expire would be refused.
- **A Claude sign-in made as the wrong account is deleted, as the documents said.** The code kept
  the mismatched `.credentials.json` in the folder and only refused to use it. It is now removed
  from that folder (nothing else in the folder is touched) and the message says to sign in again
  in a private browser window. A login that merely could not be verified, for example during a
  network blip, stays.
- **The plugin no longer reads which login a running conversation holds** (`live_pools` in
  `/local-state`): that read went through private Desktop server state, and after a restart there
  are no running conversations to read. The saved stores and each reopened backend's own selection
  are still verified.
- Hermes authenticates every plugin route itself; the plugin's own check is now only "the caller
  is on this PC".

## 1.2.1

- **The button works out which machine it is talking to.** Hermes Desktop sends a plugin's
  requests to the machine the chat you have open runs on. With a chat on a remote or SSH
  connection active, the button asked that machine, which answered "not available (404)",
  and the dialog blamed the plugin's enablement. A machine with the plugin installed would
  have been switched instead of this PC. The button now sends nothing while a remote chat is
  active: it reads "Account switch: this PC only" and the dialog says to open a chat on This
  device. Desktop builds that do not report the active connection behave as before, and their
  404 message now names this cause first.
- **Recheck reads the account status again** as well as re-running the safety check, so the
  dialog recovers at once after you move to a local chat.

## 1.2.0

The first public release: 1.2.0-rc.4, which was proven live on real machines with real
accounts, with the version number, the wording of the documents and the one fix below. The
catalog submission drafts are no longer part of the release archive.

Changes found by installing the plugin the way a user would on a PC whose gateway runs as
a Windows service:

- **A gateway run by a Windows service is found by setup and never skipped by a switch.** Such a
  gateway hides its command line from an ordinary program, so settings written by `setup.cmd`
  said "no service", the plugin then saw no gateway at all, and a switch would have restarted
  Hermes Desktop alone and left the gateway on the previous account. `setup.cmd` now finds the
  service from the gateway's own status record and the process tree above it, and the plugin
  refuses to switch while a gateway is running that its settings do not account for, naming
  the service and the command. `setup.cmd service <name>` sets the service by hand.
- **Setup asks for a short name for each Codex account.** It used to take the name Hermes
  keeps for the login, which for a login added without a name is "device_code": the button
  read "Codex: Device_Code". Enter takes the name offered, which for such a login is now the
  first part of the email address. Only a short plain name is taken: a command typed ahead
  while setup was waiting used to become the account's name.
- **The dialog sets the plugin up.** Before there are settings the button reads "Account
  switch: set up"; the dialog lists the Codex and Claude accounts already signed in, takes a
  name for each and saves. It used to show an error that told the reader to find a folder and
  run a command, which is the first thing a catalog install would have shown.
- **An agent can do the setup.** `setup.cmd show` prints what setup found and changes nothing;
  `setup.cmd auto "email=Name" ...` writes without asking. The dialog copies a brief for an
  agent that uses them and leaves signing in and switching to the person.
- Setup finds the Claude accounts already signed in under `claude-auth`, and asks Anthropic
  whose each login is. `setup.cmd` and `setup_settings.py` are installed with the plugin's
  code, so they are in the same place however the plugin was installed.
- **A profile that stood idle no longer stops everything.** A Codex access token lapses after
  some days and Hermes renews it the next time its profile runs. The plugin read a lapsed one
  in any profile as a reason to refuse, so after two idle days in one profile it showed only
  an error. A lapsed login is now read like any other, and a switch to it goes ahead without the
  availability check, which a lapsed token cannot make; the receipt says so, and the button warns
  afterwards if Hermes could not renew it.
- **No console windows flash during a switch.** The worker that performs a switch ran with no
  console, so every console program started under it, including the git calls Hermes's own code
  makes to learn its version, opened a new console, and Windows 11 showed each one in Windows
  Terminal: six or seven flashes per switch on the author's PC, traced process by process. The
  worker and its helper commands now run in consoles created hidden, which Windows does not show.
- **A read-only request never blocks a switch.** Hermes Desktop re-reads the project tree, session
  lists and usage meters all the time; with a large project folder on OneDrive one of those was in
  flight at every try, and every switch was refused. Only a request that can start work blocks now,
  and the refusal names it and how long it has been running.
- A Codex login that has reached its usage limit is said to come back by itself; the warning
  used to say to sign in again, which would have changed nothing.
- A failed request is shown as the backend's sentence, without the error class, status code
  and braces Hermes Desktop wraps around it.
- The instructions name both switches a newer Hermes gives a plugin: the one on its card, and
  "Desktop" inside the opened card, which shows the button.

## 1.2.0-rc.4 (release candidate — NOT RELEASED)

Found by running refusals, failures and a Hermes update on a real stock Windows install.

- **A switch cut off while Hermes was closed no longer locks the plugin for good.** After a crash
  or a power cut the plugin said "An account switch is already in progress" forever, and the
  installer refused as well. Reopening Hermes Desktop now clears a switch whose worker is proved
  gone, at any stage, removes the plugin's own gateway drain markers, and the dialog says where
  the switch stopped and whether the gateway needs starting. Nothing is reordered by the
  recovery.
- **Claude switching was refused on every real install.** The check for the Claude provider
  plugin looked for the wrong folder name and demanded an `enabled` entry the provider does not
  need. Found while preparing the first live Claude run; Claude switching is now proven live.
- **New: `setup.cmd`.** One command installs the plugin and sets up the accounts, using the
  Python Hermes brings: a new Windows has no working `python`. It finds the Hermes folders and
  the Codex accounts already signed in and writes the settings file after a yes.
  `setup.cmd claude <key> <email>` adds a Claude account and signs it in;
  `setup.cmd profile <name>` installs into one more profile.
- The Claude identity check retries once, so a network blip does not read as a bad login.
- `claude` is also found where Anthropic's installer puts it, which is not on PATH.
- Hermes installed with its official Windows installer is supported: it builds the same install
  as the install script.
- Failure messages name the step that failed, give the plain reason, and say when the previous
  account order could not be written back.
- Identity refusals name the login: "The Codex login for … has expired" and "Hermes holds a Codex
  login for …, which is not an account in the account-switch settings".
- The "not enabled" refusal names the profile and gives the command.
- The first profile is called "default" in messages, not by its folder's name.
- The dialog shows times in the reader's own language and time zone.
- The status-bar button is quiet text instead of a solid button, and no longer mentions a
  provider that has no accounts set up.
- The example settings file is valid for an ordinary install (it paired "no Windows service" with
  a health address such an install does not serve).
- The installer's "lock exists" message says what to do.

Proven on the test machines with this build: which account pays follows the switch, for Codex
(measured on the wire) and for Claude; a second profile follows the switch; the plugin survives a
Hermes update; a first-time install on a blank Windows works end to end. See
[acceptance.md](acceptance.md).

## 1.2.0-rc.3 (release candidate — NOT RELEASED)

- Stock Hermes support: `gateway_service: null` stops and starts the gateway with Hermes's own
  `gateway stop` / `gateway start`. A Windows service name keeps the service route. Proven on a
  stock Windows install.
- The freeze now covers long-running Desktop requests (`shell.exec`, `slash.exec`, `llm.oneshot`, ...),
  which previously could start after the switch had frozen new work. A Hermes build whose
  dispatcher has a route the freeze does not cover is refused.
- Profiles Hermes has marked deleted are skipped instead of failing the switch.
- Recovery always restarts Hermes, even when restoring the previous selection fails.
- The status badge warns when the preferred Codex login is dead or exhausted and names the
  account that is really paying.
- User-facing messages no longer name a specific Windows service.
- Desktop-only installs (no gateway ever set up) can switch; only Hermes Desktop restarts.
- Gateway commands go through Hermes's own launcher, and stock health is read from the gateway's
  own status record instead of a web address stock Hermes does not serve.
- Installer: the rollback copy moved out of the folder Hermes scans for plugins (it was loaded as
  a duplicate plugin), and updates no longer refuse after the plugin's first run.
- The switch worker no longer inherits the Desktop backend's interpreter settings, which ended
  every switch on stock Hermes before its first check; a worker that is ended by a process exit
  now records a failure instead of staying pending.
- Preflight no longer waits on its own backend, and a stock single-profile backend is discovered.

## 1.2.0-rc.2 (release candidate — NOT RELEASED)

- Fix backend loading on stock-style Hermes environments without PyYAML by using
  the host's `hermes_yaml` parser through the compatibility boundary.
- Refuse Claude switching when the external provider is absent or disabled in a
  discovered profile. Codex-only configuration remains supported without it.
- Add and package regression coverage for import safety, YAML parsing and provider
  prerequisites. Mainline gates: 151 Python tests and 21 frontend tests pass.
- A real Hermes-host import/parser probe passed with PyYAML absent. This is not a
  real account-switch test or Windows VM acceptance.

Guided first-run setup, generic Windows gateway lifecycle support, live switching,
upgrade survival, licensing and public catalog submission remain incomplete.

## 1.2.0-rc.1 (release candidate — NOT RELEASED)

Candidate packaging of the formerly internal "codex-account-switch" Desktop tool as an
independent, stranger-usable Windows plugin. Same plugin ID (`codex-account-switch`) as the
internal tool, so an existing installation migrates in place.

### Changes from the internal 1.1.x line

- **Independent packaging.** Installs from a release ZIP via
  `python install.py install --home <path>` — code only, never changing plugin enablement in
  either direction, preserving all settings, auth stores, and unrelated files. Upgrades refuse
  unknown/modified plugin files; legacy installs without an ownership receipt require explicit
  manual migration rather than an overwrite. Explicit rollback and uninstall commands with the
  same preservation guarantees. Official enabling is `hermes plugins enable
  codex-account-switch` in the profile you choose. The package ships `.gitattributes`
  (`* -text` — no newline normalization, release bytes preserved exactly; an earlier `eol=lf`
  attempt still normalized mixed Windows source bytes on commit) and the builder uses a file
  allowlist so the archive's bytes are exact and reproducible across Windows checkouts.
- **External account configuration.** Personal account tables move out of the source into
  `<hermes-root>\plugin-data\codex-account-switch\settings.json` (see `settings.example.json`),
  shared by the root home and named profiles, overridable via `HERMES_SWITCH_SETTINGS`. Labels are
  display-only; verified identity is checked against the configured expected email.
- **No bundled credentials or personal data.** Example data uses reserved `example.invalid`
  addresses and generic paths. No runtime Python path is configured; the active Hermes-managed
  interpreter is resolved dynamically.
- **Generalized account keys.** Arbitrary lowercase account keys per provider instead of the
  previous fixed personal set; Codex-only or Claude-only configurations are supported.
- **Compatibility groundwork.** A runtime compatibility gate (in development at this candidate)
  is intended to refuse switching on unsupported Hermes builds before any mutation.
  The audited private-interface map and verified source baselines are in
  [compatibility.md](compatibility.md).
- **Unchanged safety contract.** User-selected account changes only; preflight, idle gating,
  concurrency lock, authenticated loopback backend, hidden one-shot worker, bounded recovery, and
  post-restart verification with receipts. Restart scope is still exactly Hermes Desktop plus the
  configured gateway service.

### Known limitations at this candidate (rc.1; see acceptance.md for the current state)

- When work is active, the switch blocker reports it generically and does not list individual
  running job names (the private lookup that provided them was removed to stay inside the
  Desktop SDK surface).
- Live account switching and Hermes upgrade survival have **not been run** against this candidate
  (they require separately approved test identities). See [acceptance.md](acceptance.md).
- Not catalog-listed; no public repository exists yet. Catalog materials under
  [../catalog/](../catalog/) are drafts and are not submittable as-is.
- No license granted; all rights reserved by the author pending the decision recorded in
  [license-decision.md](license-decision.md).
- Windows only. On unmodified stock Hermes builds, the idle preflight may read a stale busy count
  and refuse switches (fail-safe); see compatibility.md for details.

### Test and acceptance status

On the release archive: 140 Python tests passing (114 core + 26 packaging), 21 frontend tests,
official validation `ok: true` by both routes — the upstream `validate_plugin_dir` function
against the isolated extracted tree, and the official CLI
(`python -m hermes_cli.main plugins validate --json`, isolated `HERMES_HOME` + stock
`PYTHONPATH`, exit 0) on the exact native fixture from the final archive; security scan safe,
desktop surface pass, one documented skipped probe (no `__init__.py` — the Python capability
probe is skipped, not passed). A real-archive install/reinstall/rollback/uninstall cycle passed
in an isolated home with synthetic auth/settings preserved. The native official installer route
(`cmd_install`, `enable=False`, `no_deps=True`) is byte-exact — frontend and backend hashes
match the original archive, preserved by `.gitattributes` `* -text` plus a builder allowlist
(an earlier `eol=lf` attempt normalized mixed Windows source bytes and was rejected). Core is
at `d0129c1` with the dev-only Vitest dependency patched to 4.1.11; `npm audit` reports zero.
Receipts dated 2026-09-26. **Real account switching, UI enablement, and Hermes upgrade survival
have NOT been run**; no license granted; not published.

## 1.1.2 (internal, unreleased)

- Accepts multiple grants for the same verified Codex account ID (for example after CLI login
  recovery), selecting the lowest-priority row without deleting any grant; conflicting selections
  still fail closed.

## 1.1.1 (internal, unreleased)

- Launcher compatibility: backend discovery accepts both `python -m hermes_cli.main` and the
  canonical managed launcher, including isolated mode. Preflight exceptions return safe HTTP 409
  details; failed receipts labelled "Last switch attempt" do not block new switches.

## 1.1.0 and earlier (internal, unreleased)

- Internal tool evolution: per-profile writes, warm-session verification, shared-backend support,
  recovery rules, one-shot worker with Job Object breakaway. Historical detail lives in the
  private development history, which is not part of any public release.
