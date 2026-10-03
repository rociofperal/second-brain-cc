#!/usr/bin/env python3
"""Windows installation and agent integration: every command Brain generates for win32 is rendered here
on any platform (platform and interpreter are injected), and the wrappers are checked for what they must hold.

Covers: pycmd (the one place that names the interpreter), the Claude Code hook commands localized for a
Windows vault (guardian_core), the skills' interpreter (install_plugin), the recommended permissions
(claude_settings), the MCP snippets and registration (first run), the live-hook look of the watch, the
installers (install.py, bootstrap.py run for real in a scratch HOME) and the .ps1/.cmd wrappers.

    python3 _bin/windows_install_test.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for p in (HERE, os.path.join(REPO, "integrations", "first-run"), os.path.join(REPO, "integrations", "claude-code")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pycmd  # noqa: E402
import install_plugin as IP  # noqa: E402
import claude_settings as CS  # noqa: E402
from guardian_core import domain as GD  # noqa: E402
from guardian_core import claude_code as CC  # noqa: E402
from events_core import domain as ED  # noqa: E402
from first_run_core import domain as FD  # noqa: E402
from first_run_core import adapters as FA  # noqa: E402

ok, fail = [], []
V = "C:\\Users\\rocio\\Mi Brain"                      # a space in the path on purpose
E = "C:\\Program Files\\Python312\\python.exe"
HOOKS_JSON = os.path.join(REPO, "integrations", "claude-code", "plugin", "brain", "hooks", "hooks.json")


def check(name, cond, detail=""):
    (ok if cond else fail).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("\n      → " + str(detail)[:800]) if detail and not cond else ""))


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8", "newline": ""})) as fh:
        return fh.read()


def commands(hooks):
    return [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]


WIN_HOOK = re.compile(r'^"C:\\Program Files\\Python312\\python\.exe" -X utf8 '
                      r'"C:\\Users\\rocio\\Mi Brain\\_bin\\[a-z_]+\.py"( [^"]*)?$')


# ---------------------------------------------------------------- pycmd


def test_pycmd():
    print("\n== pycmd ==")
    check("a Windows hook command: quoted python.exe, -X utf8, quoted backslash script, then the arguments",
          pycmd.hook_command("C:/Users/rocio/Mi Brain/_bin/vault_sync.py", "--hook", platform="win32", executable=E)
          == '"%s" -X utf8 "%s\\_bin\\vault_sync.py" --hook' % (E, V))
    check("without arguments there is no trailing space",
          pycmd.hook_command(V + "\\_bin\\compass.py", platform="win32", executable=E).endswith('compass.py"'))
    check("POSIX hook command is `<python> <script> <args>` as it always was",
          pycmd.hook_command("/h/Brain/_bin/vault_sync.py", "--hook", platform="linux") == "/usr/bin/python3 /h/Brain/_bin/vault_sync.py --hook"
          and pycmd.hook_command("/h/x.py", python="/opt/py", platform="linux") == "/opt/py /h/x.py")
    check("interpreter(): Windows adds -X utf8, POSIX is the interpreter alone",
          pycmd.interpreter(platform="win32", executable=E) == [E, "-X", "utf8"]
          and pycmd.interpreter("/usr/bin/python3", platform="darwin") == ["/usr/bin/python3"])
    check("script_argv on Windows: backslash script after -X utf8",
          pycmd.script_argv("C:/v/_bin/x.py", ["a"], platform="win32", executable=E) == [E, "-X", "utf8", "C:\\v\\_bin\\x.py", "a"])
    check("mcp_command on Windows: python.exe with -X utf8 before the server",
          pycmd.mcp_command("C:/v/integrations/mcp/server.py", platform="win32", executable=E)
          == (E, ["-X", "utf8", "C:\\v\\integrations\\mcp\\server.py"]))
    check("mcp_command on POSIX: python3 and the server", pycmd.mcp_command("/v/s.py", platform="linux") == ("python3", ["/v/s.py"]))
    check("child_env asks for UTF-8 mode on Windows only",
          pycmd.child_env({"A": "1"}, "win32") == {"A": "1", "PYTHONUTF8": "1"} and pycmd.child_env({"A": "1"}, "linux") == {"A": "1"})
    check("a Windows command line quotes words with spaces or backslashes",
          pycmd.shell_join([E, "-X", "utf8"], "win32") == '"%s" -X utf8' % E)


# ---------------------------------------------------------------- hooks


def test_hooks():
    print("\n== the hooks written into settings.json ==")
    canonical = json.load(open(HOOKS_JSON, encoding="utf-8"))["hooks"]
    posix = GD.localize_hooks(canonical, vault="/home/x/Vault", home="/home/x", platform="linux")
    old_way = json.loads(json.dumps(canonical).replace("/home/brain-origin/Brain", "/home/x/Vault").replace("/home/brain-origin", "/home/x"))
    check("POSIX output is the plain replacement it always was (python3 and slashes untouched)",
          posix == old_way and all(c.startswith("/usr/bin/python3 /home/x/Vault/_bin/") for c in commands(posix)))
    win = GD.localize_hooks(canonical, vault=V, home="C:\\Users\\rocio", platform="win32", executable=E)
    wc = commands(win)
    check("every Windows hook is `\"<python.exe>\" -X utf8 \"<vault>\\_bin\\<script>.py\" <args>`",
          wc and all(WIN_HOOK.match(c) for c in wc), [c for c in wc if not WIN_HOOK.match(c)][:3])
    check("no Windows hook keeps an origin path, /usr/bin/python3 or a forward slash",
          not any("brain-origin" in c or "/usr/bin" in c or "/" in c for c in wc), [c for c in wc if "/" in c][:2])
    check("arguments survive (`vault_sync.py --hook`)", any(c.endswith('vault_sync.py" --hook') for c in wc))
    check("timeouts, matchers and order are untouched",
          [{k: v for k, v in h.items() if k != "command"} for g in sum(win.values(), []) for h in g["hooks"]]
          == [{k: v for k, v in h.items() if k != "command"} for g in sum(posix.values(), []) for h in g["hooks"]]
          and list(win) == list(posix))
    check("a hook keeps its identity across platforms, so repair recognises it",
          [GD.hook_identity(c) for c in wc] == [GD.hook_identity(c) for c in commands(posix)], [GD.hook_identity(c) for c in wc][:3])
    check("the vault's scripts are found under the Windows vault", len(GD.brain_hook_paths(win, GD.brain_script_dirs(V))) == len(set(GD.brain_hook_paths(win, GD.brain_script_dirs(V)))) > 5)
    merged, changes = GD.reconcile_hooks(win, posix, GD.brain_script_dirs(V), set())
    check("a settings file with the POSIX commands is rewritten to the Windows ones by repair",
          changes and all(c.kind == "fixed" for c in changes) and commands(merged) == wc, [c.text for c in changes][:2])
    merged2, changes2 = GD.reconcile_hooks(win, win, GD.brain_script_dirs(V), set())
    check("and a second repair has nothing to do", changes2 == [] and merged2 == win)
    check("a command that is not the origin's (another tool's hook) is left alone",
          GD.localize_hooks({"Stop": [{"hooks": [{"type": "command", "command": "node C:\\x\\hook.js"}]}]},
                            V, "C:\\Users\\rocio", platform="win32", executable=E)["Stop"][0]["hooks"][0]["command"] == "node C:\\x\\hook.js")
    d = tempfile.mkdtemp(prefix="wininstall-")
    try:
        cf = CC.CanonicalHooksFile(HOOKS_JSON, vault=V, home="C:\\Users\\rocio", platform="win32", executable=E)
        check("CanonicalHooksFile.load gives the Windows commands", commands(cf.load()) == wc)
        check("and with no platform given, this machine's (POSIX here, win32 there)",
              commands(CC.CanonicalHooksFile(HOOKS_JSON, vault="/v", home="/h").load())[0].startswith(
                  "/usr/bin/python3 " if sys.platform != "win32" else '"'))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    wanted = ["vault_sync.py --hook", "compass.py"]
    check("the watch recognises Windows hooks as wired (no repair every five minutes)",
          ED.hooks_look_present({"SessionStart": [{"hooks": [{"command": c} for c in wc]}]}, wanted))
    check("and still notices a missing one",
          not ED.hooks_look_present({"SessionStart": [{"hooks": [{"command": c} for c in wc if "compass" not in c]}]}, wanted))
    check("POSIX live hooks are recognised as before",
          ED.hooks_look_present({"S": [{"hooks": [{"command": "/usr/bin/python3 /v/_bin/compass.py"}]}]}, ["compass.py"]))


# ---------------------------------------------------------------- skills and settings


def check_any_interpreter_backports():
    """A skill installed by one interpreter and back-ported by another (the guardian runs under
    pythonw.exe from Task Scheduler) must never put a Windows command into the canonical copy."""
    A, W = "C:\\Py312\\python.exe", "C:\\Py312\\pythonw.exe"
    canon = ("!`/usr/bin/python3 __VAULT__/_bin/kp.py status`\nallowed: Bash(python3 __VAULT__/_bin/machine_update.py:*),"
             " Bash(/usr/bin/python3 __VAULT__/_bin/machine_update.py:*)\nthen run `python3 __VAULT__/_bin/doctor.py`\n")
    live = IP.localize_text(canon, V, "win32", A)
    check("bare `python3 __VAULT__/_bin/x.py` is localized too, with the attached -Xutf8 spelling",
          '"%s" -Xutf8 "%s\\_bin\\doctor.py"' % (A, V) in live and "python3 __VAULT__" not in live, live)
    for other in (A, W, "C:\\Other\\python3.exe", "C:\\Windows\\py.exe", None):
        check("back-ported by %s: the canonical text exactly" % (other or "this Python"),
              IP.delocalize_text(live, V, "win32", other) == canon, IP.delocalize_text(live, V, "win32", other))
    for py in (W, "C:\\Windows\\py.exe", "C:\\Other\\PYTHON3.EXE"):
        written = IP.localize_text(canon, V, "win32", py)
        check("written by %s and read back by python.exe: canonical" % py,
              IP.delocalize_text(written, V, "win32", A) == canon, written)
    d = tempfile.mkdtemp(prefix="wininstall-")
    try:
        plugin, claude, state = (os.path.join(d, n) for n in ("plugin", "claude", "state"))
        os.makedirs(os.path.join(plugin, "skills", "kp"))
        with open(os.path.join(plugin, "skills", "kp", "SKILL.md"), "w", encoding="utf-8", newline="") as fh:
            fh.write(canon)
        IP.Syncer(plugin, claude, state, vault=V, platform="win32", executable=A).apply()
        g = IP.Syncer(plugin, claude, state, vault=V, platform="win32", executable=W)
        check("the guardian (pythonw.exe) sees the terminal's install as the same, not a back-port",
              [i["action"] for i in g.plan()] == ["same"], g.plan())
        g.apply()
        check("and the vault's canonical copy is untouched", read(os.path.join(plugin, "skills", "kp", "SKILL.md")) == canon)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    skills = os.path.join(REPO, "integrations", "claude-code", "plugin", "brain")
    for root, _, files in os.walk(skills):
        for name in files:
            if name.endswith(".md"):
                text = read(os.path.join(root, name))
                if "__VAULT__" in text and IP.delocalize_text(IP.localize_text(text, V, "win32", A), V, "win32", W) != text:
                    check("every shipped skill and agent round-trips on Windows: %s" % name, False)
                    return
    check("every shipped skill and agent round-trips on Windows (installed by python.exe, read by pythonw.exe)", True)


def test_canonical_interpreter():
    print("\n== the one Windows interpreter configs name ==")
    W, P = "C:\\Py312\\pythonw.exe", "C:\\Py312\\python.exe"
    there = lambda p: p == P
    check("pythonw.exe becomes the python.exe next to it",
          pycmd.windows_python(W, environ={}, exists=there) == P)
    check("unless there is none", pycmd.windows_python(W, environ={}, exists=lambda p: False) == W)
    check("BRAIN_PYTHON wins over this Python",
          pycmd.windows_python(environ={"BRAIN_PYTHON": "D:\\py\\python.exe"}) == "D:\\py\\python.exe")
    check("python.exe stays as it is", pycmd.windows_python(P, environ={}, exists=there) == P)
    real_exe, real_isfile = sys.executable, os.path.isfile
    try:
        sys.executable = W                       # what Task Scheduler runs the guardian under
        pycmd.windows_python.__defaults__ = (None, None, there)
        hooks = CC.CanonicalHooksFile(HOOKS_JSON, vault=V, home="C:\\Users\\rocio", platform="win32").load()
        cmds = [c for c in commands(hooks) if "_bin" in c]
        check("hooks built without an executable under pythonw.exe name python.exe",
              cmds and all(c.startswith('"%s" -X utf8' % P) for c in cmds), cmds[:3])
        check("so do skills and MCP entries",
              IP.localize_text("/usr/bin/python3 __VAULT__/_bin/q.py", V, "win32").startswith('"%s"' % P)
              and pycmd.mcp_command("C:/v/s.py", platform="win32")[0] == P)
    finally:
        sys.executable = real_exe
        pycmd.windows_python.__defaults__ = (None, None, real_isfile)


def test_skills_and_settings():
    print("\n== skills, agents and recommended permissions ==")
    canon = "Run: !`/usr/bin/python3 __VAULT__/_bin/query.py \"$ARGUMENTS\" --limit 8`\nallowed: Bash(/usr/bin/python3 __VAULT__/_bin/kp.py:*)\nsee __VAULT__/90-Meta\n"
    live = IP.localize_text(canon, V, "win32", E)
    check("on Windows a skill runs `\"<python.exe>\" -X utf8 \"<vault>\\_bin\\x.py\"`",
          '!`"%s" -X utf8 "%s\\_bin\\query.py" "$ARGUMENTS" --limit 8`' % (E, V) in live
          and 'Bash("%s" -X utf8 "%s\\_bin\\kp.py":*)' % (E, V) in live and "/usr/bin/python3" not in live, live)
    check("and turns back into the canonical text exactly", IP.delocalize_text(live, V, "win32", E) == canon)
    check("POSIX localization is the plain __VAULT__ replacement",
          IP.localize_text(canon, "/h/B", "linux") == canon.replace("__VAULT__", "/h/B"))
    d = tempfile.mkdtemp(prefix="wininstall-")
    try:
        plugin, claude, state = (os.path.join(d, n) for n in ("plugin", "claude", "state"))
        os.makedirs(os.path.join(plugin, "skills", "recall"))
        with open(os.path.join(plugin, "skills", "recall", "SKILL.md"), "w", encoding="utf-8", newline="") as fh:
            fh.write(canon + "Español: acción, ñandú\n")
        s = IP.Syncer(plugin, claude, state, vault=V, platform="win32", executable=E)
        report = s.apply(install_only=True)
        text = read(os.path.join(claude, "skills", "recall", "SKILL.md"))
        check("install writes the Windows command and keeps UTF-8 text", report[0]["action"] == "install"
              and '"%s" -X utf8' % E in text and "acción, ñandú" in text, text)
        check("the installed copy counts as the same as the canonical one",
              [i["action"] for i in s.plan()] == ["same"], s.plan())
        with open(os.path.join(claude, "skills", "recall", "SKILL.md"), "a", encoding="utf-8", newline="") as fh:
            fh.write("edited live\n")
        check("an edit in the live copy is a back-port", [i["action"] for i in s.plan()] == ["backport"])
        s.apply()
        back = read(os.path.join(plugin, "skills", "recall", "SKILL.md"))
        check("the back-port restores `/usr/bin/python3 __VAULT__/_bin/...` in the vault's copy",
              back == canon + "Español: acción, ñandú\nedited live\n", back)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    check_any_interpreter_backports()
    example = json.load(open(os.path.join(REPO, "integrations", "claude-code", "settings.example.json"), encoding="utf-8"))
    merged, changes = CS.merge({}, example, V, "win32", E)
    allow = merged["permissions"]["allow"]
    check("recommended permissions name the Windows command",
          'Bash("%s" -X utf8 "%s\\_bin\\query.py" *)' % (E, V) in allow
          and not any("python3" in a for a in allow), [a for a in allow if "python3" in a])
    check("and Read() uses `/` separators", "Read(C:/Users/rocio/Mi Brain/**)" in allow, [a for a in allow if a.startswith("Read")])
    pmerged, _ = CS.merge({}, example, "/h/B", "linux")
    check("POSIX permissions are unchanged", "Bash(python3 /h/B/_bin/query.py *)" in pmerged["permissions"]["allow"]
          and "Read(/h/B/**)" in pmerged["permissions"]["allow"])


# ---------------------------------------------------------------- MCP


def test_mcp():
    print("\n== MCP server snippets ==")
    snip = FD.mcp_snippets(V, "python3", platform="win32", executable=E)
    server = V + "\\integrations\\mcp\\server.py"
    desktop = json.loads(snip["claude-desktop"])["mcpServers"]["brain"]
    check("Claude Desktop: command is python.exe, args are -X utf8 and the server",
          desktop == {"command": E, "args": ["-X", "utf8", server]}, desktop)
    jc = json.loads(snip["json-clients"])["mcpServers"]["brain"]
    check("JSON clients also carry BRAIN_VAULT", jc["args"][:2] == ["-X", "utf8"] and jc["env"] == {"BRAIN_VAULT": V}, jc)
    oc = json.loads(snip["opencode"])["mcp"]["brain"]
    check("OpenCode: one command list", oc["command"] == [E, "-X", "utf8", server] and oc["environment"] == {"BRAIN_VAULT": V}, oc)
    check("claude mcp add line quotes both paths",
          snip["claude-code"] == 'claude mcp add brain -- "%s" -X utf8 "%s"' % (E, server), snip["claude-code"])
    posix = FD.mcp_snippets("/srv/vault", "python3", platform="linux")
    check("POSIX snippets are unchanged",
          posix["claude-code"] == "claude mcp add brain -- python3 /srv/vault/integrations/mcp/server.py"
          and json.loads(posix["claude-desktop"]) == {"mcpServers": {"brain": {"command": "python3", "args": ["/srv/vault/integrations/mcp/server.py"]}}})
    calls = []
    m = FA.McpSetup(V, "C:\\Users\\rocio", environ={}, which=lambda n: "C:\\npm\\claude.cmd",
                    run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
                    platform="win32", executable=E)
    good, _ = m.register_claude()
    check("registration with Claude Code on Windows passes python.exe -X utf8 and the server as separate arguments",
          good and calls[0] == ["C:\\npm\\claude.cmd", "mcp", "add", "brain", "--", E, "-X", "utf8", server], calls)
    check("the adapter's snippets use the same command", json.loads(m.snippets()["claude-desktop"])["mcpServers"]["brain"]["command"] == E)
    src = read(os.path.join(REPO, "integrations", "mcp", "server.py"))
    check("the MCP server starts the scripts it runs in UTF-8 mode on Windows",
          'PY_ARGV = [PY, "-X", "utf8"] if WIN else [PY]' in src and 'PYTHONUTF8' in src)
    cli = read(os.path.join(REPO, "integrations", "cli", "brain"))
    check("so does the brain command", 'PY_ARGV = [PY, "-X", "utf8"] if sys.platform == "win32" else [PY]' in cli
          and "PYTHONUTF8" in cli)


# ---------------------------------------------------------------- installers


class Stream:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def load_install():
    import importlib.util
    spec = importlib.util.spec_from_file_location("claude_install", os.path.join(REPO, "integrations", "claude-code", "install.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_install_py():
    print("\n== integrations/claude-code/install.py ==")
    mod = load_install()
    old = os.environ.get("BRAIN_VAULT")
    fake_vault = os.path.join(tempfile.gettempdir(), "fake vault")
    os.environ["BRAIN_VAULT"] = fake_vault
    try:
        for platform, tty in (("win32", False), ("win32", True), ("linux", False), ("linux", True)):
            seen = []
            code = mod.main(stdin=Stream(tty), platform=platform, call=lambda cmd, **kw: seen.append((cmd, kw)) or 0)
            scripts = [os.path.basename(c[-1] if c[-1].endswith(".py") else c[-2] if c[-2].endswith(".py") else c[-3]) for c, _ in seen]
            tail = [c[-1] for c, _ in seen][-1]
            label = "%s, %s" % (platform, "terminal" if tty else "no terminal")
            check("%s: the same steps in the same order as install.sh" % label,
                  code == 0 and [os.path.basename(next(a for a in c if a.endswith(".py"))) for c, _ in seen]
                  == ["skills_index.py", "install_plugin.py", "guardian.py", "claude_settings.py"], seen)
            check("%s: settings are %s" % (label, "merged" if tty else "only shown"), tail == ("merge" if tty else "show"), seen[-1])
            if platform == "win32":
                check("%s: every Python starts in UTF-8 mode with PYTHONUTF8 for its children" % label,
                      all(c[1:3] == ["-X", "utf8"] and kw["env"]["PYTHONUTF8"] == "1" for c, kw in seen), seen[0])
            else:
                check("%s: the interpreter is the one running install.py, no extra flags" % label,
                      all(c[0] == sys.executable and c[1].endswith(".py") for c, _ in seen))
            check("%s: BRAIN_VAULT is handed down" % label, all(kw["env"]["BRAIN_VAULT"] == os.path.abspath(fake_vault) for _, kw in seen))
        seen = []
        code = mod.main(stdin=Stream(False), platform="linux",
                        call=lambda cmd, **kw: seen.append(cmd) or (3 if "install_plugin.py" in cmd[1] else 0))
        check("a failing step stops the install and its exit code is passed on (set -e)", code == 3 and len(seen) == 2, seen)
        seen = []
        mod.main(stdin=Stream(False), platform="linux", call=lambda cmd, **kw: seen.append(cmd) or (1 if "skills_index.py" in cmd[1] else 0))
        check("the skills catalogue may fail without stopping it (`|| true`)", len(seen) == 4, seen)
    finally:
        if old is None:
            os.environ.pop("BRAIN_VAULT", None)
        else:
            os.environ["BRAIN_VAULT"] = old


def scratch_env(root):
    home, state = os.path.join(root, "home"), os.path.join(root, "state")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BRAIN_")}
    env.update(HOME=home, USERPROFILE=home, BRAIN_STATE=state, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
               APPDATA=os.path.join(home, "AppData", "Roaming"), LOCALAPPDATA=os.path.join(home, "AppData", "Local"))
    return home, env


def test_install_end_to_end():
    print("\n== install.py and bootstrap.py, for real, in a scratch HOME ==")
    root = tempfile.mkdtemp(prefix="wininstall-")
    try:
        # on a copy: skills_index.py writes the skills catalogue into the vault it runs for
        vault = os.path.join(root, "vault")
        shutil.copytree(REPO, vault, ignore=shutil.ignore_patterns(".git", "_index", "__pycache__", "*.pyc", ".venv", "venv", "node_modules", "ms-playwright"))
        home, env = scratch_env(os.path.join(root, "a"))
        env["BRAIN_VAULT"] = vault
        p = subprocess.run([sys.executable, os.path.join(vault, "integrations", "claude-code", "install.py")], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        check("install.py exits 0 without a terminal", p.returncode == 0, (p.stdout + p.stderr)[-1500:])
        check("it says it merged nothing and how to merge later", "not a terminal: nothing merged" in p.stdout, p.stdout[-600:])
        hooks = json.loads(read(os.path.join(home, ".claude", "settings.json"))).get("hooks") or {}
        cmds = commands(hooks)
        check("the hooks are in the scratch settings.json", len(cmds) >= 10, len(cmds))
        if sys.platform == "win32":
            check("on Windows each hook starts python.exe in UTF-8 mode", all(re.match(r'^".+python[w]?\.exe" -X utf8 ".+\.py"', c) for c in cmds), cmds[:2])
        else:
            check("on POSIX each hook is /usr/bin/python3 <script>, byte for byte as before",
                  all(c.startswith("/usr/bin/python3 " + os.path.join(vault, "_bin") + "/") for c in cmds), cmds[:2])
        check("the skills are installed with the vault path filled in",
              os.path.isfile(os.path.join(home, ".claude", "skills", "recall", "SKILL.md"))
              and "__VAULT__" not in read(os.path.join(home, ".claude", "skills", "recall", "SKILL.md")))

        home, env = scratch_env(os.path.join(root, "b"))
        p = subprocess.run([sys.executable, "-X", "utf8", os.path.join(vault, "bootstrap.py")], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
        out = p.stdout + p.stderr
        check("bootstrap.py exits 0 in a scratch HOME without a terminal", p.returncode == 0, out[-1500:])
        check("it checks Python and FTS5, builds the index and shows the health head",
              "OK: Python" in out and os.path.isdir(os.path.join(vault, "_index")) and "== Vault ==" in out, out[:900])
        check("the first run is offered but asks nothing (no terminal)",
              "needs a terminal" in out and not os.path.exists(os.path.join(root, "b", "state", "first-run.json")), out[-900:])
        check("it ends with the connection hints", "== Core ready ==" in out and "integrations/mcp/README.md" in out)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_bootstrap_py_text():
    print("\n== bootstrap.py, per platform ==")
    sys.path.insert(0, REPO)
    import importlib.util
    spec = importlib.util.spec_from_file_location("bootstrap_mod", os.path.join(REPO, "bootstrap.py"))
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)
    win = "\n".join(bs.optional_tools(which=lambda n: None, platform="win32", exists=lambda p: False, environ={}))
    check("Windows hints: winget for KeePassXC, Obsidian and Git",
          "winget install KeePassXCTeam.KeePassXC" in win and "winget install Obsidian.Obsidian" in win and "winget install Git.Git" in win, win)
    nix = "\n".join(bs.optional_tools(which=lambda n: None, platform="linux", exists=lambda p: False, environ={}))
    check("macOS/Linux hints are bootstrap.sh's words",
          "macOS: brew install --cask keepassxc    Linux: your distribution's keepassxc package" in nix
          and "tip: Obsidian (https://obsidian.md) gives the vault a GUI; optional" in nix
          and "git not found: the vault cannot sync between machines without it" in nix and "winget" not in nix, nix)
    found = bs.optional_tools(which=lambda n: "/x/" + n, platform="win32", exists=lambda p: False, environ={})
    check("tools that are installed print no install hint", len(found) == 1 and "keepassxc-cli found" in found[0], found)
    check("Obsidian is found in %LOCALAPPDATA%\\Programs on Windows",
          bs.has_obsidian({"LOCALAPPDATA": "C:\\L"}, lambda n: None, lambda p: p.endswith("Obsidian.exe") and "Programs" in p, "win32"))
    check("the closing text points at the PowerShell scripts on Windows",
          "install.ps1" in bs.closing("win32") and "setup.ps1" in bs.closing("win32") and "bash " not in bs.closing("win32"))
    check("and at the shell scripts elsewhere, as bootstrap.sh does",
          "bash integrations/claude-code/install.sh" in bs.closing("linux") and "python3 _bin/run_all_tests.py" in bs.closing("linux"))
    sh = read(os.path.join(REPO, "bootstrap.sh"))
    check("bootstrap.sh is still there and still the same script", "keepassxc-cli" in sh and "first-run/setup.sh" in sh)


# ---------------------------------------------------------------- wrappers


def balanced(text):
    stripped = re.sub(r"'[^']*'|\"[^\"]*\"|#.*", "", text)
    return all(stripped.count(a) == stripped.count(b) for a, b in ("{}", "()", "[]"))


def test_wrappers():
    print("\n== PowerShell and cmd wrappers ==")
    files = {
        "bootstrap.ps1": os.path.join(REPO, "bootstrap.ps1"),
        "install.ps1": os.path.join(REPO, "integrations", "claude-code", "install.ps1"),
        "setup.ps1": os.path.join(REPO, "integrations", "first-run", "setup.ps1"),
        "brain.ps1": os.path.join(REPO, "integrations", "cli", "brain.ps1"),
        "find-python.ps1": os.path.join(REPO, "_bin", "find-python.ps1"),
        "brain.cmd": os.path.join(REPO, "integrations", "cli", "brain.cmd"),
    }
    check("every wrapper exists", all(os.path.isfile(p) for p in files.values()), [k for k, p in files.items() if not os.path.isfile(p)])
    text = {k: read(p) for k, p in files.items() if os.path.isfile(p)}
    check("they are plain ASCII (Windows PowerShell 5.1 reads a file without a BOM as ANSI)",
          all(t.isascii() for t in text.values()), [k for k, t in text.items() if not t.isascii()])
    check("the PowerShell scripts have balanced braces, parentheses and brackets", all(balanced(t) for k, t in text.items() if k.endswith(".ps1")),
          [k for k, t in text.items() if k.endswith(".ps1") and not balanced(t)])
    fp = text["find-python.ps1"]
    check("the finder tries py -3, then python, then python3, and says how to install Python",
          fp.index("'py'") < fp.index("'python'") < fp.index("'python3'") and "'-3'" in fp
          and "winget install Python.Python.3.12" in fp and "BRAIN_PYTHON" in fp and "exit 9" in fp)
    check("it checks for 3.9 and skips Store stubs by running the interpreter", "(3, 9)" in fp and "sys.executable" in fp)
    b = text["bootstrap.ps1"]
    check("bootstrap.ps1: finds Python, sets BRAIN_VAULT to its own folder, runs bootstrap.py with -X utf8 and passes arguments on",
          "Find-BrainPython" in b and "$env:BRAIN_VAULT = $PSScriptRoot" in b and "-X utf8" in b and "bootstrap.py" in b
          and "@args" in b and "exit $LASTEXITCODE" in b, b)
    i = text["install.ps1"]
    check("install.ps1 runs install.py in UTF-8 mode with the vault set", "-X utf8" in i and "install.py" in i
          and "$env:BRAIN_VAULT" in i and "Find-BrainPython" in i and "exit $LASTEXITCODE" in i)
    s = text["setup.ps1"]
    check("setup.ps1 checks for a terminal first, exits 0 and names skip-all, then runs first_run.py run in UTF-8 mode",
          s.index("IsInputRedirected") < s.index("first_run.py'") and ") run @args" in s and "exit 0" in s and "skip-all" in s and "-X utf8" in s
          and "@args" in s, s)
    c = text["brain.cmd"]
    check("brain.cmd: CRLF line endings, finds python like the ps1 does, runs the script next to it in UTF-8 mode",
          "\r\n" in c and "\n" not in c.replace("\r\n", "") and 'py -3' in c and "python -c" in c and 'python3 -c' in c
          and '-X utf8 "%~dp0brain" %*' in c and "exit /b %errorlevel%" in c and "winget install Python.Python.3.12" in c, repr(c[:300]))
    check("brain.cmd goes to :run once a Python is found",
          c.count("goto run") == 3 and c.index(":run") > c.rindex("goto run"))
    bp = text["brain.ps1"]
    check("brain.ps1 runs the brain script next to it in UTF-8 mode", "-X utf8" in bp and "'brain'" in bp and "@args" in bp and "Find-BrainPython" in bp)
    attrs = read(os.path.join(REPO, ".gitattributes"))
    check(".gitattributes keeps .cmd files CRLF and shell scripts LF", "*.cmd text eol=crlf" in attrs and "*.sh text eol=lf" in attrs)
    sh = [os.path.join(REPO, p) for p in ("bootstrap.sh", "integrations/claude-code/install.sh", "integrations/first-run/setup.sh")]
    check("the .sh entry points are still there", all(os.path.isfile(p) for p in sh))
    check("setup.sh still exits 0 without a terminal", '[ ! -t 0 ]' in read(sh[2]) and "exit 0" in read(sh[2]))
    readme = read(os.path.join(REPO, "integrations", "cli", "README.md"))
    check("the CLI README says how to put the folder on PATH on Windows", "SetEnvironmentVariable" in readme and "brain.cmd" in readme)
    top = read(os.path.join(REPO, "README.md"))
    check("the README has a Windows section with the PowerShell quick start",
          "## Windows" in top and "bootstrap.ps1" in top and "install.ps1" in top and "Task Scheduler" in top and "UTF-8" in top)


def test_fake_launcher():
    import testbin
    sh = os.path.join("G", "Git", "bin", "sh.exe")
    usr = os.path.join("G", "Git", "usr", "bin")
    text = testbin._launcher_text(sh, os.path.join("t", "slow"), isdir=lambda p: p == usr)
    check("a fake's .cmd puts Git's usr/bin on PATH so sleep and cat are found", usr in text and "%PATH%" in text, text)
    check("and keeps that PATH to the one call (setlocal)", text.startswith("@setlocal"), text)
    py = testbin._launcher_text(sys.executable, "x.py", isdir=lambda p: True)
    check("a Python fake's .cmd touches no PATH", "PATH" not in py, py)


def main():
    test_fake_launcher()
    test_pycmd()
    test_canonical_interpreter()
    test_hooks()
    test_skills_and_settings()
    test_mcp()
    test_install_py()
    test_bootstrap_py_text()
    test_wrappers()
    test_install_end_to_end()
    print("\nRESULT: %d passed, %d failed" % (len(ok), len(fail)))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
