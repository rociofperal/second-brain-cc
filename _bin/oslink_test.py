#!/usr/bin/env python3
"""oslink: the Windows junction path goes through _winapi.CreateJunction (no shell), mklink only as a fallback."""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oslink

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("  ok  " + name)
    else:
        failed += 1
        print("  FAIL " + name)


tmp = tempfile.mkdtemp(prefix="oslink-test-")
target = os.path.join(tmp, "a&b^c")
os.makedirs(target)

real_symlink, real_platform, real_run, real_mod = os.symlink, sys.platform, oslink.subprocess.run, oslink._winapi_module


def no_symlink(*a, **k):
    raise OSError("no privilege")


class FakeWinapi:
    calls = []
    fail = False

    @classmethod
    def CreateJunction(cls, src, dst):
        cls.calls.append((src, dst))
        if cls.fail:
            raise OSError("boom")
        os.mkdir(dst)                    # stands in for the junction


ran = []


def fake_run(cmd, **k):
    ran.append(cmd)
    os.mkdir(cmd[4].strip('"'))
    return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()


try:
    os.symlink = no_symlink
    sys.platform = "win32"
    oslink._winapi_module = lambda: FakeWinapi
    oslink.subprocess.run = fake_run
    link = os.path.join(tmp, "l1")
    check("junction made via CreateJunction", oslink.make_dir_link(target, link) == "junction")
    check("called with (target, link), unquoted", FakeWinapi.calls == [(target, link)])
    check("the shell was not used", ran == [])

    FakeWinapi.fail = True
    link2 = os.path.join(tmp, "l2")
    check("falls back to mklink when CreateJunction fails", oslink.make_dir_link(target, link2) == "junction")
    check("the fallback quotes both paths", ran and ran[0][4] == '"%s"' % link2 and ran[0][5] == '"%s"' % target)

    oslink._winapi_module = lambda: None
    link3 = os.path.join(tmp, "l3")
    ran.clear()
    check("no _winapi (not Windows Python): quoted mklink", oslink.make_dir_link(target, link3) == "junction" and len(ran) == 1)
finally:
    os.symlink, sys.platform, oslink.subprocess.run, oslink._winapi_module = real_symlink, real_platform, real_run, real_mod
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print("RESULT: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
