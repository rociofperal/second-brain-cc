#!/usr/bin/env python3
"""Directory links that work on POSIX and on Windows without administrator rights (stdlib only).

`os.symlink` needs administrator rights or Developer Mode on Windows. A directory junction
(`mklink /J`) does not, and for a directory it behaves the same for every reader that matters:
the old path resolves to the new one. `make_dir_link` tries the symlink first and falls back
to a junction; `is_link` is true for either.
"""
import os
import subprocess
import sys


def is_link(path):
    """True for a symlink, and on Windows for a junction too (islink() is False for those)."""
    if os.path.islink(path):
        return True
    junction = getattr(os.path, "isjunction", None)
    if junction is not None:
        return junction(path)
    if sys.platform == "win32" and os.path.isdir(path):
        try:
            return bool(os.lstat(path).st_file_attributes & 0x400) and not os.path.islink(path)
        except (OSError, AttributeError):
            return False
    return False


def _winapi_module():
    try:
        import _winapi
        return _winapi
    except ImportError:
        return None


def make_dir_link(target, link):
    """Make `link` resolve to the directory `target`. Raises OSError when neither way works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            raise
    create = getattr(_winapi_module(), "CreateJunction", None)
    if create is not None:
        try:
            create(os.fspath(target), os.fspath(link))    # no shell: & and ^ in a user name are harmless
            if os.path.isdir(link):
                return "junction"
        except OSError:
            pass
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", '"%s"' % os.fspath(link), '"%s"' % os.fspath(target)],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isdir(link):
        raise OSError("cannot link %s to %s: %s" % (link, target, (proc.stdout + proc.stderr).strip()))
    return "junction"


def remove_link(link):
    """Remove a link made by make_dir_link without touching what it points at."""
    if os.path.islink(link):
        os.remove(link)
    else:
        os.rmdir(link)                   # a junction is removed with rmdir
