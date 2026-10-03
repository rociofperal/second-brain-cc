#!/usr/bin/env python3
"""osproc: the alive/gone probe works for this process, a finished child and garbage input."""
import os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import osproc

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("  ok  " + name)
    else:
        failed += 1
        print("  FAIL " + name)


check("own pid is alive", osproc.pid_state(os.getpid()) is True)
check("own pid, as a string", osproc.pid_state(str(os.getpid())) is True)
check("pid_alive wraps it", osproc.pid_alive(os.getpid()) is True)

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(0.3)
check("running child is alive", osproc.pid_state(child.pid) is True)
child.kill()
child.wait()
time.sleep(0.2)
check("finished child is gone", osproc.pid_state(child.pid) is False)
check("finished child, strict reading", osproc.pid_alive(child.pid) is False)

for bad in (None, "", "abc", 0, -5):
    check("garbage %r is not alive" % (bad,), osproc.pid_state(bad) is False)

# ---- the portable process-control helpers, both platforms checked from either one
import types

check("current_uid is None on Windows", osproc.current_uid("win32") is None)
if hasattr(os, "getuid"):
    check("current_uid is os.getuid() on POSIX", osproc.current_uid("linux") == os.getuid())
check("is_root is never true on Windows", osproc.is_root("win32") is False)

flags = osproc.new_group_kwargs("win32")
check("a new process group on Windows is CREATE_NEW_PROCESS_GROUP, never start_new_session",
      flags == {"creationflags": 0x200})
check("and a new session on POSIX", osproc.new_group_kwargs("linux") == {"start_new_session": True})
det = osproc.detached_kwargs("win32")
check("a detached worker on Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | BREAKAWAY",
      det["creationflags"] == 0x08 | 0x200 | 0x08000000 | 0x01000000 and "start_new_session" not in det)
check("a detached worker on POSIX: a new session", osproc.detached_kwargs("darwin") == {"start_new_session": True})

seen = []
fake_proc = types.SimpleNamespace(pid=4242, kill=lambda: seen.append("kill"))
ok_run = lambda cmd, **kw: seen.append(cmd) or types.SimpleNamespace(returncode=0)
check("kill_tree on Windows asks taskkill for the whole tree, forced",
      osproc.kill_tree(fake_proc, "win32", run=ok_run) and seen == [["taskkill", "/T", "/F", "/PID", "4242"]])
seen.clear()
bad_run = lambda cmd, **kw: types.SimpleNamespace(returncode=128)
check("when taskkill fails it falls back to killing the process itself",
      osproc.kill_tree(fake_proc, "win32", run=bad_run) and seen == ["kill"])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **osproc.new_group_kwargs())
osproc.kill_tree(child)
check("kill_tree ends a real child started in its own group", child.wait(timeout=10) is not None)

check("split_command keeps a Windows path whole, quoted or not",
      osproc.split_command(r'"C:\Program Files\x\claude.exe" -p {prompt} C:\a\b', "win32")
      == [r"C:\Program Files\x\claude.exe", "-p", "{prompt}", r"C:\a\b"])
check("and is shlex.split on POSIX", osproc.split_command("a 'b c' d", "linux") == ["a", "b c", "d"])
for line, want in ((r'claude --x="a b" y', ["claude", "--x=a b", "y"]),
                   (r'a"b c"d e', ["ab cd", "e"]),
                   (r'"a ""q"" b"', ['a "q" b']),
                   (r'a\\\"b', ['a\\"b']),
                   (r'"C:\dir\\" next', ["C:\\dir\\", "next"]),
                   (r'x\y\\z "" end', ["x\\y\\\\z", "", "end"]),
                   ("claude -p {prompt} --allowedTools 'Bash(git status:*)'",
                    ["claude", "-p", "{prompt}", "--allowedTools", "Bash(git status:*)"]),
                   ("it's\tfine", ["it's", "fine"])):
    got = osproc.split_command(line, "win32")
    check("split_command on Windows follows CommandLineToArgvW: %s -> %r" % (line, got), got == want)
for bad in ('"open', "'open"):
    try:
        osproc.split_command(bad, "win32")
        check("an unclosed quote raises ValueError: %s" % bad, False)
    except ValueError:
        check("an unclosed quote raises ValueError: %s" % bad, True)
check("POSIX split_command is unchanged for the same line",
      osproc.split_command('claude --x="a b" y', "linux") == ["claude", "--x=a b", "y"])

calls = []


def _popen(refuse):
    def popen(argv, **kw):
        calls.append(kw.get("creationflags"))
        if refuse and kw.get("creationflags", 0) & osproc.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(13, "Access is denied")
        return types.SimpleNamespace(pid=1, kw=kw)
    return popen


check("detached workers on Windows ask to break away from the parent's job",
      osproc.detached_kwargs("win32")["creationflags"] & 0x01000000)
p = osproc.spawn_detached(["w"], "win32", popen=_popen(False), stdout=-3)
check("spawn_detached starts it once with the detached flags and the caller's own arguments",
      calls == [0x08 | 0x200 | 0x08000000 | 0x01000000] and p.kw["stdout"] == -3 and p.kw["close_fds"])
calls.clear()
p = osproc.spawn_detached(["w"], "win32", popen=_popen(True))
check("a job that forbids breakaway: started again without CREATE_BREAKAWAY_FROM_JOB",
      calls == [0x08 | 0x200 | 0x08000000 | 0x01000000, 0x08 | 0x200 | 0x08000000])
calls.clear()


def _missing(argv, **kw):
    calls.append(kw.get("creationflags"))
    raise FileNotFoundError(2, "nope")


