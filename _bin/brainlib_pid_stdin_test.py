#!/usr/bin/env python3
"""brainlib.pid_alive keeps upstream semantics (PermissionError is not our session) and the
win32 stdin reader reads raw bytes on a thread."""
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import brainlib

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok  " + name)
    else:
        failed += 1
        print("  FAIL " + name)


if sys.platform != "win32":
    check("own pid is alive", brainlib.pid_alive(os.getpid()) is True)
    check("own pid as a string", brainlib.pid_alive(str(os.getpid())) is True)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    time.sleep(0.2)
    check("a dead child is not alive", brainlib.pid_alive(child.pid) is False)
    check("garbage is not alive", brainlib.pid_alive("?") is False and brainlib.pid_alive(None) is False)

    real_kill = os.kill

    def denied(pid, sig):
        raise PermissionError(1, "not permitted")
    os.kill = denied
    try:
        check("PermissionError (another user's process) is not alive", brainlib.pid_alive(1) is False)
    finally:
        os.kill = real_kill

# the win32 branch, by injection
real_platform, real_win = sys.platform, brainlib._pid_alive_windows
seen = []
try:
    sys.platform = "win32"
    brainlib._pid_alive_windows = lambda pid: seen.append(pid) or False
    check("on win32 the Windows probe decides (access denied -> False)", brainlib.pid_alive(4242) is False and seen == [4242])
    brainlib._pid_alive_windows = lambda pid: True
    check("on win32 a live, ours process is alive", brainlib.pid_alive(4242) is True)
finally:
    sys.platform, brainlib._pid_alive_windows = real_platform, real_win
if sys.platform != "win32":
    check("the real Windows probe never raises off Windows", brainlib._pid_alive_windows(os.getpid()) is False)

# win32 stdin branch, exercised on any OS in a child: fd 0 is a pipe
CHILD = r'''
import sys, json
sys.path.insert(0, %r)
import brainlib
sys.platform = "win32"
t = float(sys.argv[1])
print(json.dumps(brainlib._read_stdin_json(t)))
''' % HERE


def run(data, timeout, close=True):
    """The child's (stdout, stderr, exit status). close=True: `data` is sent and stdin closed, through
    communicate() alone (Python 3.9's communicate() flushes stdin first, so a stdin closed by hand
    before it raises "flush of closed file"). close=False: stdin stays open and silent until the
    child has exited on its own, which communicate() cannot do (it closes stdin when given no input)."""
    p = subprocess.Popen([sys.executable, "-c", CHILD, str(timeout)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if close:
        out, err = p.communicate(input=data or b"", timeout=30)
        return out.decode().strip(), err.decode(), p.returncode
    try:
        p.wait(timeout=30)                 # its output is a few bytes: the pipes cannot fill up
        out, err = p.stdout.read(), p.stderr.read()
    finally:
        p.stdin.close()
        p.stdout.close()
        p.stderr.close()
    return out.decode().strip(), err.decode(), p.returncode


out, err, rc = run('{"k": "ñandú ✓"}'.encode("utf-8"), 2)
check("win32 branch decodes utf-8 bytes", out == '{"k": "\\u00f1and\\u00fa \\u2713"}', out)
big = ('{"k": "%s"}' % ("x" * 200000)).encode()
out, err, rc = run(big, 5)
check("win32 branch reads input larger than one chunk", out.startswith('{"k": "xxx') and rc == 0)
out, err, rc = run(None, 0.5, close=False)
check("win32 branch: an open, silent stdin times out to {} and exits cleanly",
      out == "{}" and rc == 0 and "Fatal" not in err, (out, err, rc))

print("RESULT: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
