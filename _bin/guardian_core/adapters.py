"""The guardian's adapters: every place it actually touches the machine.

Each class implements one port from ports.py. Anything external (subprocess, launchctl,
osascript) is injectable so the tests can put a fake in its place; the defaults are the
real thing.

Nothing here decides anything. What is a problem, what to merge, when to speak up —
that is domain.py; these only read and write.
"""

from __future__ import annotations

import datetime as dt
import json
import ntpath
import os
import plistlib
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager

from . import domain as D

try:
    import oslock                       # lives in _bin/, the parent of this package
except ImportError:                     # pragma: no cover - _bin not on sys.path
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import oslock
import osproc                           # next to oslock: uid, process groups, command splitting
import pycmd                            # the one python.exe Windows hooks name

import sys as _sys_platform
IS_WINDOWS = _sys_platform.platform == "win32"
# git by absolute path where it has one (launchd starts jobs with a bare PATH); from PATH on Windows.
GIT = "git" if IS_WINDOWS else "/usr/bin/git"

HOME = os.path.expanduser("~")
CLT = "/Library/Developer/CommandLineTools"


def default_state_dir() -> str:
    """Brain's own state directory. Resolved by brain_paths.state_dir(), nowhere else.

    Deliberately not under ~/.claude: the guardian's state, queue and raised alerts must
    survive any agent being uninstalled, reset or swapped. (Older scripts still log under
    ~/.claude/state/brain; they are read where needed, never written by the guardian.)
    """
    import brain_paths

    return brain_paths.state_dir()


def legacy_state_dir():
    """Where the pre-guardian scripts (linkfix, tasks) keep their state: brainlib.STATE."""
    try:
        import brainlib as B

        return B.STATE
    except Exception:
        return None


# ---------------------------------------------------------------- file helpers


def atomic_write(path: str, text: str, mode=None) -> None:
    """Write next to the target and rename over it: a reader never sees half a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@contextmanager
def locked(path: str):
    """An exclusive advisory lock on `<path>.lock`, for files two processes write."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "a") as fh:
        oslock.lock(fh)
        try:
            yield
        finally:
            oslock.unlock(fh)


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def plain_run(cmd, cwd=None, timeout=10):
    """(rc, stdout, stderr), stripped. Never raises."""
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return 1, "", "%s: %s" % (type(exc).__name__, exc)


def _brainlib_run():
    """brainlib.run, which logs slow subprocesses the way the rest of _bin does."""
    try:
        import brainlib as B

        return lambda cmd, cwd=None, timeout=10: B.run(cmd, cwd=cwd, timeout=timeout)
    except Exception:
        return plain_run


# ---------------------------------------------------------------- launchd


