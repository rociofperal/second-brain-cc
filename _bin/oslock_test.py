#!/usr/bin/env python3
"""Tests for oslock — the cross-platform exclusive file lock (flock on POSIX, msvcrt on Windows).

Lock and unlock with a file object and with an fd, a second process's non-blocking attempt
failing while the lock is held and succeeding after release, a blocking attempt waiting for
the holder and then getting it, and the lock file staying readable and writable while held.
Only portable calls (subprocess with sys.executable, no fork, no signals), so it is valid on
Windows too. Run standalone:

    python3 _bin/oslock_test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import oslock

ok, fail = [], []
TMP = tempfile.mkdtemp(prefix="oslock_test_")

# A second process: try the lock and print what happened.
#   argv: <path> <mode: nb|block> <use: fh|fd>
CHILD = r"""
import os, sys, time
sys.path.insert(0, %r)
import oslock
path, mode, use = sys.argv[1], sys.argv[2], sys.argv[3]
fh = open(path, "a+")
target = fh if use == "fh" else fh.fileno()
t0 = time.time()
try:
    oslock.lock(target, blocking=(mode == "block"))
except BlockingIOError:
    print("BLOCKED"); sys.exit(0)
except OSError as exc:
    print("OSERROR %%r" %% (exc,)); sys.exit(0)
print("GOT %%.2f" %% (time.time() - t0)); sys.stdout.flush()
oslock.unlock(target)
fh.close()
""" % HERE


def check(name, cond, detail=""):
    (ok if cond else fail).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("\n      → " + str(detail)) if detail else ""))


def child(path, mode, use="fh", wait=True):
    args = [sys.executable, "-c", CHILD, path, mode, use]
    if not wait:
        return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    return (r.stdout.strip() or r.stderr.strip())


def lock_path(name):
    return os.path.join(TMP, name + ".lock")


def test_lock_unlock_in_process():
    for use in ("fh", "fd"):
        path = lock_path("basic_" + use)
        with open(path, "a+") as fh:
            target = fh if use == "fh" else fh.fileno()
            try:
                oslock.lock(target)
                oslock.unlock(target)
                oslock.lock(target, blocking=False)
                oslock.unlock(target)
                check("lock/unlock with a %s, blocking and non-blocking" % use, True)
            except Exception as exc:
                check("lock/unlock with a %s, blocking and non-blocking" % use, False, repr(exc))


def test_nonblocking_from_second_process():
    for use in ("fh", "fd"):
        path = lock_path("nb_" + use)
        with open(path, "a+") as fh:
            target = fh if use == "fh" else fh.fileno()
            oslock.lock(target)
            out = child(path, "nb", use)
            check("a second process's non-blocking lock raises BlockingIOError while held (%s)" % use,
                  out == "BLOCKED", out)
            oslock.unlock(target)
            out = child(path, "nb", use)
            check("and succeeds once the holder unlocks (%s)" % use, out.startswith("GOT"), out)


def test_nonblocking_error_matches_callers():
    path = lock_path("errno")
    with open(path, "a+") as fh:
        oslock.lock(fh)
        r = subprocess.run([sys.executable, "-c",
                            "import sys, errno; sys.path.insert(0, %r); import oslock\n"
                            "fh = open(%r, 'a+')\n"
                            "try:\n    oslock.lock(fh, blocking=False)\n"
                            "except OSError as e:\n"
                            "    print(isinstance(e, BlockingIOError), e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES))\n"
                            % (HERE, path)],
                           capture_output=True, text=True, timeout=60)
        oslock.unlock(fh)
    check("the contention error is an OSError/BlockingIOError with the errno tasks.py expects",
          r.stdout.strip() == "True True", r.stdout + r.stderr)


def test_blocking_waits_then_gets_it():
    path = lock_path("block")
    with open(path, "a+") as fh:
        oslock.lock(fh)
        p = child(path, "block", "fd", wait=False)
        time.sleep(1.5)
        check("a blocking lock in a second process is still waiting while held", p.poll() is None)
        oslock.unlock(fh)
        try:
            out, err = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        out = (out or "").strip()
        waited = float(out.split()[1]) if out.startswith("GOT ") else -1
        check("then gets it once the holder unlocks", out.startswith("GOT"), out + (err or ""))
        check("and it really waited for the holder (>= 0.3 s after starting)", waited >= 0.3, waited)


def test_file_usable_while_locked():
    path = lock_path("io")
    with open(path, "a+") as fh:
        oslock.lock(fh)
        fh.write("pid 123\n")
        fh.flush()
        fh.seek(0)
        mine = fh.read()
        with open(path) as other:
            theirs = other.read()
        oslock.unlock(fh)
        check("the holder can write and read the lock file while holding it", mine == "pid 123\n", repr(mine))
        check("another handle can read the lock file while it is held", theirs == "pid 123\n", repr(theirs))
    with open(path, "r+b") as fh:
        fh.seek(3)
        oslock.lock(fh)
        after_lock = (fh.tell(), os.lseek(fh.fileno(), 0, os.SEEK_CUR))
        oslock.unlock(fh)
        after_unlock = (fh.tell(), os.lseek(fh.fileno(), 0, os.SEEK_CUR))
        check("lock and unlock leave the file position where it was",
              after_lock == (3, 3) and after_unlock == (3, 3), (after_lock, after_unlock))


def main():
    try:
        for t in (test_lock_unlock_in_process, test_nonblocking_from_second_process,
                  test_nonblocking_error_matches_callers, test_blocking_waits_then_gets_it,
                  test_file_usable_while_locked):
            print("\n== %s ==" % t.__name__)
            try:
                t()
            except Exception as exc:
                check("%s ran without raising" % t.__name__, False, repr(exc))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    return finish()


def finish():
    print("\nRESULT: %d passed, %d failed" % (len(ok), len(fail)))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
