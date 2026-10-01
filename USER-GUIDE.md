# User Guide — Hermes Account Switcher

Plain-language guide for using the Codex & Claude account switcher with Hermes Desktop on Windows.
You do not need to be a developer, but you should be comfortable running a command in a terminal.

## What this is

Hermes can use a **Codex** login and a **Claude** subscription account. If you have more than one,
say a personal and a work account, this plugin lets you pick which one Hermes uses, everywhere on
this PC, from one button in the Hermes status bar.

Changing accounts means closing and reopening Hermes Desktop and its background gateway, because
the account choice is read when they start. The plugin does this carefully: it waits until Hermes
is idle, applies the change everywhere at once, reopens everything, and then checks that the
change really took effect before it tells you it worked.

Three things it will never do:

- It never stores or copies your passwords or logins. They stay where Hermes keeps them.
- It never switches accounts while something is running.
- It never changes anything outside this PC.

## Before you start

You need:

- Windows 11 with Hermes and Hermes Desktop installed. You do not need Python; the plugin uses
  the one Hermes brings.
- For Codex: two or more Codex accounts signed in to Hermes. Sign each one in with
  `hermes auth add openai-codex`.
- For Claude: Claude Code (the `claude` command, from Anthropic's installer) and the Claude
  subscription provider plugin (`hermes plugins install claude-subscription-directsdk`).

You can use just Codex switching, just Claude switching, or both.

## Step 1 — Install and set up your accounts

Unpack the release ZIP, open a terminal in that folder, and run:

```
setup.cmd
```

Wait for it to finish before typing the next command: it asks questions and takes whatever is
typed as the answer.

It copies the plugin's code into Hermes, then finds your Hermes folders and the Codex accounts you
are signed in to. For each account it asks for a short name, such as `Personal` or `Work`: type
one, or press Enter to take the name it offers. That name is what the button and the dialog
show. It then shows everything it found, the account's **email** included, and asks before
writing anything.

The email matters: the plugin checks each login's real owner against it and refuses to switch if
they do not match. The name is only decoration.

Signed in another account later? Run the same command again. It adds the new account and keeps
the names you already have.

## Step 2 — Turn it on

```
hermes plugins enable codex-account-switch
```

Restart Hermes Desktop. Then open **Capabilities → Plugins**, choose **Installed**, and turn on
the switch on the **Account switch** card. If no account button appears at the bottom right,
click the card to open it and turn on **Desktop** as well: newer Hermes builds keep a separate
switch for a plugin's button.

## Setting up from the account button

If the plugin came from Hermes's plugin catalog, or you skipped the questions in Step 1, the
button at the bottom right reads **Account switch: set up**. Click it. The dialog lists the
Codex and Claude accounts already signed in on this PC. Type a short name for each, or leave
the one offered, and press **Save**. Nothing is switched and nobody is signed in by this.

## Letting your agent set it up

In that same dialog, **Copy instructions for my agent** copies a short brief. Paste it to your
agent. The agent shows you what it found, asks you for the names, and writes the settings. Two
things stay with you: signing in an account, which happens in your own browser, and switching,
which you do from the button.

If you use more than one Hermes profile, the plugin has to be installed and enabled in each one,
because a switch covers all of them. For a profile named `work`:

```
setup.cmd profile work
hermes -p work plugins enable codex-account-switch
```

## Step 3 — Sign in your Claude accounts (Claude switching only)

Once per Claude account, with a short name of your choosing and the account's email:

```
setup.cmd claude personal me@example.com
```

1. The tool prints a sign-in link and copies it to your clipboard. No browser opens by itself, so
   a browser that is already signed in cannot log you in as the wrong account.
2. Paste the link into a **private** browser window, sign in as that account, and approve.
3. Paste the code the page shows back into the terminal.
4. The tool asks Anthropic who signed in. If it was the wrong account, the login is thrown away.

## Step 4 — Switch accounts

Click the account button in the Hermes status bar. It shows the account in use, for example
**Codex: Personal**.

The dialog checks that Hermes is idle. Pick the account you want and press **Switch account**.
Hermes Desktop closes, the choice is written everywhere and read back, Hermes reopens, and the
plugin verifies the result. Expect 40 to 70 seconds. Do not force-close anything meanwhile.

**What "success" means:** the dialog reports the result from a receipt written at
`<Hermes home>\logs\codex-account-switch-last.json`. Only a receipt that says
`"state": "complete"` means the switch took effect.

## When it will refuse

The plugin blocks the switch, telling you why, when:

- a conversation or a scheduled job is running,
- another switch is already running,
- the plugin is not enabled in one of your profiles,
- the profiles on this PC disagree about which account is selected,
- a login belongs to an account that is not in your settings, or has expired,
- the provider does not confirm the account you picked,
- your settings file is missing or invalid.

These refusals are the safety system working. Fix the cause and try again.

One known limitation: when work is active, the reason is **general**. It tells you a conversation
or job is running, but not which one.

## The warning sign on the button

If the button shows **⚠** and a different account than you selected, the login you selected has
stopped working and Hermes has quietly moved on to your other account, which is now the one
paying. Hover over the button or open the dialog to read which login failed, then sign that
account in again with `hermes auth add openai-codex`.

## Changing your mind

- **Back out a plugin update:** `python install.py rollback --home <path>`.
- **Remove the plugin:** `python install.py uninstall --home <path>`. It removes only files it
  installed; your settings and logins stay.

  These two need a Python. Hermes brings one: `<Hermes home>\tools\python-...\python.exe`.
- **Stop routing Claude through a folder:** remove
  `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` from each profile's `.env`, then restart Hermes.

None of these change which account is selected. Only a switch does that.

## Where things live

| What | Where |
|---|---|
| Your settings | `<Hermes home>\plugin-data\codex-account-switch\settings.json` |
| Switch receipts | `<Hermes home>\logs\codex-account-switch-last.json` |
| Claude login folders | wherever each account's `directory` points |
| The plugin's code | `<Hermes home>\plugins\codex-account-switch` (and the same inside each profile) |

## Troubleshooting

| What you see | What it means and what to do |
|---|---|
| No account button in the status bar | A switch is off. Under Capabilities → Plugins → Installed, turn on the switch on the plugin's card, then click the card to open it and turn on **Desktop** as well: newer Hermes builds keep a separate switch for a plugin's button. Restart Hermes Desktop after enabling the plugin. |
| An account has the wrong name | The name is the `label` in the settings file (see "Where things live"). Change it there and restart Hermes Desktop. |
| The button reads "Codex: Unknown · Claude: Unknown" | The plugin is turned off in Hermes but its button is still on. Turn it back on with `hermes plugins enable codex-account-switch` and restart Hermes Desktop, or turn off the switch on its card under Capabilities → Plugins → Installed. |
| The button reads "Account switch: this PC only" | The chat you have open runs on another machine (a remote or SSH connection). The button only works on this PC. Open a chat on **This device** and press **Recheck**. |
| "The account-switch backend is not available (404)" | On an older Hermes Desktop this is how a chat on another machine shows up: open a chat on **This device** and press **Recheck**. If it stays, the plugin is not enabled for this profile or Hermes Desktop needs one restart. |
| "Account-switch settings are missing" | Run `setup.cmd`. |
| "The Hermes gateway on this PC runs as the Windows service …" | The settings were written while that gateway was stopped, so setup could not see it. Run the command the message gives (`setup.cmd service <name>`), then restart Hermes Desktop. |
| "not logged in" next to a Claude account | That account has not been signed in. Run `setup.cmd claude <its key>`. |
| "The … Claude folder is logged in as …, not …" | The sign-in was made as a different account, usually because the browser was already signed in as someone else. Run `setup.cmd claude <its key>` again and use a private browser window. |
| "The Claude subscription provider plugin is not installed" | Run `hermes plugins install claude-subscription-directsdk`. |
| "… is not enabled in this profile" | The message names the profile and the command. Install the plugin into that profile's folder first; Hermes only enables a plugin it finds there. |
| "Hermes holds a Codex login for …, which is not an account in the account-switch settings" | You signed in an account the plugin has not been told about. Run `setup.cmd` again. |
| "The Codex login for … has expired" | Hermes normally renews it by itself within seconds. Press **Recheck**. If it stays, sign that account in again. |
| "Could not confirm the selected Codex account with OpenAI" | OpenAI did not accept that login, or could not be reached. Nothing was changed. Try again; if it stays, sign that account in again. |
| "The last account switch was interrupted …" | A switch was cut off while Hermes was closed. The plugin has cleared what it left behind. Check which account the button shows, and start the gateway again if the message asks you to (`hermes gateway start`). |
| "A switch or installer lock exists" when installing | A switch was cut off. Open Hermes Desktop once so the plugin can clear it, then run the installer again. |
| "Hermes profiles disagree on which Codex account comes first" | A switch was cut off while the choice was being written. Set the order you want in each profile that holds its own logins with `hermes -p <profile> auth priority openai-codex <login label> 0` (`hermes -p <profile> auth list` shows the labels), then reopen Hermes Desktop. |
| Receipt says failed | The message says what failed and during which step. Your previous selection was restored unless the message says it could not be written back. |
| After a Hermes update the dialog says switching is disabled | Hermes changed something the plugin depends on. The plugin refuses rather than guess. Check [docs/compatibility.md](docs/compatibility.md) for verified builds. |

## Safety and privacy summary

- Everything stays on this machine, on the loopback interface, behind Hermes's own sign-in.
- No login or password appears in the dialog, the receipts or the logs.
- No telemetry of any kind is sent.
- The plugin has no self-updater: its code changes only when you install or update it yourself.
