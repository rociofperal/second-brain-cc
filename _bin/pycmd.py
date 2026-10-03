#!/usr/bin/env python3
"""How Brain names its Python interpreter in every entry point it writes (stdlib only).

POSIX keeps what it always had: the interpreter named by the caller, the script path as is.

Windows reads and writes files in the ANSI code page unless Python runs in UTF-8 mode, and the
vault is UTF-8 notes, so every command line Brain generates there starts Python as

    "<python.exe>" -X utf8 "<script>" <args>

with backslash paths and the interpreter and the script in double quotes. Claude Code on Windows
runs hook commands through a shell (Git Bash, or cmd), and a double-quoted backslash path is
read the same way by both: the characters that bash treats as escapes inside double quotes
(`\\`, `$`, a backtick, `"`) do not occur in a path that names a script. Every function takes
`platform` and `executable` so tests on Linux can render the win32 output.
"""
import ntpath
import os
import sys

POSIX_PYTHON = "/usr/bin/python3"
UTF8_FLAGS = ("-X", "utf8")


def is_windows(platform=None):
    return (sys.platform if platform is None else platform) == "win32"


def win_path(path):
    """A path with backslashes, as Windows tools print it (drive letters and UNC prefixes kept)."""
    return ntpath.normpath(path) if path else path


def quote(text):
    """Double quotes around one word of a command line. A path never holds a double quote."""
    return '"%s"' % text


def windows_python(executable=None, environ=None, exists=os.path.isfile):
    """The one python.exe every Windows command line Brain writes into a config names: hooks, skills,
    permission rules, MCP entries.

    `executable` when given (tests, callers that chose one); else BRAIN_PYTHON when set; else this
    Python. pythonw.exe (what the scheduled jobs run under, so no console flashes) becomes the
    python.exe next to it when there is one: otherwise a job that rewrites a config from Task
    Scheduler would name pythonw.exe and the next one run from a terminal python.exe, flipping the
    file back and forth. A hook needs its stdout, which pythonw.exe has none of anyway."""
    if not executable:
        env = os.environ if environ is None else environ
        chosen = (env.get("BRAIN_PYTHON") or "").strip()
        if chosen:
            return chosen
        executable = sys.executable or "python.exe"
    folder, name = ntpath.split(executable)
    if name.lower() == "pythonw.exe":
        sibling = ntpath.join(folder, name[:-len("w.exe")] + name[-len(".exe"):])   # keeps the case
        if exists(sibling):
            return sibling
    return executable


def interpreter(python=None, platform=None, executable=None):
    """The interpreter part of an argv. Windows: [python.exe, -X, utf8]; POSIX: [python]."""
    if is_windows(platform):
        return [executable or sys.executable or "python.exe"] + list(UTF8_FLAGS)
    return [python or POSIX_PYTHON]


def script_argv(script, args=(), python=None, platform=None, executable=None):
    """The argv that runs a Python script: interpreter, script, arguments."""
    script = win_path(script) if is_windows(platform) else script
    return interpreter(python, platform, executable) + [script] + [str(a) for a in args]


def hook_command(script, args="", python=None, platform=None, executable=None):
    """The command line of a Claude Code hook that runs `script`, as one string.

    POSIX: `<python> <script> <args>`, exactly what hooks.json has always held.
    Windows: `"<python.exe>" -X utf8 "<script>" <args>`."""
    if not is_windows(platform):
        return "%s %s%s" % (python or POSIX_PYTHON, script, (" " + args) if args else "")
    py = windows_python(executable)
    line = "%s %s %s" % (quote(win_path(py)), " ".join(UTF8_FLAGS), quote(win_path(script)))
    return line + (" " + args if args else "")


def mcp_command(server, python="python3", platform=None, executable=None):
    """(command, args) of the MCP server entry every MCP client config takes.

    POSIX: (python, [server]). Windows: (python.exe, ["-X", "utf8", server])."""
    if is_windows(platform):
        return (win_path(windows_python(executable)),
                list(UTF8_FLAGS) + [win_path(server)])
    return python, [server]


def shell_join(argv, platform=None):
    """One command line for a user to paste: words with a space in them are double-quoted on
    Windows; POSIX uses shlex.quote."""
    if is_windows(platform):
        return " ".join(quote(w) if (" " in w or "\\" in w) else w for w in argv)
    import shlex
    return " ".join(shlex.quote(w) for w in argv)


def child_env(environ=None, platform=None):
    """A copy of the environment for a child Python: on Windows UTF-8 mode is also asked for by
    PYTHONUTF8, which `-X utf8` does not pass on to the processes a script starts."""
    env = dict(os.environ if environ is None else environ)
    if is_windows(platform):
        env["PYTHONUTF8"] = "1"
    return env
