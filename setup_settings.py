"""Guided settings setup. Finds this Hermes installation and the accounts already signed in, shows what it
would write, and writes the Account Switcher's settings file only after a yes.

It never signs anyone in, never changes which account is selected, never enables a plugin, and never
prints or stores a token: the only thing it reads from a login is the email address of its owner.

For a person:      python setup_settings.py
For an agent:      python setup_settings.py --show
                   python setup_settings.py --yes --name "me@example.com=Personal" --name "claude:me@example.com=Family"
"""
import argparse
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'dashboard'))
from first_run import (DESKTOP, NAME_RULE, PLUGIN, SetupError, claude_logins, existing_settings,  # noqa: E402,F401
                       gateway_service_found, plan, settings_file, usable_name, write)
import os  # noqa: E402


def default_home():
    local = os.environ.get('LOCALAPPDATA')
    return Path(local) / 'hermes' if local else None


def describe(data, target):
    lines = [f'Settings file: {target}', f"Hermes home:   {data['hermes_root']}",
             f"Hermes Desktop: {data['desktop_exe']}",
             'Gateway:       ' + (f"Windows service {data['gateway_service']}" if data['gateway_service']
                                  else "Hermes's own gateway (no Windows service)"),
             'Codex accounts:']
    lines += [f"  {key}: {row['label']} <{row['email']}>" for key, row in data['codex'].items()] or ['  none']
    if data['claude']:
        lines.append('Claude accounts:')
        lines += [f"  {key}: {row['label']} <{row['email']}>" for key, row in data['claude'].items()]
    if len(data['codex']) == 1 and not data['claude']:
        lines.append('Only one Codex account is signed in; there is nothing to switch between until a second one '
                     'is added with "hermes auth add openai-codex".')
    return lines


def names_given(pairs):
    """``EMAIL=NAME`` arguments into {(kind, email): name}. A Codex account and a Claude account may share an
    email and still have different names: ``claude:EMAIL=NAME`` names the Claude one."""
    given = {}
    for text in pairs:
        email, sep, name = text.partition('=')
        kind, colon, rest = email.partition(':')
        kind, email = (kind.strip().lower(), rest) if colon and kind.strip().lower() in ('codex', 'claude') else ('codex', email)
        if not sep or '@' not in email or usable_name(name) is None:
            raise SetupError(f'A name is given as "email=Name" inside quotes, for example "me@example.com=Personal"; '
                             f'and "claude:me@example.com=Family" for a Claude account; for the name, {NAME_RULE}. '
                             f'Got: {text[:60]}')
        given[kind, email.strip().lower()] = name
    return given


def main(argv=None, ask=input, say=print, owner=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', type=Path, default=default_home(), help='the Hermes home folder '
                        '(default: %%LOCALAPPDATA%%\\hermes)')
    parser.add_argument('--gateway-service', help='only if your Hermes gateway runs as a Windows service: its name '
                        '(found by itself while that gateway is running)')
    parser.add_argument('--claude', action='append', default=[], metavar='KEY=EMAIL',
                        help='add a Claude account that is not signed in yet, for example personal=me@example.com; '
                             'sign it in afterwards with claude_login.py')
    parser.add_argument('--name', action='append', default=[], metavar='EMAIL=NAME',
                        help='the name to show for an account, for example "me@example.com=Personal"; '
                             '"claude:me@example.com=Family" names a Claude account')
    parser.add_argument('--show', action='store_true', help='show what would be written and write nothing')
    parser.add_argument('--yes', action='store_true', help='write without asking; accounts without --name get the '
                                                            'name offered')
    args = parser.parse_args(argv)
    try:
        if args.home is None:
            raise SetupError('Pass the Hermes home folder with --home.')
        target = settings_file(args.home)
        existing = existing_settings(args.home)
        given = names_given(args.name)
        quiet = args.yes or args.show

        def typed(what, email, offered):
            # With --yes or --show nobody is there to ask. Text that was meant for the terminal and landed here
            # instead (the next command, pasted or typed ahead) is asked about again rather than kept as a name.
            if (what.lower(), email) in given:
                return given[what.lower(), email]
            if quiet:
                return offered
            for _ in range(3):
                answer = ask(f'Short name for the {what} account {email} [{offered}]: ')
                if not str(answer or '').strip():
                    return offered
                if usable_name(answer):
                    return usable_name(answer)
                say(f'"{str(answer).strip()[:40]}" cannot be a name: {NAME_RULE}, or press Enter for {offered}.')
            raise SetupError('No usable name was given. Nothing was written; run setup again.')

        known = {str(row.get('email', '')).lower() for row in ((existing or {}).get('claude') or {}).values()}
        logins = [{**row, 'name': typed('Claude', row['email'], row['name'])}
                  for row in claude_logins(args.home, owner)
                  if row['email'] not in known]
        data = plan(args.home, args.gateway_service or gateway_service_found(args.home), existing, args.claude,
                    lambda email, offered: typed('Codex', email, offered), logins)
        for line in describe(data, target):
            say(line)
        if existing == data:
            say('Nothing to change.')
            return 0
        if args.show:
            say('Nothing was written (--show).')
            return 0
        if not args.yes and ask('Write these settings? [y/N] ').strip().lower() not in ('y', 'yes'):
            say('Nothing was written.')
            return 0
        write(data, target)
        say('Settings written.')
        return 0
    except SetupError as exc:
        say(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
