#!/usr/bin/env python3
"""jobrun.py — how Windows Task Scheduler starts a Brain job.

  pythonw.exe -X utf8 jobrun.py <label> <script.py> [args...]

Task Scheduler has no per-task environment and no log redirection, which systemd units
(Environment=, the journal) and launchd plists (EnvironmentVariables, StandardOutPath) both
have. This is the small piece in between: it sets BRAIN_JOB_LABEL (how a job knows it runs
scheduled, and how the guardian recognises its own job) and PYTHONUTF8=1 (so any Python the job
starts runs in UTF-8 mode too), sends stdout and stderr to <brain state>/logs/<label>.log
(pythonw.exe has no console to print to), then runs the script in this same interpreter as
__main__, with its own arguments. The script's exit status is the task's Last Run Result.

pythonw.exe also has no console, so every console program the job starts (git, claude.exe,
schtasks, tasklist) would open a console window of its own, and closing one kills that child.
On Windows jobrun first gives itself a console and hides it (hide_console): the children share
that hidden one instead.

Standard library only; on macOS and Linux it works the same and is simply not used.
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def log_path(label, environ=None):
    sys.path.insert(0, HERE)
    import brain_paths

    return os.path.join(brain_paths.state_dir(environ), "logs", "%s.log" % label)


def prepare(label, environ):
    """The environment a scheduled job runs with. Pure apart from `environ`, which it updates."""
    environ["BRAIN_JOB_LABEL"] = label
    environ["PYTHONUTF8"] = "1"
    return environ


SW_HIDE = 0


def hide_console(platform=None, kernel32=None, user32=None):
    """Windows, when this process has no console: allocate one and hide its window, so the console
    programs a job starts inherit it rather than each opening a visible window. Best effort: True
    when a hidden console was set up, False otherwise, never an exception. The kernel32/user32
    handles are injectable for tests."""
    if (sys.platform if platform is None else platform) != "win32":
        return False
    try:
        if kernel32 is None or user32 is None:
            import ctypes

            kernel32 = kernel32 or ctypes.WinDLL("kernel32")
            user32 = user32 or ctypes.WinDLL("user32")
        if kernel32.GetConsoleWindow():            # started from a console: leave it as it is
            return False
        if not kernel32.AllocConsole():
            return False
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, SW_HIDE)
        return bool(hwnd)
    except Exception:
        return False


def main(argv=None, environ=None, run=runpy.run_path, platform=None, console=hide_console):
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    if len(argv) < 2:
        sys.stderr.write("usage: jobrun.py <label> <script.py> [args...]\n") if sys.stderr else None
        return 2
    label, script, args = argv[0], argv[1], argv[2:]
    prepare(label, environ)
    log = None
    if sys.stdout is None or sys.stderr is None:          # pythonw.exe: no console at all
        try:
            path = log_path(label, environ)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            log = open(path, "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or log
            sys.stderr = sys.stderr or log
        except OSError:
            pass
    console(platform)                                       # after the check above: pythonw's None streams stay logged
    old_argv = sys.argv
    sys.argv = [script] + list(args)
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
    try:
        run(script, run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        if sys.stderr:
            sys.stderr.write("%s\n" % code)
        return 1
    finally:
        sys.argv = old_argv
        if log:
            log.flush()


if __name__ == "__main__":
    sys.exit(main())
