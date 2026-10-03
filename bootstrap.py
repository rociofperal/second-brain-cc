#!/usr/bin/env python3
"""Core bootstrap for the Second Brain: agent-agnostic, macOS, Linux and Windows.

Does exactly what bootstrap.sh does (that script stays, for people who run it): sets up the vault
engine (index and health), then offers the first run.

  macOS / Linux:   python3 bootstrap.py          (or: bash bootstrap.sh)
  Windows:         powershell -File bootstrap.ps1   (it finds Python and starts this in UTF-8 mode)

It installs nothing into any agent and schedules nothing. Afterwards:
  - the first run, one yes at a time:  integrations/first-run/setup.sh  (Windows: setup.ps1)
  - MCP server (any MCP agent):        integrations/mcp/README.md
  - command line (any shell agent):    integrations/cli/README.md
  - Claude Code (deepest):             integrations/claude-code/install.sh  (Windows: install.ps1)

On Windows every Python it starts runs in UTF-8 mode (`-X utf8`): the vault is UTF-8 notes, and
Python would otherwise read and write them in the ANSI code page.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "_bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import pycmd  # noqa: E402

WIN = sys.platform == "win32"


def say(text=""):
    print(text, flush=True)


def check_python():
    """Python 3.9+ with SQLite FTS5, the only hard requirement. Returns an error text or None."""
    import sqlite3

    if sys.version_info < (3, 9):
        return "Python 3.9+ required, found %s" % sys.version.split()[0]
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(b)")
    except sqlite3.Error as exc:
        return "this Python's SQLite has no FTS5 (%s)" % exc
    say("   OK: Python %s with SQLite %s and FTS5" % (sys.version.split()[0], sqlite3.sqlite_version))
    return None


def has_obsidian(environ=None, which=shutil.which, exists=os.path.exists, platform=None):
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if which("obsidian") or exists("/Applications/Obsidian.app"):
        return True
    if platform == "win32":
        roots = [environ.get("LOCALAPPDATA"), environ.get("ProgramFiles"), environ.get("ProgramFiles(x86)")]
        return any(exists(os.path.join(r, "Programs" if i == 0 else "", "Obsidian", "Obsidian.exe"))
                   for i, r in enumerate(roots) if r)
    return False


def optional_tools(which=shutil.which, platform=None, environ=None, exists=os.path.exists):
    """The lines the optional-tools step prints (same words as bootstrap.sh on macOS and Linux)."""
    platform = sys.platform if platform is None else platform
    out = []
    if not which("git"):
        out.append("   git not found: the vault cannot sync between machines without it" +
                   ("\n     Windows: winget install Git.Git" if platform == "win32" else ""))
    if which("keepassxc-cli"):
        out.append("   keepassxc-cli found: credentials can live in a local KeePass database (the first run asks)")
    else:
        out.append("   keepassxc-cli not found: install KeePassXC only if you want credentials in a KeePass database")
        if platform == "win32":
            out.append("     Windows: winget install KeePassXCTeam.KeePassXC")
        else:
            out.append("     macOS: brew install --cask keepassxc    Linux: your distribution's keepassxc package")
    if not has_obsidian(environ, which, exists, platform):
        out.append("   tip: Obsidian (https://obsidian.md) gives the vault a GUI; optional" +
                   ("\n     Windows: winget install Obsidian.Obsidian" if platform == "win32" else ""))
    return out


def shell_for_ps1(which=shutil.which):
    return which("pwsh") or which("powershell") or "powershell"


def first_run(vault, env):
    """The first run: setup.sh through bash, or setup.ps1 through PowerShell on Windows. Both exit 0 at
    once, saying how to start it, when there is no terminal."""
    if WIN:
        cmd = [shell_for_ps1(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               os.path.join(vault, "integrations", "first-run", "setup.ps1")]
    else:
        cmd = ["bash", os.path.join(vault, "integrations", "first-run", "setup.sh")]
    try:
        return subprocess.call(cmd, env=env)
    except OSError as exc:
        say("   could not start the first run (%s: %s); start it yourself, see below" % (type(exc).__name__, exc))
        return 0


def closing(platform=None):
    win = (sys.platform if platform is None else platform) == "win32"
    if win:
        setup, install, tests = (r"powershell -File integrations\first-run\setup.ps1",
                                 r"powershell -File integrations\claude-code\install.ps1",
                                 r"python -X utf8 _bin\run_all_tests.py")
        status = r"python -X utf8 integrations\first-run\first_run.py status"
    else:
        setup, install, tests = ("bash integrations/first-run/setup.sh", "bash integrations/claude-code/install.sh",
                                 "python3 _bin/run_all_tests.py")
        status = "python3 integrations/first-run/first_run.py status"
    return """
== Core ready ==

  Connect your agent:
  1) MCP server  (Claude Desktop, Cline, Cursor, Zed, OpenCode, ...)  integrations/mcp/README.md
  2) CLI         (any agent that can run a shell, or you)             integrations/cli/README.md
  3) Claude Code (automatic recall, agents, skills)                   %s

  The first run can be started, resumed or checked any time:
     %s
     %s

  Every test, each in a scratch HOME:  %s""" % (install, setup, status, tests)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")      # a console that is not UTF-8 must not stop the report
        except (AttributeError, ValueError):
            pass
    vault = os.path.abspath(os.environ.get("BRAIN_VAULT") or HERE)
    env = pycmd.child_env()
    env["BRAIN_VAULT"] = vault
    py = pycmd.interpreter(sys.executable)
    say("== Second Brain: core bootstrap ==")
    say("   vault: %s" % vault)

    say("-> checking python3 and SQLite/FTS5 (the only hard requirement)")
    problem = check_python()
    if problem:
        sys.stderr.write("AssertionError: %s\n" % problem)
        return 1

    say("-> optional tools")
    for line in optional_tools():
        say(line)

    say("-> building the initial search index")
    code = subprocess.call(py + [os.path.join(vault, "_bin", "index_vault.py"), "--full"], env=env)
    if code != 0:
        return code

    say("-> health check")
    try:
        p = subprocess.run(py + [os.path.join(vault, "_bin", "doctor.py")], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
        head = p.stdout.decode("utf-8", "replace").splitlines()[:6]
        if head:
            say("\n".join(head))
    except (OSError, subprocess.SubprocessError):
        pass

    say("-> first run")
    sys.stdout.flush()
    code = first_run(vault, env)
    if code != 0:
        return code

    say(closing())
    return 0


if __name__ == "__main__":
    sys.exit(main())