class LaunchctlControl:
    """The launchd jobs the vault defines: every `_bin/*.plist` is one.

    `allowed` narrows them to the labels the user accepted at first run; None means every
    template (tests, and a person running the adapter by hand)."""

    def __init__(self, vault, home=HOME, uid=None, agents_dir=None, launchctl="/bin/launchctl",
                 run=subprocess.run, timeout=15, backup_dir=None, environ=None, clock=None, allowed=None):
        self.vault, self.home = vault, home
        self.allowed = None if allowed is None else set(allowed)
        self.uid = osproc.current_uid() if uid is None else uid    # None on Windows
        self.agents_dir = agents_dir or os.path.join(home, "Library", "LaunchAgents")
        self.launchctl, self.run, self.timeout = launchctl, run, timeout
        self.backup_dir = backup_dir
        self.environ = os.environ if environ is None else environ
        self.clock = clock or SystemClock()

    def _call(self, *args):
        try:
            p = self.run([self.launchctl] + list(args), capture_output=True, text=True,
                         timeout=self.timeout, stdin=subprocess.DEVNULL)
            return p.returncode, p.stdout or "", p.stderr or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", "%s: %s" % (type(exc).__name__, exc)

    def _template(self, label):
        return os.path.join(self.vault, "_bin", label + ".plist")

    def _installed(self, label):
        return os.path.join(self.agents_dir, label + ".plist")

    def labels(self) -> list:
        folder = os.path.join(self.vault, "_bin")
        try:
            names = sorted(f[:-len(".plist")] for f in os.listdir(folder) if f.endswith(".plist"))
        except OSError:
            return []
        return [n for n in names if self.allowed is None or n in self.allowed]

    def installed(self, label) -> bool:
        return os.path.exists(self._installed(label))

    def is_loaded(self, label) -> bool:
        return self._call("print", "gui/%s/%s" % (self.uid, label))[0] == 0

    def last_exit_ok(self, label):
        rc, out, _ = self._call("list", label)
        if rc != 0:
            return False, "not listed by launchctl (exit %d)" % rc
        m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)\s*;', out)
        if not m:
            return True, "never exited"
        status = int(m.group(1))
        return status == 0, "LastExitStatus=%d" % status

    def render(self, label) -> str:
        """The vault's plist with the original machine's paths rewritten, like bootstrap.sh."""
        with open(self._template(label), encoding="utf-8") as fh:
            text = fh.read()
        return text.replace(D.ORIGIN_VAULT, self.vault).replace(D.ORIGIN_HOME, self.home)

    def install(self, label):
        try:
            text = self.render(label)
            data = plistlib.loads(text.encode("utf-8"))
            for key in ("StandardOutPath", "StandardErrorPath"):
                if data.get(key):
                    os.makedirs(os.path.dirname(data[key]), exist_ok=True)
            atomic_write(self._installed(label), text)
            return True, self._installed(label)
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)

    def bootstrap(self, label):
        rc, out, err = self._call("bootstrap", "gui/%s" % self.uid, self._installed(label))
        return rc == 0, (err or out).strip()

    def drifted(self, label) -> bool:
        """The installed plist is not what the vault's template renders to on this machine."""
        try:
            with open(self._installed(label), encoding="utf-8") as fh:
                return fh.read() != self.render(label)
        except OSError:
            return False

    def self_label(self) -> str:
        """The launchd job this process runs as (launchd sets XPC_SERVICE_NAME), or ""."""
        name = self.environ.get("XPC_SERVICE_NAME") or ""
        return name if name.startswith("com.") else ""

    def reinstall(self, label):
        """Back the drifted plist up, rewrite it from the template and make launchd reread it.

        The backup goes to the state directory, not next to the plist: launchd reads
        LaunchAgents at login, and a stray copy there is one more thing it might load.
        """
        try:
            backup_dir = self.backup_dir or os.path.join(default_state_dir(), "plist-backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = self.clock.now().strftime("%Y%m%d-%H%M%S")
            backup = os.path.join(backup_dir, "%s.plist.%s" % (label, stamp))
            shutil.copy2(self._installed(label), backup)
        except Exception as exc:
            return False, "backup failed, plist left as it was: %s: %s" % (type(exc).__name__, exc)
        note = "previous plist in %s" % backup          # the repair alert lists it
        was_loaded = self.is_loaded(label)
        done, detail = self.install(label)
        if not done:
            return False, "%s; %s" % (detail, note)
        if was_loaded:
            self._call("bootout", "gui/%s/%s" % (self.uid, label))
            done, detail = self.bootstrap(label)
            return done, "; ".join(x for x in (note, detail) if x)
        return True, "rewritten (not loaded); " + note


# ---------------------------------------------------------------- small adapters


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now()


def _as_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class OsascriptNotifier:
    """A macOS notification. Best effort: no GUI session, no notification, no error."""

    def __init__(self, run=subprocess.run, osascript="/usr/bin/osascript", timeout=10):
        self.run, self.osascript, self.timeout = run, osascript, timeout

    def notify(self, title: str, message: str) -> None:
        script = "display notification %s with title %s" % (_as_quote(message), _as_quote(title))
        try:
            self.run([self.osascript, "-e", script], capture_output=True, text=True,
                     timeout=self.timeout, stdin=subprocess.DEVNULL)
        except Exception:
            pass


class LocalPaths:
    def exists(self, path: str) -> bool:
        return os.path.exists(path)


class JsonStateStore:
    def __init__(self, path):
        self.path = path

    def load(self) -> dict:
        return read_json(self.path, {})

    def save(self, data: dict) -> None:
        atomic_write(self.path, json.dumps(data, indent=2, sort_keys=True))


# ---------------------------------------------------------------- raised alerts


def raised_path() -> str:
    return os.path.join(default_state_dir(), "guardian-raised.json")


class RaisedAlertsFile:
    """Alerts other processes raise for the guardian to carry (the task runner's failures)."""

    def __init__(self, path=None):
        self.path = path or raised_path()

    def load(self) -> dict:
        return read_json(self.path, {})


def raise_alert(key: str, summary: str, severity: str = D.FAIL, path=None) -> None:
    """Record a problem the next guardian run will report. Replaces the same key."""
    path = path or raised_path()
    with locked(path):
        data = read_json(path, {})
        data[key] = {"severity": severity, "summary": summary,
                     "at": dt.datetime.now().isoformat(timespec="seconds")}
        atomic_write(path, json.dumps(data, indent=2, sort_keys=True))


def clear_alert(key: str, path=None) -> None:
    """The problem behind `key` is gone (the routine succeeded). No-op when never raised."""
    path = path or raised_path()
    if key not in read_json(path, {}):
        return
    with locked(path):
        data = read_json(path, {})
        if data.pop(key, None) is not None:
            atomic_write(path, json.dumps(data, indent=2, sort_keys=True))


# ---------------------------------------------------------------- probes


class VaultDoctorProbe:
    """Sync, index and link health, read the way doctor.py reads them."""

    def __init__(self, vault, state_dir=None, git=GIT, run=None, now=time.time):
        self.vault = vault
        self.state_dir = state_dir or legacy_state_dir()     # linkfix.json lives there
        self.git, self.now = git, now
        self.run = run or _brainlib_run()

    def sync_status(self):
        rc, out, err = self.run([self.git, "status", "--porcelain"], cwd=self.vault)
        if rc != 0:
            raise RuntimeError("git status failed in %s: %s" % (self.vault, err or rc))
        pending = len([l for l in out.splitlines() if l.strip()])
        rc_u, _, _ = self.run([self.git, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                              cwd=self.vault)
        remote_ok = rc_u == 0
        unpushed = None
        if remote_ok:
            rc, out, _ = self.run([self.git, "log", "@{u}..HEAD", "--format=%ct"], cwd=self.vault)
            stamps = [int(x) for x in out.split() if x.isdigit()]
            if rc == 0 and stamps:
                unpushed = max(0.0, self.now() - min(stamps))
        return D.SyncStatus(pending=pending, unpushed_age_s=unpushed, remote_ok=remote_ok)

    def index_age_s(self):
        try:
            return max(0.0, self.now() - os.path.getmtime(os.path.join(self.vault, "_index", "vault.db")))
        except OSError:
            return None

    def broken_links(self) -> int:
        if not self.state_dir:
            return 0
        return len(read_json(os.path.join(self.state_dir, "linkfix.json"), {}).get("broken") or [])


def _git_hook_names() -> tuple:
    """The git hooks Brain generates: events_core's list, the one brain_watch.py writes from."""
    from events_core import domain as ED

    return tuple(ED.GIT_HOOKS)


class GitHooksControl:
    """core.hooksPath and the githooks/ files of the vault's repository.

    One key, in one file: the main repository's config, found with
    `git rev-parse --git-common-dir` and then read and written with `git config --file`.
    From a linked worktree of the vault that is the same shared .git/config, which is the
    point: the hooks are the vault's, for every checkout. `--file` is what keeps
    config.worktree, the global and the system config out of reach, even when
    extensions.worktreeConfig is on.
    """

    def __init__(self, vault, git=GIT, run=plain_run, names=None):
        self.vault, self.git, self.run = vault, git, run
        self.names = tuple(names) if names else _git_hook_names()

    def _config_file(self) -> str:
        rc, out, err = self.run([self.git, "-C", self.vault, "rev-parse", "--git-common-dir"])
        if rc != 0 or not out:
            raise RuntimeError("not a git repository: %s (%s)" % (self.vault, err or "exit %d" % rc))
        return os.path.join(os.path.normpath(os.path.join(self.vault, out)), "config")

    def _hook(self, name) -> str:
        return os.path.join(self.vault, D.GIT_HOOKS_DIR, name)

    def status(self):
        config = self._config_file()
        rc, out, err = self.run([self.git, "config", "--file", config, "--get", "core.hooksPath"])
        if rc not in (0, 1):                                  # 1: the key is not set
            raise RuntimeError("git config --get core.hooksPath failed on %s: %s" % (config, err or rc))
        files = []
        for name in self.names:
            path = self._hook(name)
            exists = os.path.isfile(path)
            files.append(D.GitHookFile(name, exists, exists and os.access(path, os.X_OK)))
        return D.GitHooksStatus(out if rc == 0 else None, files)

    def set_hooks_path(self):
        try:
            config = self._config_file()
        except RuntimeError as exc:
            return False, str(exc)
        rc, _, err = self.run([self.git, "config", "--file", config, "core.hooksPath", D.GIT_HOOKS_DIR])
        if rc != 0:
            return False, err or "git config exit %d" % rc
        return True, "core.hooksPath = %s in %s" % (D.GIT_HOOKS_DIR, config)

    def make_executable(self, name):
        """chmod +x: an execute bit wherever there is a read bit. Only Brain's own hook files."""
        if name not in self.names:
            return False, "%s is not one of Brain's git hooks" % name
        path = self._hook(name)
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            new = mode | ((mode & 0o444) >> 2)
            os.chmod(path, new)
        except OSError as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        return True, "%s mode %o" % (path, new)


class TokenPoolProbe:
    """The agent routines' token pool: 90-Meta/routine-tokens.json plus the state file
    routine_auth_core writes after every attempt. Reads both, writes neither, never reads
    KeePass. The pool rules (parsing, expiry, the renewal commands) are routine_auth_core's;
    this is where the two hexagons meet.
    """

    def __init__(self, pool_path, state_path):
        self.pool_path, self.state_path = pool_path, state_path

    def pool(self):
        from routine_auth_core import domain as RA

        if not os.path.exists(self.pool_path):
            return D.TokenPool([])
        try:
            with open(self.pool_path, encoding="utf-8") as fh:
                entries = RA.parse_pool(fh.read())
        except OSError as exc:
            return D.TokenPool([], "unreadable: %s" % type(exc).__name__)
        except RA.PoolConfigError as exc:
            return D.TokenPool([], str(exc))
        state = read_json(self.state_path, {})
        tokens = []
        for e in entries:
            s = state.get(e.label) if isinstance(state.get(e.label), dict) else {}
            tokens.append(D.TokenHealth(
                label=e.label, account=e.account, kp_ref=e.kp_ref, status=s.get("status") or "healthy",
                until=s.get("until"), last_kind=s.get("last_kind"), last_at=s.get("last_at"),
                detail=s.get("detail") or "", expires=RA.expires_on(e.issued).isoformat(),
                renew=RA.renew_command(e.kp_ref), restore=RA.restore_command(e.kp_ref)))
        return D.TokenPool(tokens)


class DesktopScheduledTasksProbe:
    """Enabled Claude app scheduled tasks, per account: every
    `<sessions dir>/<account>/<session>/scheduled-tasks.json` `scheduledTasks[]` entry with
    `enabled: true`. Reads only; a file that does not parse is skipped."""

    def __init__(self, sessions_dir):
        self.sessions_dir = sessions_dir

    def enabled(self) -> list:
        out = []
        try:
            accounts = sorted(os.listdir(self.sessions_dir))
        except OSError:
            return out
        for account in accounts:
            account_dir = os.path.join(self.sessions_dir, account)
            if not os.path.isdir(account_dir):
                continue
            try:
                sessions = sorted(os.listdir(account_dir))
            except OSError:
                continue
            for session in sessions:
                tasks = read_json(os.path.join(account_dir, session, "scheduled-tasks.json"), {}).get("scheduledTasks")
                if not isinstance(tasks, list):
                    continue
                for t in tasks:
                    if isinstance(t, dict) and t.get("enabled") is True and isinstance(t.get("id"), str):
                        item = (t["id"], account)
                        if item not in out:
                            out.append(item)
        return out


class InterpreterHealthProbe:
    """Does python3 run? The hooks' interpreter first, judged the way hooks run it."""

    DEFAULT_FALLBACKS = ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3")

    def __init__(self, hook_python="/usr/bin/python3", fallbacks=DEFAULT_FALLBACKS, clt=CLT,
                 run=subprocess.run, timeout=10):
        self.hook_python, self.fallbacks = hook_python, list(fallbacks)
        self.clt, self.run, self.timeout = clt, run, timeout

    def _probe(self, path, extra_env=None):
        if not os.path.exists(path):
            return False, "missing"
        env = dict(os.environ)
        env.pop("DEVELOPER_DIR", None)       # hooks and launchd do not carry the caller's
        env.update(extra_env or {})
        try:
            p = self.run([path, "-c", "import sqlite3"], env=env, capture_output=True, text=True,
                         timeout=self.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "timed out"
        except OSError as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        if p.returncode == 0:
            return True, "ok"
        last = (p.stderr or "").strip().splitlines()
        return False, "exit %d%s" % (p.returncode, (": " + last[-1][:120]) if last else "")

    @classmethod
    def for_platform(cls, platform=None, executable=None):
        """The defaults for this platform. Windows has no /usr/bin/python3 and no Xcode gate: the hooks
        run the Python the vault was set up with, so that is the one judged, with no fallbacks."""
        if (platform or _sys_platform.platform) == "win32":
            return cls(hook_python=pycmd.windows_python(executable), fallbacks=())
        return cls()

    def python3_health(self) -> list:
        out = [D.InterpreterStatus(self.hook_python, *self._probe(self.hook_python))]
        for c in self.fallbacks:
            if os.path.isdir(self.clt) and os.path.exists(c):
                out.append(D.InterpreterStatus("%s (DEVELOPER_DIR=%s)" % (c, self.clt),
                                               *self._probe(c, {"DEVELOPER_DIR": self.clt})))
            out.append(D.InterpreterStatus(c, *self._probe(c)))
        return out


# ---------------------------------------------------------------- routines and agents


def strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + len("\n---\n"):]
    return text


def load_agent_command(vault: str, environ=None) -> str:
    """The agent command template: BRAIN_AGENT_CMD, else 90-Meta/agent-command.txt.

    Configuration, never code: switching to another CLI agent, account or model is
    editing that one line. Blank lines and `#` comments are skipped.
    """
    environ = os.environ if environ is None else environ
    value = (environ.get("BRAIN_AGENT_CMD") or "").strip()
    if value:
        return value
    try:
        with open(os.path.join(vault, "90-Meta", "agent-command.txt"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return ""


def _split(template) -> list:
    """The template's words. A list is taken as already split (routine_auth_core passes one, so no
    word is quoted and split again)."""
    if isinstance(template, (list, tuple)):
        return [str(t) for t in template]
    try:
        return osproc.split_command(template or "")
    except ValueError:
        return []


class CliAgentRunner:
    """Runs a routine through whatever CLI agent the template names.

    `{prompt}` becomes the routine's body (frontmatter stripped) as one argument,
    `{prompt_file}` its path. The agent runs in its own process group so a timeout kills
    everything it started, not just the top process.
    """

    def __init__(self, template, cwd=None, env=None, popen=subprocess.Popen, which=shutil.which,
                 platform=None, environ=None, isfile=os.path.isfile, read=None):
        self.template, self.cwd, self.env = template, cwd, env
        self.popen, self.which = popen, which
        self.platform = platform or _sys_platform.platform
        self.environ, self.isfile, self.read = environ, isfile, read      # Windows probes, injectable

    def _windows_argv(self, argv):
        """Windows: a `.cmd` npm shim cuts its command line at the first newline, so the prompt would
        arrive as its first line. Run node on the script it launches instead (osproc.unwrap_npm_shim)."""
        exe = argv[0]
        if not (os.path.dirname(exe) or ntpath.dirname(exe)):      # a bare name: the file PATH finds for it
            found = self.which(exe, path=(self.env or os.environ).get("PATH"))
            exe = found or exe
        else:
            exe = osproc.resolve_exe(exe, platform="win32", environ=self.environ, isfile=self.isfile)
        unwrapped = osproc.unwrap_npm_shim(exe, platform="win32", read=self.read, isfile=self.isfile,
                                           which=self.which)
        return unwrapped + argv[1:] if unwrapped else [exe] + argv[1:]

    def available(self) -> bool:
        toks = _split(self.template)
        if not toks:
            return False
        if os.path.dirname(toks[0]):                     # a path, not a name to look up on PATH
            exe = osproc.resolve_exe(toks[0])            # Windows: claude means claude.exe / .cmd
            return os.path.isfile(exe) and os.access(exe, os.X_OK)
        return self.which(toks[0], path=(self.env or os.environ).get("PATH")) is not None

    def run(self, prompt_path: str, timeout: int):
        toks = _split(self.template)
        if not toks:
            return 127, "", "no agent command configured (90-Meta/agent-command.txt or BRAIN_AGENT_CMD)"
        try:
            with open(prompt_path, encoding="utf-8") as fh:
                body = strip_frontmatter(fh.read()).strip()
        except OSError as exc:
            return 2, "", "routine file unreadable: %s" % exc
        argv = [t.replace("{prompt_file}", prompt_path).replace("{prompt}", body) for t in toks]
        if self.platform == "win32":
            argv = self._windows_argv(argv)
            problem = osproc.batch_argument_problem(argv, platform="win32")
            if problem:                    # cmd.exe would execute `&`, `|`, %VAR% in the routine text
                return 126, "", problem
        elif os.path.dirname(argv[0]):
            argv[0] = osproc.resolve_exe(argv[0])
        try:
            proc = self.popen(argv, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              **osproc.new_group_kwargs())
        except FileNotFoundError:
            return 127, "", "agent command not found: %s" % toks[0]
        except PermissionError:
            return 126, "", "agent command not executable: %s" % toks[0]
        except OSError as exc:
            return 1, "", "%s: %s" % (type(exc).__name__, exc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            osproc.kill_tree(proc)          # its process group on POSIX, taskkill /T on Windows
            out, err = proc.communicate()
            return 124, out or "", (err or "") + "agent timed out after %ss" % timeout
        return proc.returncode, out or "", err or ""


def _routine_permission_problem(text: str):
    """routine_auth_core's verdict on a routine's agent_args: where the two hexagons meet."""
    from routine_auth_core import domain as RA

    args, _ = RA.parse_agent_args(text)
    return RA.permission_problem(args)


class TasksRegistrySource:
    """The routines as tasks.py sees them: its registry parser and its local state."""

    def __init__(self, tasks_module=None):
        self._tasks = tasks_module

    def _t(self):
        if self._tasks is None:
            import tasks

            self._tasks = tasks
        return self._tasks

    def host(self) -> str:
        return self._t().host()

    def is_mine(self, machine: str) -> bool:
        return self._t().machine_is_mine(machine)

    def routines(self) -> list:
        return self._t().read_registry()

    def last_runs(self) -> dict:
        return self._t().load_state()

    def meta(self, row) -> dict:
        path = row.get("command") or ""
        if not os.path.isabs(path):
            path = os.path.join(str(self._t().VAULT), path)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        meta = D.routine_meta(text)
        meta["permission_problem"] = _routine_permission_problem(text)
        return meta


# ---------------------------------------------------------------- hook liveness


class HookLivenessSource:
    """Evidence that Brain's hooks fire, gathered with no agent in the loop.

    - Claude Code writes one transcript per session, `<projects>/<project dir>/<session uuid>.jsonl`.
      Subagent transcripts sit one directory deeper and are not sessions.
    - Every Brain hook appends one line per run to `<brain state>/logs/heartbeat.jsonl`
      (brainlib.heartbeat), rotated to `.1` ... `.5`.
    - The event registry says which hook each event is and how often it should fire.
    - The epoch file records when liveness started, so no session from before it is judged.
    - An optional JSON config (`window_min`, `silent_days`, `ignore_dirs`) changes the windows.
    """

    def __init__(self, projects_dir, heartbeat_log, registry_path, epoch_path, config_path=None, keep=5):
        self.projects_dir, self.heartbeat_log = projects_dir, heartbeat_log
        self.registry_path, self.epoch_path, self.config_path = registry_path, epoch_path, config_path
        self.keep = keep

    def sessions(self, since):
        out = []
        try:
            dirs = sorted(os.listdir(self.projects_dir))
        except OSError:
            return out
        for d in dirs:
            folder = os.path.join(self.projects_dir, d)
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                try:
                    st = os.stat(os.path.join(folder, name))
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_mtime < since:
                    continue
                # Linux has no st_birthtime, so a long session touched now (Claude Code appends
                # metadata when its process exits, with no turn and no hook) would look born now
                # and have its earlier heartbeats cut off: its first record's timestamp says better.
                first, cancelled = self._head(os.path.join(folder, name))
                started = min(getattr(st, "st_birthtime", None) or st.st_mtime, first or st.st_mtime)
                out.append(D.SessionTranscript(D.session_sid(name[:-len(".jsonl")]), d,
                                               min(started, st.st_mtime), st.st_mtime, cancelled))
        return out

    HEAD_BYTES = 64 * 1024

    @classmethod
    def _head(cls, path):
        """(first record's timestamp or None, hook events Claude Code cancelled) from a transcript.

        Only the head is read: both are written before the first turn, and a transcript can run
        to megabytes. Lines that cannot mention a cancellation are parsed only until a timestamp
        turns up.
        """
        first, out = None, set()
        try:
            with open(path, "rb") as fh:
                head = fh.read(cls.HEAD_BYTES)
        except OSError:
            return None, frozenset()
        for line in head.splitlines():
            if first is None and b'"timestamp"' in line:
                try:
                    ts = json.loads(line).get("timestamp")
                    first = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError, TypeError):
                    pass
            if b"hook_cancelled" not in line:
                continue
            try:
                att = json.loads(line).get("attachment") or {}
            except (ValueError, AttributeError):
                continue
            if isinstance(att, dict) and att.get("type") == "hook_cancelled":
                event = att.get("hookEvent") or str(att.get("hookName") or "").split(":")[0]
                if event:
                    out.add(str(event))
        return first, frozenset(out)

    def heartbeats(self, since):
        out = []
        paths = [self.heartbeat_log] + ["%s.%d" % (self.heartbeat_log, i) for i in range(1, self.keep + 1)]
        for path in paths:
            try:
                if os.path.getmtime(path) < since:
                    continue          # last written before the horizon: every line in it is older
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            h = D.heartbeat_from_record(json.loads(line))
                        except ValueError:
                            continue
                        if h is not None and h.ts >= since:
                            out.append(h)
            except OSError:
                continue
        out.sort(key=lambda h: h.ts)
        return out

    def events(self):
        from events_core import domain as ED     # the registry's own parser: the two hexagons meet in adapters

        with open(self.registry_path, encoding="utf-8") as fh:
            registry = ED.load_registry(fh.read())
        return [D.HookEventSpec(e.id, t.spec["event"], D.hook_identity(t.spec["command"]) or t.spec["command"],
                                getattr(e, "liveness", "") or D.LIVENESS_REGULAR)
                for e, t in registry.triggers("claude-hook")]

    def epoch(self):
        v = read_json(self.epoch_path, {}).get("since")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def start_epoch(self, ts):
        with locked(self.epoch_path):
            current = self.epoch()
            if current is not None:
                return current
            atomic_write(self.epoch_path, json.dumps({"since": ts}))
            return ts

    def config(self):
        import dataclasses

        raw = read_json(self.config_path, {}) if self.config_path else {}
        base = D.LivenessConfig()

        def number(key, scale, default):
            v = raw.get(key)
            return float(v) * scale if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default

        extra = tuple(p for p in (raw.get("ignore_dirs") or []) if isinstance(p, str))
        return dataclasses.replace(base, window_s=number("window_min", 60, base.window_s),
                                   silent_s=number("silent_days", 86400, base.silent_s),
                                   ignore_dirs=base.ignore_dirs + extra)


class HookProbe:
    """Every canonical Brain hook, run the way Claude Code runs it, with no Claude at all.

    Same interpreter and script path as hooks.json, Claude Code's stdin for the event, the
    hook's timeout. Everything a hook writes lands in a scratch directory made for the run and
    removed after it: HOME, BRAIN_STATE, BRAIN_VAULT and TMPDIR all point inside it, BRAIN_OFFLINE
    stops presence, lease, pull, reindex and link-repair processes, git cannot discover a
    repository above it, and no bytecode is written beside the real scripts. The environment is
    built from scratch, so neither DEVELOPER_DIR nor the caller's Brain variables leak in. Runs
    once per process: check, repair and status share the results.
    """

    KEEP_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "SHELL")
    # Windows: a process started without SYSTEMROOT cannot even initialise Python's random.
    KEEP_ENV_WINDOWS = ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "USERNAME", "USERDOMAIN")

    def __init__(self, canonical, events, run=subprocess.run, scratch_root=None, environ=None):
        self.canonical, self.events = canonical, events
        self.run, self.scratch_root = run, scratch_root
        self.environ = os.environ if environ is None else environ
        self.ran = False
        self._results = []

    def _env(self, root):
        env = {k: self.environ[k] for k in self.KEEP_ENV if self.environ.get(k)}
        env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        env.update({"HOME": os.path.join(root, "home"), "TMPDIR": root,
                    "BRAIN_STATE": os.path.join(root, "state"), "BRAIN_VAULT": os.path.join(root, "vault"),
                    "BRAIN_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                    "GIT_CEILING_DIRECTORIES": os.pathsep.join((root, os.path.realpath(root))),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        if IS_WINDOWS:
            env.update({k: self.environ[k] for k in self.KEEP_ENV_WINDOWS if self.environ.get(k)})
            home = os.path.join(root, "home")
            env.update({"USERPROFILE": home, "APPDATA": os.path.join(home, "AppData", "Roaming"),
                        "LOCALAPPDATA": os.path.join(home, "AppData", "Local"), "TEMP": root, "TMP": root,
                        "PYTHONUTF8": "1"})
        return env

    def _one(self, case, env, cwd):
        try:
            argv = osproc.split_command(case.command)
        except ValueError as exc:
            return D.ProbeResult(case.event_id, None, error="command does not parse: %s" % exc)
        try:
            p = self.run(argv, input=json.dumps(case.stdin), env=env, cwd=cwd, capture_output=True, text=True,
                         timeout=case.timeout)
        except subprocess.TimeoutExpired:
            return D.ProbeResult(case.event_id, None, timed_out=True)
        except OSError as exc:
            return D.ProbeResult(case.event_id, None, error="%s: %s" % (type(exc).__name__, exc))
        stdout, stderr = p.stdout or "", p.stderr or ""
        parsed, parse_error = None, ""
        if stdout.lstrip().startswith("{"):
            try:
                parsed = json.loads(stdout)
            except ValueError as exc:
                parse_error = str(exc)
        return D.ProbeResult(case.event_id, p.returncode, stdout[-4000:], stderr[-4000:],
                             parsed=parsed, parse_error=parse_error)

    def results(self):
        if self.ran:
            return list(self._results)
        root = tempfile.mkdtemp(prefix="brain-hook-probe-", dir=self.scratch_root)
        try:
            for sub in ("home", "state", "work", os.path.join("vault", "_index")):
                os.makedirs(os.path.join(root, sub), exist_ok=True)
            env, cwd = self._env(root), os.path.join(root, "work")
            results = [(case, self._one(case, env, cwd))
                       for case in D.probe_cases(self.canonical.load(), self.events.events(), cwd)]
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self._results, self.ran = results, True
        return list(results)


# ---------------------------------------------------------------- scheduled jobs beyond launchd


class SystemdUserControl:
    """The scheduled jobs the vault defines, as systemd user units (Linux).

    Every `_bin/systemd/<label>.timer` with its `<label>.service` is one job, and so is a
    `<label>.service` with no timer: a long-lived server kept up by `Restart=always` (the
    Remote Control server). Same port as LaunchctlControl: install writes the units into
    ~/.config/systemd/user and reloads the user manager, bootstrap enables and starts the
    timer (or the service, when there is no timer), reinstall backs drifted units up first.
    `allowed` narrows the jobs to the ones accepted at first run.
    """

    def __init__(self, vault, home=HOME, units_dir=None, systemctl="systemctl", run=subprocess.run, timeout=15,
                 backup_dir=None, environ=None, clock=None, allowed=None):
        self.vault, self.home = vault, home
        self.units_dir = units_dir or os.path.join(home, ".config", "systemd", "user")
        self.systemctl, self.run, self.timeout = systemctl, run, timeout
        self.backup_dir = backup_dir
        self.environ = os.environ if environ is None else environ
        self.clock = clock or SystemClock()
        self.allowed = None if allowed is None else set(allowed)

    def _call(self, *args):
        try:
            p = self.run([self.systemctl, "--user"] + list(args), capture_output=True, text=True,
                         timeout=self.timeout, stdin=subprocess.DEVNULL)
            return p.returncode, p.stdout or "", p.stderr or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", "%s: %s" % (type(exc).__name__, exc)

    def _folder(self):
        return os.path.join(self.vault, "_bin", "systemd")

    def _timed(self, label) -> bool:
        return os.path.exists(os.path.join(self._folder(), label + ".timer"))

    def _units(self, label):
        return (label + ".service", label + ".timer") if self._timed(label) else (label + ".service",)

    def _main(self, label) -> str:
        """The unit that is enabled, started and checked: the timer, or the service when it runs alone."""
        return label + (".timer" if self._timed(label) else ".service")

    def labels(self) -> list:
        try:
            names = sorted({os.path.splitext(f)[0] for f in os.listdir(self._folder())
                            if f.endswith(".timer") or f.endswith(".service")})
        except OSError:
            return []
        return [n for n in names if self.allowed is None or n in self.allowed]

    def render(self, label) -> dict:
        out = {}
        for unit in self._units(label):
            with open(os.path.join(self._folder(), unit), encoding="utf-8") as fh:
                out[unit] = fh.read().replace(D.ORIGIN_VAULT, self.vault).replace(D.ORIGIN_HOME, self.home)
        return out

    def installed(self, label) -> bool:
        return all(os.path.exists(os.path.join(self.units_dir, u)) for u in self._units(label))

    def is_loaded(self, label) -> bool:
        return self._call("is-active", "--quiet", self._main(label))[0] == 0

    def last_exit_ok(self, label):
        rc, out, _ = self._call("show", label + ".service", "--property=Result", "--property=ExecMainStatus")
        if rc != 0:
            return False, "not known to systemctl (exit %d)" % rc
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        result = (props.get("Result") or "success").strip()
        status = (props.get("ExecMainStatus") or "0").strip()
        return result == "success" and status == "0", "Result=%s ExecMainStatus=%s" % (result, status)

    def install(self, label):
        try:
            for unit, text in self.render(label).items():
                atomic_write(os.path.join(self.units_dir, unit), text)
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        rc, out, err = self._call("daemon-reload")
        if rc != 0:
            return False, "daemon-reload failed: %s" % (err or out).strip()
        return True, os.path.join(self.units_dir, self._main(label))

    def bootstrap(self, label):
        rc, out, err = self._call("enable", "--now", self._main(label))
        return rc == 0, (err or out).strip()

    def drifted(self, label) -> bool:
        """An installed unit is not what the vault's template renders to on this machine."""
        try:
            for unit, text in self.render(label).items():
                with open(os.path.join(self.units_dir, unit), encoding="utf-8") as fh:
                    if fh.read() != text:
                        return True
        except OSError:
            return False
        return False

    def self_label(self) -> str:
        """The job this process runs as: the units set BRAIN_JOB_LABEL, or ""."""
        return self.environ.get("BRAIN_JOB_LABEL") or ""

    def reinstall(self, label):
        try:
            backup_dir = self.backup_dir or os.path.join(default_state_dir(), "unit-backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = self.clock.now().strftime("%Y%m%d-%H%M%S")
            for unit in self._units(label):
                src = os.path.join(self.units_dir, unit)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(backup_dir, "%s.%s" % (unit, stamp)))
        except Exception as exc:
            return False, "backup failed, units left as they were: %s: %s" % (type(exc).__name__, exc)
        note = "previous units in %s" % backup_dir
        was_loaded = self.is_loaded(label)
        done, detail = self.install(label)
        if not done:
            return False, "%s; %s" % (detail, note)
        if was_loaded:
            rc, out, err = self._call("restart", self._main(label))
            return rc == 0, "; ".join(x for x in (note, (err or out).strip()) if x)
        return True, "rewritten (not loaded); " + note


CRON_BEGIN = "# BEGIN second-brain (managed by guardian.py; edit _bin/cron/*.cron in the vault instead)"
CRON_END = "# END second-brain"


class CronControl:
    """Where there are no systemd user units: one crontab line per job, inside a block Brain owns.

    Every `_bin/cron/<label>.cron` is one job (its first line that is not a comment); the line
    installed ends in `# brain:<label>`. Lines outside the block are never touched. Cron keeps
    no exit status and needs no load step, so those two answers are fixed.
    """

    def __init__(self, vault, home=HOME, crontab="crontab", run=subprocess.run, timeout=15, environ=None,
                 allowed=None):
        self.vault, self.home = vault, home
        self.crontab, self.run, self.timeout = crontab, run, timeout
        self.environ = os.environ if environ is None else environ
        self.allowed = None if allowed is None else set(allowed)

    def _call(self, args, stdin=None):
        try:
            p = self.run([self.crontab] + list(args), input=stdin, capture_output=True, text=True,
                         timeout=self.timeout)
            return p.returncode, p.stdout or "", p.stderr or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", "%s: %s" % (type(exc).__name__, exc)

    def _folder(self):
        return os.path.join(self.vault, "_bin", "cron")

    def labels(self) -> list:
        try:
            names = sorted(f[:-len(".cron")] for f in os.listdir(self._folder()) if f.endswith(".cron"))
        except OSError:
            return []
        return [n for n in names if self.allowed is None or n in self.allowed]

    def render(self, label) -> str:
        with open(os.path.join(self._folder(), label + ".cron"), encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip() and not l.strip().startswith("#")]
        line = lines[0].replace(D.ORIGIN_VAULT, self.vault).replace(D.ORIGIN_HOME, self.home)
        return "%s # brain:%s" % (line, label)

    def _split(self):
        rc, out, _ = self._call(["-l"])
        lines = out.splitlines() if rc == 0 else []
        if CRON_BEGIN in lines and CRON_END in lines[lines.index(CRON_BEGIN):]:
            i = lines.index(CRON_BEGIN)
            j = lines.index(CRON_END, i)
            return lines[:i], lines[i + 1:j], lines[j + 1:]
        return lines, [], []

    def _line(self, label):
        marker = "# brain:%s" % label
        return next((l for l in self._split()[1] if l.endswith(marker)), None)

    def installed(self, label) -> bool:
        return self._line(label) is not None

    def is_loaded(self, label) -> bool:
        return self.installed(label)

    def last_exit_ok(self, label):
        return True, "cron keeps no exit status"

    def install(self, label):
        try:
            wanted = self.render(label)
        except (OSError, IndexError) as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        before, block, after = self._split()
        block = [l for l in block if not l.endswith("# brain:%s" % label)] + [wanted]
        text = "\n".join(before + [CRON_BEGIN] + block + [CRON_END] + after) + "\n"
        rc, out, err = self._call(["-"], stdin=text)
        return rc == 0, (err or out).strip() or "crontab updated"

    def bootstrap(self, label):
        return True, "cron runs installed lines without a load step"

    def drifted(self, label) -> bool:
        line = self._line(label)
        try:
            return line is not None and line != self.render(label)
        except (OSError, IndexError):
            return False

    def self_label(self) -> str:
        return self.environ.get("BRAIN_JOB_LABEL") or ""

    def reinstall(self, label):
        done, detail = self.install(label)
        return done, "rewritten; " + detail


# ---------------------------------------------------------------- Windows Task Scheduler


SCHTASKS_START = "2026-01-01T00:00:00"     # a fixed past start: the render is the same on every run
# Last Run Result codes that are not a failure: 0, ready (0x41300), running (0x41301),
# never run (0x41303), ended by the user or by the scheduler on a restart (0x41306).
SCHTASKS_OK_RESULTS = {0, 267008, 267009, 267011, 267014}


def default_pythonw(executable=None):
    """pythonw.exe next to this Python when there is one (no console window every minute), else this Python."""
    exe = executable or _sys_platform.executable
    folder, name = os.path.split(exe)
    if name.lower() == "python.exe":
        candidate = os.path.join(folder, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def _xml(text) -> str:
    from xml.sax.saxutils import escape

    return escape(str(text))


class SchtasksControl:
    """The scheduled jobs the vault defines, as per-user Windows Task Scheduler tasks (schtasks.exe).

    Every `_bin/schtasks/<label>.json` is one job: the script it runs and either `every_minutes`
    (a periodic job, like a systemd timer) or `at_logon` (a long-lived server, restarted when it
    fails, like Restart=always). Same port as LaunchctlControl and SystemdUserControl:

      install    renders the task XML, keeps a copy in <brain state>/schtasks/<label>.xml (the
                 "installed" file, as ~/.config/systemd/user holds the units) and registers it
                 with `schtasks /Create /XML <file> /TN <label> /F`
      bootstrap  registers it again from that copy (a task deleted by hand comes back) and starts a
                 server at once with `/Run`
      is_loaded  the task is registered and enabled (`/Query /XML`)
      reinstall  backs the drifted copy up first, then installs; a running server is restarted

    Every task runs as the current user, only while that user is logged on (InteractiveToken):
    no password is stored and no administrator rights are needed. The command is the real Python
    (pythonw.exe, so no console window opens) in UTF-8 mode, through jobrun.py, which sets
    BRAIN_JOB_LABEL and PYTHONUTF8=1 and logs to <brain state>/logs/<label>.log:

      pythonw.exe -X utf8 "<vault>\\_bin\\jobrun.py" <label> "<vault>\\_bin\\guardian.py" repair
    """

    def __init__(self, vault, home=HOME, tasks_dir=None, schtasks="schtasks", run=subprocess.run, timeout=30,
                 backup_dir=None, environ=None, clock=None, allowed=None, python=None, user=None):
        self.vault, self.home = vault, home
        self.environ = os.environ if environ is None else environ
        self.tasks_dir = tasks_dir or os.path.join(default_state_dir(), "schtasks")
        self.schtasks, self.run, self.timeout = schtasks, run, timeout
        self.backup_dir = backup_dir
        self.clock = clock or SystemClock()
        self.allowed = None if allowed is None else set(allowed)
        self.python = python or default_pythonw()
        if user is None:
            name, domain = self.environ.get("USERNAME") or "", self.environ.get("USERDOMAIN") or ""
            user = ("%s\\%s" % (domain, name)) if domain and name else name
        self.user = user

    def _call(self, *args):
        try:
            p = self.run([self.schtasks] + list(args), capture_output=True, text=True, errors="replace",
                         timeout=self.timeout, stdin=subprocess.DEVNULL)
            return p.returncode, (p.stdout or "").replace("\x00", ""), (p.stderr or "").replace("\x00", "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", "%s: %s" % (type(exc).__name__, exc)

    def _folder(self):
        return os.path.join(self.vault, "_bin", "schtasks")

    def _installed(self, label):
        return os.path.join(self.tasks_dir, label + ".xml")

    def labels(self) -> list:
        try:
            names = sorted(f[:-len(".json")] for f in os.listdir(self._folder()) if f.endswith(".json"))
        except OSError:
            return []
        return [n for n in names if self.allowed is None or n in self.allowed]

    def spec(self, label) -> dict:
        with open(os.path.join(self._folder(), label + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not data.get("script"):
            raise ValueError("%s.json names no script" % label)
        return data

    def _long_lived(self, label) -> bool:
        try:
            return bool(self.spec(label).get("at_logon"))
        except (OSError, ValueError):
            return False

    def command(self, label):
        """(program, arguments, working directory) the task runs."""
        spec = self.spec(label)
        script = os.path.join(self.vault, *spec["script"].split("/"))
        jobrun = os.path.join(self.vault, "_bin", "jobrun.py")
        argv = ["-X", "utf8", jobrun, label, script] + [str(a) for a in spec.get("args") or []]
        cwd = self.home if spec.get("cwd") == "home" else self.vault
        return self.python, subprocess.list2cmdline(argv), cwd

    def render(self, label) -> str:
        """The task as Task Scheduler XML, for this vault, home, Python and user."""
        spec = self.spec(label)
        program, arguments, cwd = self.command(label)
        user = "      <UserId>%s</UserId>\n" % _xml(self.user) if self.user else ""
        if spec.get("at_logon"):
            trigger = ("    <LogonTrigger>\n      <Enabled>true</Enabled>\n%s    </LogonTrigger>\n"
                       % user)
            limit, restart = "PT0S", ("    <RestartOnFailure>\n      <Interval>PT1M</Interval>\n"
                                      "      <Count>999</Count>\n    </RestartOnFailure>\n")
        else:
            minutes = int(spec.get("every_minutes") or 0)
            if minutes < 1:
                raise ValueError("%s.json has neither every_minutes nor at_logon" % label)
            trigger = ("    <TimeTrigger>\n      <Repetition>\n        <Interval>PT%dM</Interval>\n"
                       "        <StopAtDurationEnd>false</StopAtDurationEnd>\n      </Repetition>\n"
                       "      <StartBoundary>%s</StartBoundary>\n      <Enabled>true</Enabled>\n"
                       "    </TimeTrigger>\n" % (minutes, SCHTASKS_START))
            limit, restart = "PT1H", ""
        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            "  <RegistrationInfo>\n"
            "    <Description>%(desc)s</Description>\n"
            "    <URI>\\%(label)s</URI>\n"
            "  </RegistrationInfo>\n"
            "  <Triggers>\n%(trigger)s  </Triggers>\n"
            "  <Principals>\n"
            '    <Principal id="Author">\n'
            "%(user)s"
            "      <LogonType>InteractiveToken</LogonType>\n"
            "      <RunLevel>LeastPrivilege</RunLevel>\n"
            "    </Principal>\n"
            "  </Principals>\n"
            "  <Settings>\n"
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <AllowHardTerminate>true</AllowHardTerminate>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
            "    <IdleSettings>\n"
            "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
            "      <RestartOnIdle>false</RestartOnIdle>\n"
            "    </IdleSettings>\n"
            "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
            "    <Enabled>true</Enabled>\n"
            "    <Hidden>false</Hidden>\n"
            "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
            "    <WakeToRun>false</WakeToRun>\n"
            "    <ExecutionTimeLimit>%(limit)s</ExecutionTimeLimit>\n"
            "    <Priority>7</Priority>\n"
            "%(restart)s"
            "  </Settings>\n"
            '  <Actions Context="Author">\n'
            "    <Exec>\n"
            "      <Command>%(program)s</Command>\n"
            "      <Arguments>%(arguments)s</Arguments>\n"
            "      <WorkingDirectory>%(cwd)s</WorkingDirectory>\n"
            "    </Exec>\n"
            "  </Actions>\n"
            "</Task>\n"
        ) % {"desc": _xml(spec.get("description") or label), "label": _xml(label), "trigger": trigger,
             "user": user, "limit": limit, "restart": restart, "program": _xml(program),
             "arguments": _xml(arguments), "cwd": _xml(cwd)}

    def _read_installed(self, label):
        with open(self._installed(label), encoding="utf-16") as fh:
            return fh.read()

    def installed(self, label) -> bool:
        return os.path.exists(self._installed(label))

    def is_loaded(self, label) -> bool:
        rc, out, _ = self._call("/Query", "/TN", label, "/XML")
        if rc != 0:
            return False
        settings = re.search(r"<Settings>(.*?)</Settings>", out, re.S)
        enabled = re.search(r"<Enabled>\s*(\w+)\s*</Enabled>", re.sub(r"<IdleSettings>.*?</IdleSettings>", "",
                                                                    settings.group(1), flags=re.S)) if settings else None
        return not (enabled and enabled.group(1).lower() == "false")

    def last_exit_ok(self, label):
        import csv
        import io

        rc, out, _ = self._call("/Query", "/TN", label, "/V", "/FO", "CSV", "/NH")
        if rc != 0:
            return False, "not known to schtasks (exit %d)" % rc
        rows = [r for r in csv.reader(io.StringIO(out)) if len(r) > 6]
        if not rows:
            return True, "no run recorded"
        value = rows[0][6].strip()                       # "Last Result": the same column in every language
        try:
            code = int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return True, "Last Result=%s" % value
        code &= 0xFFFFFFFF
        return code in SCHTASKS_OK_RESULTS, "Last Result=%d" % code

    def install(self, label):
        try:
            text = self.render(label)
            os.makedirs(self.tasks_dir, exist_ok=True)
            tmp = "%s.%d.tmp" % (self._installed(label), os.getpid())
            with open(tmp, "w", encoding="utf-16") as fh:            # schtasks /XML wants UTF-16
                fh.write(text)
            os.replace(tmp, self._installed(label))
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        rc, out, err = self._call("/Create", "/TN", label, "/XML", self._installed(label), "/F")
        if rc != 0:
            return False, "schtasks /Create failed: %s" % ((err or out).strip() or "exit %d" % rc)
        return True, self._installed(label)

    def bootstrap(self, label):
        rc, out, err = self._call("/Create", "/TN", label, "/XML", self._installed(label), "/F")
        if rc != 0:
            return False, (err or out).strip() or "schtasks /Create exit %d" % rc
        if self._long_lived(label):
            rc, out, err = self._call("/Run", "/TN", label)
        return rc == 0, (err or out).strip()

    def drifted(self, label) -> bool:
        """The recorded task is not what the vault's template renders to on this machine."""
        try:
            return self._read_installed(label) != self.render(label)
        except (OSError, ValueError, UnicodeError):
            return False

    def self_label(self) -> str:
        """The job this process runs as: jobrun.py sets BRAIN_JOB_LABEL, or ""."""
        return self.environ.get("BRAIN_JOB_LABEL") or ""

    def reinstall(self, label):
        try:
            backup_dir = self.backup_dir or os.path.join(default_state_dir(), "schtasks-backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = self.clock.now().strftime("%Y%m%d-%H%M%S")
            if os.path.exists(self._installed(label)):
                shutil.copy2(self._installed(label), os.path.join(backup_dir, "%s.xml.%s" % (label, stamp)))
        except Exception as exc:
            return False, "backup failed, task left as it was: %s: %s" % (type(exc).__name__, exc)
        note = "previous task in %s" % backup_dir
        was_loaded = self.is_loaded(label)
        done, detail = self.install(label)
        if not done:
            return False, "%s; %s" % (detail, note)
        if was_loaded and self._long_lived(label):
            self._call("/End", "/TN", label)
            rc, out, err = self._call("/Run", "/TN", label)
            return rc == 0, "; ".join(x for x in (note, (err or out).strip()) if x)
        return True, "rewritten; " + note


class LogNotifier:
    """No desktop notification, one line on stderr instead (the job's log under Task Scheduler,
    through jobrun.py). The guardian's alerts still reach the mail queue and the status. It is also
    what WindowsToastNotifier falls back to, so an alert is never lost."""

    def __init__(self, stream=None):
        self.stream = stream

    def notify(self, title: str, message: str) -> None:
        try:
            stream = self.stream or _sys_platform.stderr
            if stream:
                stream.write("notification: %s: %s\n" % (title, message))
        except Exception:
            pass


TOAST_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"


# PowerShell reads ' and the typographic single quotes U+2018-U+201B alike as the delimiter of a
# single-quoted string: a ’ in an alert's text would end the string and run the rest as code.
PS_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"


def _ps_quote(s: str) -> str:
    """A PowerShell single-quoted string: only its quote characters are special, and each is doubled."""
    return "'" + "".join(c + c if c in PS_SINGLE_QUOTES else c for c in str(s)) + "'"


def _xml_escape(s: str) -> str:
    """XML text, with the typographic single quotes as numeric references so none reaches PowerShell."""
    s = (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
         .replace('"', "&quot;").replace("'", "&apos;"))
    return "".join("&#x%X;" % ord(c) if c in PS_SINGLE_QUOTES else c for c in s)


def toast_script(title: str, message: str, app_id: str = TOAST_APP_ID) -> str:
    """The PowerShell that shows a ToastText02 toast (headline + body) under Windows PowerShell's own
    AppUserModelID, which every Windows already has registered. Title and message are XML-escaped
    into the toast XML, and the whole XML is then quoted as one PowerShell single-quoted string (a
    ' is doubled). Newlines become spaces: a line break inside -Command would end the statement."""
    flat = lambda t: _xml_escape(" ".join(str(t).split()))
    xml = ("<toast><visual><binding template='ToastText02'><text id='1'>%s</text><text id='2'>%s</text>"
           "</binding></visual></toast>" % (flat(title), flat(message)))
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml(%s); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(%s).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))" % (_ps_quote(xml), _ps_quote(app_id)))


class WindowsToastNotifier:
    """A Windows toast through powershell.exe, stdlib only. Best effort, and never silent: when
    PowerShell cannot run, times out or exits non-zero, the alert goes to `fallback` (the log line)."""

    def __init__(self, run=subprocess.run, powershell="powershell.exe", timeout=10, fallback=None):
        self.run, self.powershell, self.timeout = run, powershell, timeout
        self.fallback = fallback or LogNotifier()

    def argv(self, title: str, message: str) -> list:
        return [self.powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-Command", toast_script(title, message)]

    def notify(self, title: str, message: str) -> None:
        shown = False
        try:
            p = self.run(self.argv(title, message), capture_output=True, text=True, timeout=self.timeout,
                         stdin=subprocess.DEVNULL, creationflags=osproc.CREATE_NO_WINDOW)
            shown = getattr(p, "returncode", 1) == 0
        except Exception:
            shown = False
        if not shown:
            try:
                self.fallback.notify(title, message)
            except Exception:
                pass


class NotifySendNotifier:
    """A desktop notification on Linux, through notify-send. Best effort, like OsascriptNotifier."""

    def __init__(self, run=subprocess.run, notify_send="notify-send", timeout=10):
        self.run, self.notify_send, self.timeout = run, notify_send, timeout

    def notify(self, title: str, message: str) -> None:
        try:
            self.run([self.notify_send, "--app-name=Brain", title, message], capture_output=True, text=True,
                     timeout=self.timeout, stdin=subprocess.DEVNULL)
        except Exception:
            pass


def default_notifier(platform=None, which=shutil.which):
    """osascript on macOS, a toast through PowerShell (log line when that fails) on Windows,
    notify-send elsewhere."""
    import sys as _sys

    if (platform or _sys.platform) == "darwin":
        return OsascriptNotifier()
    if (platform or _sys.platform) == "win32":
        return WindowsToastNotifier()
    return NotifySendNotifier(notify_send=which("notify-send") or "notify-send")


# ---------------------------------------------------------------- first-run consent

JOBS = ("guardian", "sync", "tasks", "watch")
SCHEDULERS = ("launchd", "systemd", "cron", "schtasks")
# The Remote Control server is not a periodic job: it is accepted in its own first-run step and
# only a supervisor that restarts a long-lived process can keep it (launchd KeepAlive, systemd
# Restart=always, a Task Scheduler logon task with RestartOnFailure). Cron cannot.
REMOTE_CONTROL = "remote-control"
SUPERVISORS = ("launchd", "systemd", "schtasks")


def first_run_state_path(state_dir=None) -> str:
    """Where integrations/first-run keeps the user's answers: <brain state>/first-run.json."""
    return os.path.join(state_dir or default_state_dir(), "first-run.json")


def consented_jobs(path):
    """(scheduler kind, [job names]) the user accepted at first run; ("", []) when nothing was.

    The periodic jobs come from the scheduler step; the Remote Control server, from its own step.

    Brain installs no scheduled job the user did not accept there: the guardian repairs and
    reloads only these."""
    data = read_json(path, {})
    if not isinstance(data, dict):
        return "", []
    sched = data.get("scheduler") if isinstance(data.get("scheduler"), dict) else {}
    kind = sched.get("kind") if sched.get("kind") in SCHEDULERS else ""
    jobs = [j for j in (sched.get("jobs") or []) if j in JOBS] if kind else []
    steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
    rc = steps.get("remote_control") if isinstance(steps.get("remote_control"), dict) else {}
    if rc.get("status") == "done" and rc.get("kind") in SUPERVISORS and rc.get("kind") == (kind or rc["kind"]):
        kind = rc["kind"]
        jobs.append(REMOTE_CONTROL)
    return (kind, jobs) if kind else ("", [])


def job_label(kind: str, job: str) -> str:
    return ("com.secondbrain.%s" % job) if kind == "launchd" else ("second-brain-%s" % job)


def build_job_control(vault, state_dir=None, home=HOME):
    """The scheduler adapter for what the user accepted: launchd, systemd user units, cron or Task Scheduler."""
    kind, jobs = consented_jobs(first_run_state_path(state_dir))
    labels = [job_label(kind, j) for j in jobs]
    if kind == "systemd":
        return SystemdUserControl(vault=vault, home=home, allowed=labels)
    if kind == "cron":
        return CronControl(vault=vault, home=home, allowed=labels)
    if kind == "schtasks":
        return SchtasksControl(vault=vault, home=home, allowed=labels,
                               tasks_dir=os.path.join(state_dir or default_state_dir(), "schtasks"))
    return LaunchctlControl(vault=vault, home=home, allowed=labels)