try:
    osproc.spawn_detached(["w"], "win32", popen=_missing)
    check("a missing program is raised, not retried", False)
except FileNotFoundError:
    check("a missing program is raised, not retried", len(calls) == 1)
calls.clear()
try:
    osproc.spawn_detached(["w"], "linux", popen=lambda a, **kw: (_ for _ in ()).throw(PermissionError("x")))
    check("POSIX errors are raised as they are", False)
except PermissionError:
    check("POSIX errors are raised as they are", True)
check("POSIX spawn_detached is a new session", osproc.spawn_detached(["w"], "linux", popen=_popen(False)).kw
      == {"start_new_session": True})
files = {r"C:\bin\claude.cmd"}
check("resolve_exe finds claude.cmd for C:\\bin\\claude on Windows",
      osproc.resolve_exe(r"C:\bin\claude", "win32", {"PATHEXT": ".EXE;.CMD"}, isfile=files.__contains__)
      == r"C:\bin\claude.cmd")
check("keeps a path that already has its extension",
      osproc.resolve_exe(r"C:\bin\claude.exe", "win32", {"PATHEXT": ".EXE"}, isfile=lambda p: False)
      == r"C:\bin\claude.exe")
check("and changes nothing on POSIX", osproc.resolve_exe("/bin/claude", "linux") == "/bin/claude")
base = osproc.windows_base_env({"SYSTEMROOT": r"C:\Windows", "PATH": "x", "SECRET": "s"}, "win32")
check("windows_base_env carries what a Windows process needs to start, nothing else",
      base == {"SYSTEMROOT": r"C:\Windows"})
check("and nothing on POSIX", osproc.windows_base_env({"SYSTEMROOT": "x"}, "linux") == {})

# ---- npm shims: node + script instead of cmd.exe, which cuts argv at the first newline
NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*
"""
SHIM = r"C:\Users\u\AppData\Roaming\npm\claude.cmd"
CLI = r"C:\Users\u\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\cli.js"
NODE_BESIDE = r"C:\Users\u\AppData\Roaming\npm\node.exe"
have = lambda *fs: (lambda p: p in fs)
got = osproc.unwrap_npm_shim(SHIM, "win32", read=lambda p: NPM_SHIM, isfile=have(CLI, NODE_BESIDE), which=lambda n: None)
check("an npm shim unwraps to node.exe beside it plus the script its text names", got == [NODE_BESIDE, CLI])
got = osproc.unwrap_npm_shim(SHIM, "win32", read=lambda p: NPM_SHIM, isfile=have(CLI), which=lambda n: r"C:\node\node.exe")
check("without node.exe beside it, node comes from PATH", got == [r"C:\node\node.exe", CLI])
got = osproc.unwrap_npm_shim(SHIM, "win32", read=lambda p: NPM_SHIM, isfile=have(CLI), which=lambda n: None)
check("and is plain `node` when PATH has none either", got == ["node", CLI])
other = NPM_SHIM.replace(r"@anthropic-ai\claude-code\cli.js", r"other-tool\bin\run.js")
OTHER = r"C:\Users\u\AppData\Roaming\npm\node_modules\other-tool\bin\run.js"
got = osproc.unwrap_npm_shim(r"C:\Users\u\AppData\Roaming\npm\other.cmd", "win32", read=lambda p: other,
                             isfile=have(OTHER), which=lambda n: "node")
check("the script is parsed from the shim, not hard-coded", got == ["node", OTHER])
bare = "@echo off\r\n\"%dp0%\\nothing-useful\" %*\r\n"
got = osproc.unwrap_npm_shim(SHIM, "win32", read=lambda p: bare, isfile=have(CLI), which=lambda n: "node")
check("a claude shim whose text shows no script falls back to the known cli.js", got == ["node", CLI])
exe_shim = '@ECHO off\r\n"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n'
EXE = r"C:\Users\u\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
got = osproc.unwrap_npm_shim(SHIM, "win32", read=lambda p: exe_shim, isfile=have(EXE))
check("a shim that launches a native exe gives that exe", got == [EXE])
check("a .cmd that is no npm shim is None",
      osproc.unwrap_npm_shim(r"C:\x\run.cmd", "win32", read=lambda p: "@echo off\r\necho hi\r\n", isfile=lambda p: True) is None)
check("so is a .exe, an unreadable shim and a shim whose script is gone",
      osproc.unwrap_npm_shim(r"C:\x\claude.exe", "win32", read=lambda p: NPM_SHIM, isfile=lambda p: True) is None
      and osproc.unwrap_npm_shim(SHIM, "win32", read=lambda p: None, isfile=lambda p: True) is None
      and osproc.unwrap_npm_shim(SHIM, "win32", read=lambda p: NPM_SHIM, isfile=lambda p: False) is None)
check("and everything is None on POSIX",
      osproc.unwrap_npm_shim("/x/claude.cmd", "linux", read=lambda p: NPM_SHIM, isfile=lambda p: True) is None)
def _boom(p):
    raise RuntimeError("x")
check("it never raises", osproc.unwrap_npm_shim(SHIM, "win32", read=_boom, isfile=_boom) is None)

import io
class _Tty(io.StringIO):
    def isatty(self):
        return True
check("isatty: a stream that is no terminal is not one", osproc.isatty(io.StringIO()) is False
      and osproc.isatty(None) is False)
check("isatty: on POSIX a terminal is one", osproc.isatty(_Tty(), "linux") is True)
with open(os.devnull) as _nul:
    check("isatty: the null device is never a terminal (on Windows isatty() says it is)",
          osproc.isatty(_nul) is False)

print("RESULT: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
