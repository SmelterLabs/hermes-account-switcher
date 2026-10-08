# Compatibility record — Hermes Account Switcher

Which Hermes builds this plugin has actually run against, what it depends on, and how it fails
when a build is not supported. Last refreshed 2026-10-07.

## Verified builds

| Hermes build | Installed how | What ran | Result |
|---|---|---|---|
| v0.21.5+3840, upstream `9a0a1625` | official install script, Windows 11 Pro 25H2 | real switches, billing proof, refusals, failures mid-switch, second profile | passed |
| v0.21.5+4469, upstream `7154128f` | updated from the build above with `hermes update` | preflight, a real switch, billing proof in two profiles | passed |
| v0.21.5+4531, upstream `3cf2eb1c` | official Windows installer (`Hermes-Setup.exe`) on a blank Windows 11 | first-time install, Codex switches, Claude switches, both together, billing proofs | passed |

Receipts are in [receipts/](receipts/). No other build is claimed.

## Not supported

- **macOS and Linux.**
- **Hermes as a Windows Store style package.** Hermes's source can build one, but its site offers
  `Hermes-Setup.exe`, which builds the same install as the install script and is supported. The
  plugin finds Hermes's Python, its command and Hermes Desktop through that install's folders;
  where they are missing, the setup command says so and changes nothing.

## Hermes interfaces this plugin uses

Everything below is a public (non-underscore) Hermes name or command, called as is. Nothing is
replaced, wrapped or rebound, and no private name is read: that is what the Hermes plugin catalog
policy requires. Most of these names are still not part of the documented plugin kit, so Hermes is
free to change them; the plugin checks each one before it acts and refuses when one is gone.

| Interface | Used for |
|---|---|
| `pm.environments.committed_venv` | finding Hermes's current Python |
| `gateway.drain_control` (`write_drain_request`, `read_drain_request`, `drain_requested`, `clear_drain_request`) | holding new gateway work back, reversibly (the gateway's own external drain-control contract) |
| `hermes_cli.web_server_idle_proof.idle_proof` | whether a Desktop backend is idle: the probe Hermes Desktop itself uses before retiring a backend (sessions, delegations, scheduled jobs, prompts waiting on a person) |
| Hermes's web-server authentication on every plugin route | the plugin's routes answer only to Hermes's own signed-in Desktop; the plugin adds a loopback check |
| `hermes_cli.auth.read_credential_pool` | reading which logins exist |
| `hermes_cli.config.save_env_value` / `remove_env_value` | writing one `.env` key per profile (Claude) |
| Commands: `hermes auth priority`, `hermes auth reset`, `hermes gateway stop`, `hermes gateway start` | changing the login order; stopping and starting the gateway |
| The Claude subscription provider plugin (catalog name `claude-subscription-directsdk`, installed as `claude-subscription-directsdk-experimental`) and `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` | the Claude path (a separate plugin); verified with its version 0.3.0 and Claude Code 2.1.284 |

## How an unsupported build fails

Before anything is stopped or written, the plugin checks that every interface above exists with
the parameters it expects. If any check fails, switching is refused with "Hermes safety interfaces
are unavailable or changed; account switching is disabled." and nothing is changed. A Desktop
backend whose idle proof cannot be read, or answers "unknown", is a blocker, never idle. The plugin
never patches Hermes.

Up to 1.2.1 the plugin also rebound Hermes Desktop's request dispatcher to hold new requests back
during a switch. That is gone in 1.2.2 (catalog policy), so the seconds between the last idle
answer and the Desktop window closing are not guarded.

## Behavior of stock Hermes worth knowing

- **It renews an expired Codex login by itself**, within seconds of something reading the login
  store. A refusal for an expired login is normally gone on the next check.
- **A profile without Codex logins of its own uses the first profile's.**
- **`hermes -p <profile> plugins enable` only knows a plugin installed in that profile's own
  `plugins` folder.**
- **Newer Hermes builds give a plugin two switches.** The one on the card turns on the plugin in
  the profile; a second one, "Desktop", inside the opened card, turns on its button. Seen on
  upstream `5f103c1c`; on `3cf2eb1c` the card had one switch for both.
- **A new plugin's button stays hidden until the user turns on the switch on its card** under
  Capabilities → Plugins → Installed.
- **A new Windows has no working `python`**: the command opens the Microsoft Store. Hermes brings
  its own under `tools\python-*`, which is what `setup.cmd` uses.
- **Anthropic's installer puts `claude` in `~\.local\bin` and does not add it to PATH.** The plugin
  looks there too.
- **Hermes can show "Could not load agent plugins: plugins.manage timed out" on the Plugins
  screen when it is opened within seconds of Hermes Desktop starting.** It is not this plugin:
  in seven starts in a row on the test machine the message appeared with the plugin turned on,
  with it turned off and with its Desktop folder moved away, and then did not appear with the
  plugin removed, put back, or turned on again. Reopening the screen clears it. What in Hermes
  is slow on those first starts was not established.
- **`hermes plugins disable` turns off the plugin's backend, not its button.** The button then
  reads "Codex: Unknown · Claude: Unknown". Turn off the switch on the plugin's card as well.
- **A gateway run by a Windows service hides its command line** from an ordinary program. The
  plugin finds such a gateway through the gateway's own status record and its parent processes.
- **No health address is served** by the gateway unless the user turned the API server on. The
  plugin reads the gateway's own status record instead.
- **Older builds refreshed the gateway's count of active work only while a drain was requested**,
  so the idle check before a switch could read an old busy count and refuse. That is the safe
  direction; it was not seen on the verified builds.
- **Hermes logs "Failed to load plugin 'codex-account-switch': No `__init__.py`" at every start.**
  The plugin has no Python entry point for Hermes's general loader; its backend is loaded as a
  dashboard plugin. The message is harmless.

## If something breaks after a Hermes update

1. Open the plugin's dialog and read what it says. A changed interface shows as a refusal, not as
   a half-applied switch.
2. Do not delete receipts or change accounts to clear a message.
3. `python install.py rollback --home <path>` restores the previous plugin code, and `uninstall`
   removes it without touching logins.
