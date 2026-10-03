#!/usr/bin/env python3
"""Process helpers that behave the same on POSIX and Windows (stdlib only).

`os.kill(pid, 0)` is the classic "is this process alive?" probe, but on Windows signal 0 is
CTRL_C_EVENT, so the same call would send Ctrl+C to a console process group instead of probing.
Everything here goes through `pid_state`.
"""
import os
import sys

IS_WINDOWS = sys.platform == "win32"


def _pid_state_windows(pid):
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # No such process -> gone; access denied -> it exists but is not ours.
        return True if ctypes.get_last_error() == ERROR_ACCESS_DENIED else False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_state(pid):
    """True if the process is alive, False if it is gone, None when it cannot be told."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            return _pid_state_windows(pid)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # it exists, it just is not ours
    except Exception:
        return None


def pid_alive(pid):
    """Strict reading: only a process known to be alive counts."""
    return pid_state(pid) is True


# ---------------------------------------------------------------- portable process control
#
# os.getuid, os.killpg, signal.SIGKILL, os.fchmod and Popen(start_new_session=True) are POSIX
# only (fchmod reached Windows in 3.13 only). These are the one place that knows the difference.

def current_uid(platform=None):
    """The numeric user id on POSIX, None on Windows (which has SIDs, not uids)."""
    platform = sys.platform if platform is None else platform
    if platform == "win32" or not hasattr(os, "getuid"):
        return None
    return os.getuid()


def is_root(platform=None):
    """True only for euid 0 on POSIX. Windows has no root user in this sense: always False."""
    platform = sys.platform if platform is None else platform
    if platform == "win32" or not hasattr(os, "geteuid"):
        return False
    return os.geteuid() == 0


# CreateProcess flags (subprocess exposes them only on Windows, so they are spelled out here).
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def new_group_kwargs(platform=None):
    """Popen keyword arguments that put the child in a process group of its own, so kill_tree can
    end everything it started: a new session on POSIX, a new process group on Windows."""
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def detached_kwargs(platform=None):
    """Popen keyword arguments for a background worker that must outlive its parent and never
    show a console window: a new session on POSIX; DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
    CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB on Windows. The last one takes the worker out of
    the job object its parent runs in (Claude Code puts its hooks in one, and closing the job kills
    everything in it); a job that forbids breakaway makes CreateProcess fail with access denied,
    which spawn_detached answers by starting the worker again without it."""
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
                | CREATE_BREAKAWAY_FROM_JOB, "close_fds": True}
    return {"start_new_session": True}


def spawn_detached(argv, platform=None, popen=None, **kw):
    """Start a fire-and-forget worker with detached_kwargs() (`kw` adds to them or overrides them)
    and return its Popen. Windows: when the parent's job object refuses CREATE_BREAKAWAY_FROM_JOB,
    the same start is retried without that flag, so the worker still runs (inside the job). A
    missing program is not retried. Raises what Popen raises, like Popen."""
    import subprocess
    platform = sys.platform if platform is None else platform
    popen = popen or subprocess.Popen
    args = dict(detached_kwargs(platform), **kw)
    try:
        return popen(argv, **args)
    except FileNotFoundError:
        raise
    except OSError:
        flags = args.get("creationflags", 0)
        if platform != "win32" or not flags & CREATE_BREAKAWAY_FROM_JOB:
            raise
        args["creationflags"] = flags & ~CREATE_BREAKAWAY_FROM_JOB
        return popen(argv, **args)


def kill_tree(proc, platform=None, run=None):
    """Kill a child started with new_group_kwargs() and everything it started.

    POSIX: SIGKILL to its process group. Windows: `taskkill /T /F /PID`, which walks the tree.
    Falls back to proc.kill() when that fails. Never raises."""
    platform = sys.platform if platform is None else platform
    try:
        if platform == "win32":
            import subprocess
            run = run or subprocess.run
            p = run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                    stdin=subprocess.DEVNULL, timeout=15)
            if getattr(p, "returncode", 1) == 0:
                return True
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
            return True
    except Exception:
        pass
    try:
        proc.kill()
        return True
    except Exception:
        return False


def isatty(stream, platform=None):
    """stream.isatty() that is also right for NUL on Windows. There the null device (what
    stdin=DEVNULL gives a child) is a character device, so isatty() says True for it and a prompt
    would then die with EOFError. On Windows a stream counts as a terminal only when its handle is
    a console (GetConsoleMode succeeds)."""
    platform = sys.platform if platform is None else platform
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    if platform != "win32":
        return True
    try:
        import ctypes
        import msvcrt
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except Exception:
        return True


def chmod_fd(fh, mode, path=None):
    """os.fchmod on POSIX; on Windows (no fchmod before 3.13) chmod by `path`, or the file's name.
    On Windows only the read-only bit means anything, and a failure there is ignored."""
    try:
        if hasattr(os, "fchmod") and sys.platform != "win32":
            os.fchmod(fh.fileno(), mode)
        else:
            os.chmod(path or fh.name, mode)
    except (OSError, AttributeError, TypeError):
        if sys.platform != "win32":
            raise


def split_command(command, platform=None):
    """shlex.split for a configured command line, that keeps Windows paths whole.

    POSIX: shlex.split, as before. Windows: the rules programs there split their own command line
    by (CommandLineToArgvW / the C runtime): spaces and tabs separate words except inside double
    quotes, which may open and close anywhere in a word (`--x="a b"` is the one word `--x=a b`),
    `""` inside quotes is a literal quote, and backslashes are literal except before a quote
    (2n backslashes and a quote: n backslashes and the quote opens or closes; 2n+1: n backslashes
    and a literal quote), so `C:\\a\\b` stays whole. One addition for templates shared with POSIX
    machines: a whole word in single quotes ('Bash(git status:*)') is that word without them.
    Raises ValueError on an unclosed quote, like shlex.split."""
    import shlex
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return shlex.split(command or "")
    return _split_windows(command or "")


_WS = " \t\r\n"


def _split_windows(command):
    out, i, n = [], 0, len(command)
    while True:
        while i < n and command[i] in _WS:
            i += 1
        if i >= n:
            return out
        if command[i] == "'":
            end = command.find("'", i + 1)
            if end == -1:
                raise ValueError("No closing quotation")
            if end + 1 == n or command[end + 1] in _WS:
                out.append(command[i + 1:end])
                i = end + 1
                continue
        word, quoted = [], False
        while i < n:
            c = command[i]
            if c == "\\":
                j = i
                while j < n and command[j] == "\\":
                    j += 1
                count = j - i
                if j < n and command[j] == '"':
                    word.append("\\" * (count // 2))
                    if count % 2:
                        word.append('"')
                        j += 1
                    i = j                              # an even run: the quote is read next
                else:
                    word.append("\\" * count)
                    i = j
                continue
            if c == '"':
                if quoted and i + 1 < n and command[i + 1] == '"':
                    word.append('"')
                    i += 2
                    continue
                quoted = not quoted
                i += 1
                continue
            if c in _WS and not quoted:
                break
            word.append(c)
            i += 1
        if quoted:
            raise ValueError("No closing quotation")
        out.append("".join(word))


def resolve_exe(path, platform=None, environ=None, isfile=os.path.isfile):
    """The file to run for a configured program path.

    POSIX: the path itself. Windows runs only what PATHEXT names (.exe, .cmd, .bat ...), so a path
    written without one, `~/.local/bin/claude` say, means `claude.exe` or `claude.cmd` next to it:
    the first PATHEXT match wins, and the path itself is the answer only when none exists."""
    platform = sys.platform if platform is None else platform
    if platform != "win32" or not path:
        return path
    environ = os.environ if environ is None else environ
    exts = [e for e in (environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    if os.path.splitext(path)[1].upper() in [e.upper() for e in exts]:
        return path
    for ext in exts:
        for candidate in (path + ext.lower(), path + ext):
            if isfile(candidate):
                return candidate
    return path


def _read_text(path):
    for enc in ("utf-8", "cp1252"):
        try:
            with open(path, encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


# `"%_prog%"  "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*` in an npm shim; the same
# line with `"%dp0%\...\claude.exe" %*` for a package that ships a native binary.
_SHIM_KNOWN = ("node_modules", "@anthropic-ai", "claude-code", "cli.js")


def unwrap_npm_shim(path, platform=None, read=None, isfile=os.path.isfile, which=None):
    r"""Argv prefix that runs an npm `.cmd` shim without cmd.exe, or None when `path` is not one.

    cmd.exe cuts a command line at its first newline, so a multi-line prompt handed to
    `claude.cmd` arrives as its first line. The shim only starts node on a script next to it, so
    the same thing run directly keeps the prompt whole: [node_exe, script]. The script is read
    from the shim text (the `%dp0%\...js` word) rather than assumed; for a claude shim whose text
    shows none, the known `node_modules\@anthropic-ai\claude-code\cli.js` is tried. node_exe is
    `node.exe` beside the shim when there is one, else `node` from PATH. A shim that launches a
    native `.exe` gives [that exe]. POSIX, a non-.cmd path and a .cmd that is no npm shim give
    None. Never raises."""
    import ntpath
    import re
    import shutil
    platform = sys.platform if platform is None else platform
    if platform != "win32" or not path:
        return None
    try:
        if ntpath.splitext(path)[1].lower() not in (".cmd", ".bat"):
            return None
        text = (read or _read_text)(path)
        if not text or "%dp0%" not in text.lower():
            return None
        folder = ntpath.dirname(path)
        target = None
        for m in re.finditer(r'%dp0%[\\/]*([^"\r\n%]+?\.(?:c|m)?js|[^"\r\n%]+?\.exe)', text, re.I):
            if ntpath.basename(m.group(1)).lower() != "node.exe":        # `_prog` names node itself
                target = m.group(1).replace("/", "\\")
        candidates = []
        if target:
            candidates.append(ntpath.normpath(ntpath.join(folder, target)))
        if ntpath.basename(path).lower() in ("claude.cmd", "claude.bat"):
            candidates.append(ntpath.join(folder, *_SHIM_KNOWN))
        script = next((c for c in candidates if isfile(c)), None)
        if script is None:
            return None
        if script.lower().endswith(".exe"):
            return [script]
        beside = ntpath.join(folder, "node.exe")
        if isfile(beside):
            node = beside
        else:
            node = (which or shutil.which)("node") or "node"
        return [node, script]
    except Exception:
        return None


# What cmd.exe acts on inside a command line, quoted or not: command separators and redirections,
# its escape character, %VAR% and !VAR! expansion, the quote that toggles its parsing (Python escapes
# a " in an argument as \", which cmd.exe does not read as an escape) and a line break, where it
# stops reading the line.
CMD_METACHARACTERS = '&|<>^%!"\r\n'


def batch_argument_problem(argv, platform=None):
    """Why `argv` must not be run as it is, or None.

    Windows runs a `.cmd` / `.bat` through cmd.exe, which reads its whole command line: `&`, `|`,
    `>` and `%VAR%` inside an argument still run as commands and expand. So an argument holding any
    of CMD_METACHARACTERS is refused for a batch file (an npm shim unwrap_npm_shim could not see
    through, say): a routine's text passed as `{prompt}` would execute what such characters say.
    Plain arguments, and `{prompt_file}` (a path we chose), run. POSIX, or a program that is no
    batch file: None."""
    platform = sys.platform if platform is None else platform
    if platform != "win32" or not argv:
        return None
    import ntpath
    exe = str(argv[0])
    if ntpath.splitext(exe)[1].lower() not in (".cmd", ".bat"):
        return None
    for arg in argv[1:]:
        bad = sorted({c for c in str(arg) if c in CMD_METACHARACTERS})
        if bad:
            shown = " ".join({"\r": "CR", "\n": "LF"}.get(c, c) for c in bad)
            return ("refusing to run %s with an argument holding %s: it is a batch file, run through cmd.exe, "
                    "which would act on those characters (run commands, expand variables, cut the line). "
                    "Name the program itself in the agent command (claude.exe, or node and the script the "
                    "shim starts), or pass the prompt as {prompt_file}" % (exe, shown))
    return None


WINDOWS_ENV = ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA",
               "LOCALAPPDATA", "TEMP", "TMP", "USERNAME", "USERDOMAIN", "PROGRAMDATA", "ProgramFiles")


def windows_base_env(environ=None, platform=None):
    """What a Windows process needs from its parent's environment to start at all (a Python
    without SYSTEMROOT cannot even seed its random numbers), for an environment otherwise built
    from scratch. {} on POSIX, where such an environment needs nothing added."""
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return {}
    environ = os.environ if environ is None else environ
    return {k: environ[k] for k in WINDOWS_ENV if environ.get(k)}
