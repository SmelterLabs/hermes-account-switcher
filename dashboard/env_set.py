"""Set or remove ONE key in the ``.env`` of ``HERMES_HOME`` through Hermes's own writer.

Run as ``python env_set.py KEY VALUE`` or ``python env_set.py KEY --remove`` with ``HERMES_HOME`` pointing at the
store. Using ``hermes_cli.config`` keeps quoting, BOM handling, atomic replace and file mode identical to what
``hermes config`` would do; nothing is printed.
"""
import sys

from compat import set_env

key, value = sys.argv[1], sys.argv[2]
set_env(key, value)
