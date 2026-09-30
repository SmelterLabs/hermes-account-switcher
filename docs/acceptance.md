# Acceptance ledger — Hermes Account Switcher

What "accepted" means for this plugin, split into five kinds of evidence. Passing one never
substitutes for another. Nothing below is marked passed unless it ran, and every live result
names its receipt.

Release: **1.2.0**. Last updated 2026-09-29.

1.2.0 is the last release candidate, rc.4, with the version number and the wording of the
documents changed and two fixes: a gateway run by a Windows service is found by setup and is
never skipped by a switch, setup asks for a short name for each account, and the dialog does
the setup itself the first time the button is clicked. Every live result below ran on rc.4 unless it says otherwise.
The gates and the live runs on the 1.2.0 archive itself are recorded with the release,
outside the archive: an archive cannot contain its own hash.

## A. Automated tests

Run from this repository; no Hermes needed.

| Item | Status |
|---|---|
| Backend suites (fixture logins, temporary homes, stand-in commands) | **288 passing** |
| Interface suite (stand-in for Hermes's plugin kit, not the real app) | **31 passing** |

These prove the logic. They do not prove a switch on a real machine; section D does.

## B. Official validation of the release archive

| Item | Status |
|---|---|
| `hermes plugins validate --json` on the extracted archive | **Passed on the rc.4 archive**, run by a stock Hermes (upstream `7154128f`) on the test machine: exit 0, `ok: true`, security scan safe, Desktop surface inside the plugin kit. One documented skipped probe: the plugin has no `__init__.py`, so Hermes skips its Python capability probe. |
| Hermes's own installer route (`hermes plugins install`) from the exact archive, byte for byte | Passed on the rc.1 archive. **Not repeated on the rc.4 archive**; it needs the public repository. |

## C. Installer lifecycle

| Item | Status |
|---|---|
| Install, reinstall, roll back, uninstall in a scratch home with stand-in data | Passed on the rc.1 archive; covered by 19 automated tests on the current code. |
| Install and update on a real stock Hermes, from the rc.4 archive | **Passed**, first profile and a second profile; each profile's own files (`config.yaml`, `.env`, `SOUL.md`, the login store) were byte for byte unchanged. |
| Roll back, and roll back again, on a real stock Hermes | **Passed**; the second roll back returned the exact bytes of the update. |
| Uninstall on a real stock Hermes | **Passed**: the 13 installed files removed, a file the user had added kept, the rollback copy kept. |
| Install over a folder the installer does not own | **Refused**, files kept. |
| The installer never enables or disables anything | **Passed**: the plugin stayed enabled through update, roll back and uninstall. |
| The guided settings setup on a real stock Hermes | **Passed**: as a first run it produced the same folders, account keys and emails as settings written by hand. |

## D. Real switching

Test machine: a clean Windows 11 Pro (25H2, build 26200) virtual machine; Hermes installed by a
normal signed-in user; real accounts signed in there by their owner. Nothing was copied from
another machine. Most rows ran on Hermes installed with the install script; the first-time install
and every Claude row ran on a second, blank machine where Hermes was installed with the official
Windows installer.

| Item | Status | Receipt |
|---|---|---|
| Real Codex switches through the plugin's backend, both directions | **Passed** | `receipts/2026-09-28-vm-real-switch.json` |
| Real Codex switch entirely through the Desktop button and dialog | **Passed**, and again on the rc.4 archive after the Hermes update (40.1 s, billing confirmed on the wire) | same |
| Hermes's own gateway stopped and started by the switch (no Windows service) | **Passed** | `receipts/2026-09-28-vm-stock-gateway-lifecycle.json` |
| Which account pays for a request, before and after a switch, measured on the wire | **Passed** | `receipts/2026-09-28-vm-billing-proof.json` |
| Refusal: a login for an account that is not in the settings | **Passed**, nothing changed | `receipts/2026-09-28-vm-refusals-and-failures.json` |
| Refusal: the provider rejects the target login | **Passed**, nothing changed | same |
| Refusal: a conversation is running (dialog, and a forced request) | **Passed**, nothing changed, the conversation kept running | same |
| An expired login | **Refused while expired.** Hermes renewed the login by itself within seconds and the switch was then allowed. Not a standing refusal. | same |
| The selected login is dead: warning, and the account that really pays | **Passed** | same |
| Failure while Hermes is closed: settings changed | **Passed**: previous order restored, Hermes reopened | same |
| Failure while Hermes is closed: login store cannot be written | **Passed**: Hermes reopened, receipt says the restore could not be written | same |
| The switch is cut off while Hermes is closed | **Failed on rc.3** (a permanent "already in progress"). **Passed on rc.4.** | same |
| A second profile without logins of its own follows the switch | **Passed** | same |
| A profile that holds its own Codex logins | **Not run** on stock Hermes (automated tests only). | |
| A switch cut off while the choice is being written, with more than one store | **Not run.** | |
| Real Claude switches through the plugin's backend: none selected to one account, then to the other | **Passed** (62.7 s and 67.6 s) | `receipts/2026-09-29-vm-claude.json` |
| Which Claude account a real request uses, before and after a switch | **Passed**: every `claude` process the request started was handed the selected account's login folder, and Anthropic confirmed that folder's owner | same |
| Codex and Claude changed together in one switch, entirely through the Desktop dialog | **Passed** (46.3 s); both proofs repeated afterwards | same |
| A Claude sign-in made as the wrong account | **Refused**: the browser was signed in as another account, and the plugin would not accept the login for the account it was meant for | same |
| Claude sign-in by pasting the code into `setup.cmd claude` | **Not run.** The sign-ins for the test were answered through the browser instead. The same code path is what the internal tool has used. | |
| A first-time install on a blank Windows 11, with no Python on it: Hermes's official installer, `setup.cmd`, `hermes plugins enable`, the switch on the plugin's card, then a switch through the dialog | **Passed** (first switch 54.6 s, billing confirmed on the wire) | `receipts/2026-09-29-vm-first-install.json` |
| Hermes installed with the official Windows installer (`Hermes-Setup.exe`) | **Passed.** It builds the same install as the install script. | same |
| Hermes as a Windows Store style package, which Hermes's source can build but its site does not offer | **Not run.** | |
| The author's own customized Hermes build with a Windows service gateway | **Not run for this release.** The internal tool this plugin was packaged from runs there daily. | |

## E. Hermes update

| Item | Status | Receipt |
|---|---|---|
| Update Hermes from one build to a newer one with Hermes's own command; plugin, settings and logins survive; preflight, a real switch and the billing proof pass on the new build | **Passed** (upstream `9a0a1625` to `7154128f`, 629 commits) | `receipts/2026-09-28-vm-hermes-update.json` |
| An update made with Hermes Desktop's in-app updater | **Not run.** | |
| A Hermes build that changes an interface the plugin depends on is refused before anything is changed | Automated tests only. | |

## What is still open

1. Hermes's own installer route (`hermes plugins install`) from the public repository. Its
   result is recorded with the release.
2. The listing in the official catalog.

## Release claim rule

The release is complete only when every line that applies to the declared scope is passed with a
receipt. Receipts for the final archive are produced outside it: an archive cannot contain its
own hash.
