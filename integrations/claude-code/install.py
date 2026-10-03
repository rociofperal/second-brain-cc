#!/usr/bin/env python3
"""Installs the Claude Code integration: skills and agents, the hooks, and (with your yes) the
recommended settings. The cross-platform twin of install.sh. Run the core bootstrap first, then:

    macOS / Linux:   python3 integrations/claude-code/install.py     (or: bash integrations/claude-code/install.sh)
    Windows:         powershell -File integrations\\claude-code\\install.ps1

Scheduled jobs, KeePass, Google accounts and the rest are not installed here: the first run
(integrations/first-run/setup.sh, or setup.ps1) asks for each one.

On Windows the hooks are written as  "<python.exe>" -X utf8 "<vault>\\_bin\\<script>.py"  and every
Python started here runs in UTF-8 mode.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VAULT = os.path.dirname(os.path.dirname(HERE))
BIN = os.path.join(DEFAULT_VAULT, "_bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import osproc  # noqa: E402
import pycmd  # noqa: E402


def say(text=""):
    print(text, flush=True)


def settings_hint(platform=None):
    if pycmd.is_windows(platform):
        return r"python -X utf8 _bin\claude_settings.py merge"
    return "python3 _bin/claude_settings.py merge"


def main(argv=None, stdin=None, platform=None, call=subprocess.call):
    vault = os.path.abspath(os.environ.get("BRAIN_VAULT") or DEFAULT_VAULT)
    env = pycmd.child_env(platform=platform)
    env["BRAIN_VAULT"] = vault
    py = pycmd.interpreter(sys.executable, platform=platform)

    def run(script, *args, stdin_null=False):
        cmd = py + [os.path.join(vault, "_bin", script)] + list(args)
        return call(cmd, env=env, **({"stdin": subprocess.DEVNULL} if stdin_null else {}))

    say("== Claude Code integration ==")

    say("-> skills catalogue")
    run("skills_index.py", stdin_null=True)           # `|| true` in install.sh: never stops the install

    say("-> skills and agents into ~/.claude (the vault's copy is canonical; __VAULT__ becomes %s)" % vault)
    code = run("install_plugin.py", "install")
    if code != 0:
        return code

    say("-> hooks into ~/.claude/settings.json (merged with what is there, backed up, nothing else touched)")
    code = run("guardian.py", "repair", "--hooks-only")
    if code != 0:
        return code

    say("-> recommended settings: only what you do not have, and only with your yes")
    if osproc.isatty(sys.stdin if stdin is None else stdin, platform=platform):
        code = run("claude_settings.py", "merge")
    else:
        code = run("claude_settings.py", "show")
        say("   not a terminal: nothing merged. To merge later: %s" % settings_hint(platform))
    if code != 0:
        return code

    say()
    say("Done. Start a Claude Code session in %s: /recall, /save, /task, /ctx, /kp and /vault-doctor are live." % vault)
    say("KeePass, Google accounts, alert email, scheduled jobs and routines: "
        + (r"powershell -File integrations\first-run\setup.ps1" if pycmd.is_windows(platform)
           else "bash integrations/first-run/setup.sh"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
