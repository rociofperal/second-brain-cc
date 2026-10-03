#!/usr/bin/env python3
"""Tests for jobrun.py, the launcher Windows Task Scheduler starts every Brain job through.

The script it runs is a scratch one; the Brain state is a temporary directory. Run standalone:

    python3 _bin/jobrun_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jobrun  # noqa: E402

ok, fail = [], []


def check(name, cond, detail=""):
    (ok if cond else fail).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("\n      → " + str(detail)) if detail else ""))


SCRIPT = r'''
import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json"), "w") as fh:
    json.dump({"argv": sys.argv, "label": os.environ.get("BRAIN_JOB_LABEL"), "utf8": os.environ.get("PYTHONUTF8"),
               "name": __name__, "flags_utf8": sys.flags.utf8_mode}, fh)
print("ran")
if "--fail" in sys.argv:
    sys.exit(3)
if "--raise" in sys.argv:
    raise RuntimeError("boom")
'''


def main():
    root = tempfile.mkdtemp(prefix="jobrun-test-")
    try:
        script = os.path.join(root, "job.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(SCRIPT)
        seen_path = os.path.join(root, "seen.json")
        env = {}
        print("\n== in process ==")
        rc = jobrun.main(["second-brain-guardian", script, "repair"], environ=env)
        seen = json.load(open(seen_path))
        check("the script runs as __main__ with its own arguments",
              rc == 0 and seen["name"] == "__main__" and seen["argv"] == [script, "repair"], (rc, seen))
        check("BRAIN_JOB_LABEL and PYTHONUTF8 are set for it and for what it starts",
              env == {"BRAIN_JOB_LABEL": "second-brain-guardian", "PYTHONUTF8": "1"}, env)
        check("its exit status is the job's", jobrun.main(["x", script, "--fail"], environ={}) == 3)
        check("too few arguments is a usage error", jobrun.main(["only-a-label"], environ={}) == 2)

        print("\n== a hidden console for the job's children (Windows, pythonw.exe) ==")

        class K32:
            def __init__(self, window=0, alloc=1, raise_on=None):
                self.window, self.alloc, self.raise_on, self.calls = window, alloc, raise_on, []

            def GetConsoleWindow(self):
                self.calls.append("GetConsoleWindow")
                if self.raise_on == "get":
                    raise OSError("no")
                return self.window

            def AllocConsole(self):
                self.calls.append("AllocConsole")
                if self.alloc:
                    self.window = 4242
                return self.alloc

        class U32:
            def __init__(self):
                self.calls = []

            def ShowWindow(self, hwnd, cmd):
                self.calls.append((hwnd, cmd))
                return 1

        k, u = K32(), U32()
        check("no console: one is allocated and its window hidden",
              jobrun.hide_console("win32", k, u) is True and "AllocConsole" in k.calls and u.calls == [(4242, 0)],
              (k.calls, u.calls))
        k, u = K32(window=7), U32()
        check("started from a console: left alone", jobrun.hide_console("win32", k, u) is False
              and "AllocConsole" not in k.calls and u.calls == [])
        k, u = K32(alloc=0), U32()
        check("AllocConsole refused: False, nothing shown", jobrun.hide_console("win32", k, u) is False and u.calls == [])
        check("an error is swallowed", jobrun.hide_console("win32", K32(raise_on="get"), U32()) is False)
        k, u = K32(), U32()
        check("POSIX: nothing is called", jobrun.hide_console("linux", k, u) is False and k.calls == [])
        seen_platform = []
        jobrun.main(["x", script], environ={}, platform="win32", console=seen_platform.append)
        check("main sets the console up before running the job", seen_platform == ["win32"], seen_platform)

        print("\n== as Task Scheduler starts it ==")
        child_env = dict(os.environ, BRAIN_STATE=os.path.join(root, "state"))
        child_env.pop("PYTHONUTF8", None)
        child_env.pop("BRAIN_JOB_LABEL", None)
        p = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, "jobrun.py"), "second-brain-watch", script,
                            "tick"], env=child_env, capture_output=True, text=True, timeout=60)
        seen = json.load(open(seen_path))
        check("python -X utf8 jobrun.py <label> <script> <args> runs it in UTF-8 mode, labelled",
              p.returncode == 0 and "ran" in p.stdout and seen["label"] == "second-brain-watch"
              and seen["utf8"] == "1" and seen["flags_utf8"] == 1 and seen["argv"] == [script, "tick"],
              (p.returncode, p.stdout, p.stderr, seen))
        p = subprocess.run([sys.executable, os.path.join(HERE, "jobrun.py"), "lbl", script, "--raise"],
                           env=child_env, capture_output=True, text=True, timeout=60)
        check("an exception in the job is a non-zero exit with its traceback",
              p.returncode != 0 and "RuntimeError: boom" in p.stderr, (p.returncode, p.stderr[-300:]))
        log = jobrun.log_path("second-brain-watch", {"BRAIN_STATE": os.path.join(root, "state")})
        check("with no console (pythonw.exe) the output goes to <brain state>/logs/<label>.log",
              log == os.path.join(root, "state", "logs", "second-brain-watch.log"), log)
        code = ("import sys, runpy; sys.stdout = sys.stderr = None; sys.argv = %r; "
                "runpy.run_path(%r, run_name='__main__')"
                % ([os.path.join(HERE, "jobrun.py"), "second-brain-watch", script, "tick"], os.path.join(HERE, "jobrun.py")))
        p = subprocess.run([sys.executable, "-c", code], env=child_env, capture_output=True, text=True, timeout=60)
        check("and the job's own output lands there",
              os.path.exists(log) and "ran" in open(log, encoding="utf-8").read(), (p.returncode, p.stderr[-300:]))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\nRESULT: %d passed, %d failed" % (len(ok), len(fail)))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
