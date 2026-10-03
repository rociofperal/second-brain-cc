#!/usr/bin/env python3
"""Fake executables for the tests, on POSIX and on Windows (test support, standard library only).

A test that stands a fake `launchctl`, `claude` or `keepassxc-cli` in for the real one writes a
small `#!/bin/sh` (or `#!/usr/bin/env python3`) script and passes its path. POSIX runs that script
directly. Windows cannot (CreateProcess: "[WinError 193] %1 is not a valid Win32 application"), so
fake_exe() also writes `<path>.cmd`, a one-line launcher that hands the script to Git for Windows'
sh.exe (or to this Python for a Python script), and returns that path instead.

    fake = fake_exe(os.path.join(d, "launchctl"), "#!/bin/sh\\nexit 0\\n")
    LaunchctlControl(..., launchctl=fake)

On Windows the script itself stays at `path`, so a test that reads it back still finds it.
`fake_dir_on_path` is for a fake found through PATH: `.cmd` is in PATHEXT, so shutil.which()
finds `name.cmd` where it would find `name` on POSIX.
"""
import os
import shutil
import sys

IS_WINDOWS = sys.platform == "win32"


def find_sh():
    """Git for Windows' sh.exe, or None. Never WSL's bash (System32), which needs a distribution."""
    candidates = []
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.realpath(git)))    # ...\Git\cmd\git.exe -> ...\Git
        candidates += [os.path.join(root, "usr", "bin", "sh.exe"), os.path.join(root, "bin", "sh.exe")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"):
        if base:
            candidates += [os.path.join(base, "Git", "usr", "bin", "sh.exe"), os.path.join(base, "Git", "bin", "sh.exe")]
    found = shutil.which("sh")
    if found and "system32" not in found.lower():
        candidates.append(found)
    return next((c for c in candidates if os.path.isfile(c)), None)


def _interpreter(text):
    first = text.splitlines()[0] if text else ""
    if first.startswith("#!") and "python" in first:
        return sys.executable
    return find_sh()


def fake_exe(path, text, mode=0o755):
    """Write the script at `path` and return the path to run it by: `path` itself on POSIX,
    `path + ".cmd"` on Windows. None on Windows when no sh.exe is installed for a shell script."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.chmod(path, mode)
    if not IS_WINDOWS:
        return path
    interp = _interpreter(text)
    if not interp:
        return None
    launcher = path + ".cmd"
    with open(launcher, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(_launcher_text(interp, path))
    return launcher


def sh_tool_dirs(sh, isdir=os.path.isdir):
    """The folders Git for Windows' sh.exe needs on PATH to find its own tools (sleep, cat, env)."""
    here = os.path.dirname(sh)
    dirs = [here]
    usr_bin = os.path.join(os.path.dirname(here), "usr", "bin")        # Git\bin\sh.exe -> Git\usr\bin
    if isdir(usr_bin) and os.path.normcase(usr_bin) != os.path.normcase(here):
        dirs.append(usr_bin)
    return dirs


def _launcher_text(interp, path, isdir=os.path.isdir):
    """The .cmd that runs `path` with `interp`. Git for Windows' sh.exe finds sleep, cat and the
    rest of its tools only when Git's usr\\bin is on PATH, which a plain Windows PATH does not have
    (the GitHub runner happens to add it): the launcher puts it there, for this call only."""
    extra = sh_tool_dirs(interp, isdir) if interp != sys.executable else []
    lines = ["@setlocal"]
    if extra:
        lines.append('@set "PATH=%s;%%PATH%%"' % ";".join(extra))
    lines += ['@"%s" "%s" %%*' % (interp, path), "@exit /b %ERRORLEVEL%"]
    return "\n".join(lines) + "\n"


def runnable(path):
    """True when fake_exe() could make a runnable fake here (always on POSIX)."""
    return path is not None


def skip(name, reason):
    """The line a test prints for a check it does not run on this platform."""
    print("  - %s (skipped on Windows: %s)" % (name, reason))
