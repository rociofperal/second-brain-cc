#!/usr/bin/env python3
"""Skills and agents: the vault's plugin/brain is canonical, ~/.claude holds installed copies.

  install_plugin.py status            what each skill and agent needs; changes nothing
  install_plugin.py sync [--dry-run]  apply it: install, back-port, report conflicts
  install_plugin.py install           install only what is missing or moved on in the vault;
                                      never back-port (what bootstrap.sh runs)

Skills are still often edited live in ~/.claude, and the vault copy can move on from another
machine through git. Comparing the two copies cannot tell which one is newer, so each sync
records the digest it left both sides at (<brain state>/plugin-manifest.json) and decides
three-way:

  same           vault == live                          record it
  install        only the vault has it, or live is      copy vault -> ~/.claude (live copy
                 still what was last synced                 backed up first if it exists)
  backport       only live has it, live was edited      copy ~/.claude -> vault (vault copy
                 since the sync, or it never synced         backed up to tar.gz, diff written)
  conflict       both changed since the sync            touch neither; back up both, write the
                                                            diff for a person to resolve
  removed-live   deleted from ~/.claude after a sync    report only: never reinstalled
  removed-vault  deleted from the vault after a sync    report only: live is never deleted

Canonical skills and agents may say `__VAULT__` where they need the vault's path: an install writes
this machine's vault path there, a back-port turns it back into `__VAULT__`, and the live copy is
digested with the path turned back, so a fresh install still counts as `same`.

Nothing is overwritten without its previous content going to
<brain state>/plugin-backups/ first. The backup command follows
30-Knowledge/2026-09-12-convention-back-up-a-skill-before-rewriting-it.md (tar.gz of the
skill directory).
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pycmd

KINDS = ("skills", "agents")
# Dependencies and caches a skill may create in its own folder (a Python venv, browsers downloaded by
# Playwright, tool caches) are machine-local, can hold tens of thousands of files, and are never part
# of the skill: they are not copied, compared or back-ported.
IGNORED_NAMES = {".git", "node_modules", "__pycache__", ".DS_Store", ".venv", "venv", "ms-playwright",
                 ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"}
IGNORED_SUFFIXES = (".pyc",)
_IGNORE = shutil.ignore_patterns(*sorted(IGNORED_NAMES), "*.pyc")


# ---------------------------------------------------------------- pure rules


def decide(vault, live, base):
    """The action for one item, from the vault digest, the live digest and the last synced one."""
    if vault is None and live is None:
        return "none"
    if vault == live:
        return "same"
    if live is None:
        return "removed-live" if base is not None and vault == base else "install"
    if vault is None:
        return "removed-vault" if base is not None and live == base else "backport"
    if base is None:
        return "backport"
    if live == base:
        return "install"
    if vault == base:
        return "backport"
    return "conflict"


# ---------------------------------------------------------------- disk


def _ignored(name):
    return name in IGNORED_NAMES or name.endswith(IGNORED_SUFFIXES)


def _files(path):
    """Relative paths of every file under a skill directory, ignored names skipped."""
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not _ignored(d))
        for name in sorted(files):
            if not _ignored(name):
                out.append(os.path.relpath(os.path.join(root, name), path))
    return out


PLACEHOLDER = "__VAULT__"


# On Windows a skill that runs `/usr/bin/python3 __VAULT__/_bin/x.py` must start Python in UTF-8 mode
# with the interpreter this machine has: the install writes `"<python.exe>" -X utf8 "<vault>\\_bin\\x.py"`
# there (pycmd.windows_python: one canonical python.exe, never pythonw.exe). A skill may also say bare
# `python3 __VAULT__/_bin/x.py`; that one is written with the flag in its attached spelling, `-Xutf8`
# (the same option to Python), so the back-port can tell the two apart and restore each exactly.
_POSIX_RUN = re.compile(r"/usr/bin/python3 __VAULT__/_bin/([A-Za-z0-9_.\-]+\.py)")
_BARE_RUN = re.compile(r"(?<![\w/.\-])python3 __VAULT__/_bin/([A-Za-z0-9_.\-]+\.py)")
_WIN_FLAGS = {"-X utf8": "/usr/bin/python3", "-Xutf8": "python3"}


def localize_text(text, vault, platform=None, executable=None):
    """Canonical skill text for this machine: `__VAULT__` becomes the vault path. Windows: the
    canonical `/usr/bin/python3 __VAULT__/_bin/x.py` (and bare `python3 ...`) first becomes the UTF-8
    mode command."""
    if pycmd.is_windows(platform):
        def run(m, flags):
            line = pycmd.hook_command("%s/_bin/%s" % (vault, m.group(1)), platform="win32", executable=executable)
            return line.replace(" -X utf8 ", " %s " % flags, 1)
        text = _POSIX_RUN.sub(lambda m: run(m, "-X utf8"), text)
        text = _BARE_RUN.sub(lambda m: run(m, "-Xutf8"), text)
    return text.replace(PLACEHOLDER, vault)


def delocalize_text(text, vault, platform=None, executable=None):
    """The inverse of localize_text: the vault path becomes `__VAULT__` again.

    Windows: any quoted python.exe / pythonw.exe / python3.exe / py.exe running a script of this
    vault's _bin turns back, whichever interpreter wrote it and whichever one runs now (a skill
    installed from a terminal and back-ported by the guardian under pythonw.exe), so no machine's
    interpreter ever reaches the canonical copy. `executable` is accepted and not needed."""
    if pycmd.is_windows(platform):
        run = re.compile(r'"(?:[^"]*[\\/])?(?i:(?:python|pythonw|python3|py)\.exe)" (-X utf8|-Xutf8) "'
                         + re.escape(pycmd.win_path(vault) + "\\_bin\\") + r'([A-Za-z0-9_.\-]+\.py)"')
        text = run.sub(lambda m: "%s __VAULT__/_bin/%s" % (_WIN_FLAGS[m.group(1)], m.group(2)), text)
    return text.replace(vault, PLACEHOLDER)


def _unlocalized(data, vault, reverse=None):
    if reverse is not None and vault:
        try:
            return reverse(data.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            return data
    return data.replace(vault.encode("utf-8"), PLACEHOLDER.encode("utf-8")) if vault else data


def digest(path, vault=None, reverse=None):
    """sha256 of a file, or of a directory's relative paths and contents. None when missing.

    With `vault`, that path is read as `__VAULT__` first: how a live copy is compared with the
    canonical one it was installed from. `reverse` (text -> text) replaces that plain replacement."""
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(_unlocalized(fh.read(), vault, reverse)).hexdigest()
    if not os.path.isdir(path):
        return None
    h = hashlib.sha256()
    for rel in _files(path):
        with open(os.path.join(path, rel), "rb") as fh:
            h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(_unlocalized(fh.read(), vault, reverse)).digest())
    return h.hexdigest()


def _rewrite_text(path, old, new=None):
    """Replace `old` with `new` in every text file at `path` (a file or a directory). Binary files stay.
    `old` may instead be a function text -> text."""
    fn = old if callable(old) else (lambda text: text.replace(old, new) if old in text else text)
    targets = [path] if os.path.isfile(path) else [os.path.join(path, rel) for rel in _files(path)]
    for target in targets:
        try:
            with open(target, encoding="utf-8", newline="") as fh:   # keep CRLF as it is
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        converted = fn(text)
        if converted != text:
            mode = os.stat(target).st_mode
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(converted)
            os.chmod(target, mode)


class Syncer:
    def __init__(self, plugin_dir, claude_dir, state_dir, clock=time.time, log=None, vault=None,
                 platform=None, executable=None):
        self.plugin_dir, self.claude_dir, self.state_dir = plugin_dir, claude_dir, state_dir
        self.vault = vault
        # Windows only: skills run Python in UTF-8 mode with `executable` (localize_text). Injectable for tests.
        self.platform, self.executable = platform, executable
        self._windows = bool(vault) and pycmd.is_windows(platform)
        self.clock = clock
        self.log = log or (lambda s: None)

    # -- locations

    def _path(self, root, kind, name):
        return os.path.join(root, kind, name + ".md") if kind == "agents" else os.path.join(root, kind, name)

    def _names(self, root, kind):
        base = os.path.join(root, kind)
        if not os.path.isdir(base):
            return set()
        if kind == "agents":
            return {f[:-3] for f in os.listdir(base) if f.endswith(".md") and os.path.isfile(os.path.join(base, f))}
        return {d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "SKILL.md"))}

    @property
    def manifest_path(self):
        return os.path.join(self.state_dir, "plugin-manifest.json")

    def _manifest(self):
        try:
            with open(self.manifest_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_manifest(self, data):
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = "%s.%d.tmp" % (self.manifest_path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.manifest_path)

    def _reverse(self):
        """text -> text turning the live copy back into canonical, or None for the plain path swap."""
        if not self._windows:
            return None
        return lambda text: delocalize_text(text, self.vault, self.platform, self.executable)

    def _to_live(self):
        """The `rewrite` for an install (canonical -> live), None without a vault."""
        if not self.vault:
            return None
        if self._windows:
            return (lambda text: localize_text(text, self.vault, self.platform, self.executable),)
        return (PLACEHOLDER, self.vault)

    def _to_canonical(self):
        """The `rewrite` for a back-port (live -> canonical), None without a vault."""
        if not self.vault:
            return None
        if self._windows:
            return (self._reverse(),)
        return (self.vault, PLACEHOLDER)

    # -- planning

    def plan(self):
        base = self._manifest()
        items = []
        for kind in KINDS:
            for name in sorted(self._names(self.plugin_dir, kind) | self._names(self.claude_dir, kind)):
                key = "%s/%s" % (kind, name)
                v = digest(self._path(self.plugin_dir, kind, name))
                l = digest(self._path(self.claude_dir, kind, name), self.vault, self._reverse())
                items.append({"kind": kind, "name": name, "key": key, "vault": v, "live": l,
                              "base": base.get(key), "action": decide(v, l, base.get(key))})
        return items

    # -- applying

    def _stamp(self):
        return time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))

    def _backup_dir(self):
        d = os.path.join(self.state_dir, "plugin-backups")
        os.makedirs(d, exist_ok=True)
        return d

    def _unique(self, stem, ext):
        path = stem + ext
        n = 1
        while os.path.exists(path):
            path = "%s-%d%s" % (stem, n, ext)
            n += 1
        return path

    def _backup(self, root, kind, name, side):
        src = self._path(root, kind, name)
        dest = self._unique(os.path.join(self._backup_dir(), "%s-%s-%s-%s" % (self._stamp(), kind, name, side)),
                            ".tar.gz")
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(src, arcname="%s/%s" % (kind, os.path.basename(src)),
                    filter=lambda ti: None if _ignored(os.path.basename(ti.name)) else ti)
        return dest

    def _diff(self, kind, name):
        vault, live = self._path(self.plugin_dir, kind, name), self._path(self.claude_dir, kind, name)
        if kind == "agents":
            pairs = [("", vault, live)]
        else:
            rels = sorted(set(_files(vault) if os.path.isdir(vault) else []) | set(_files(live) if os.path.isdir(live) else []))
            pairs = [(rel, os.path.join(vault, rel), os.path.join(live, rel)) for rel in rels]
        out = []
        for rel, a, b in pairs:
            try:
                al = open(a, encoding="utf-8").read().splitlines(True) if os.path.isfile(a) else []
                bl = open(b, encoding="utf-8").read() if os.path.isfile(b) else ""
                if self.vault:
                    bl = self._reverse()(bl) if self._windows else bl.replace(self.vault, PLACEHOLDER)
                bl = bl.splitlines(True)
            except UnicodeDecodeError:
                out.append("Binary file %s differs\n" % (rel or name))
                continue
            label = "%s/%s%s" % (kind, name, ("/" + rel) if rel else "")
            out += difflib.unified_diff(al, bl, fromfile="vault/" + label, tofile="live/" + label)
        dest = self._unique(os.path.join(self._backup_dir(), "%s-%s-%s" % (self._stamp(), kind, name)), ".diff")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.writelines(out)
        return dest

    def _replace(self, src, dest, kind, rewrite=None):
        """Copy src over dest through a temporary sibling, so a failure never leaves half a skill.
        `rewrite` is (old, new) applied to the copy's text files before it takes dest's place."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = os.path.join(os.path.dirname(dest), ".%s.sync-tmp-%d" % (os.path.basename(dest), os.getpid()))
        if kind == "agents":
            shutil.copy2(src, tmp)
            if rewrite:
                _rewrite_text(tmp, *rewrite)
            os.replace(tmp, dest)
            return
        if os.path.exists(tmp):
            shutil.rmtree(tmp)
        shutil.copytree(src, tmp, ignore=_IGNORE)
        if rewrite:
            _rewrite_text(tmp, *rewrite)
        old = tmp + ".old"
        if os.path.exists(dest):
            os.rename(dest, old)
        os.rename(tmp, dest)
        if os.path.exists(old):
            shutil.rmtree(old)

    def apply(self, dry_run=False, install_only=False):
        items = self.plan()
        if dry_run:
            return [{"kind": i["kind"], "name": i["name"], "action": i["action"]} for i in items]
        manifest = self._manifest()
        report = []
        for i in items:
            kind, name, key, action = i["kind"], i["name"], i["key"], i["action"]
            vault_path, live_path = self._path(self.plugin_dir, kind, name), self._path(self.claude_dir, kind, name)
            entry = {"kind": kind, "name": name, "action": action}
            try:
                if action == "same":
                    manifest[key] = i["vault"]
                elif action == "install":
                    if i["live"] is not None:
                        entry["backup"] = self._backup(self.claude_dir, kind, name, "live")
                    self._replace(vault_path, live_path, kind, self._to_live())
                    manifest[key] = digest(live_path, self.vault, self._reverse())
                elif install_only and action in ("backport", "conflict"):
                    entry["action"] = "%s (skipped: install only)" % action
                elif action == "backport":
                    if i["vault"] is not None:
                        entry["backup"] = self._backup(self.plugin_dir, kind, name, "vault")
                        entry["diff"] = self._diff(kind, name)
                    self._replace(live_path, vault_path, kind, self._to_canonical())
                    manifest[key] = digest(vault_path)
                elif action == "conflict":
                    entry["backups"] = [self._backup(self.plugin_dir, kind, name, "vault"),
                                        self._backup(self.claude_dir, kind, name, "live")]
                    entry["diff"] = self._diff(kind, name)
            except Exception as exc:
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            if action not in ("same", "none"):
                self.log("plugin %s %s%s" % (entry["action"], key, (" — " + entry["error"]) if "error" in entry else ""))
            report.append(entry)
        self._save_manifest(manifest)
        return report


# ---------------------------------------------------------------- cli


def default_syncer(log=None):
    import brain_paths

    vault = os.environ.get("BRAIN_VAULT") or os.path.dirname(HERE)
    return Syncer(os.path.join(vault, "integrations", "claude-code", "plugin", "brain"), os.path.join(os.path.expanduser("~"), ".claude"),
                  brain_paths.state_dir(), log=log, vault=vault)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="install_plugin.py", description="sync skills and agents: vault canonical")
    sub = ap.add_subparsers(dest="cmd", metavar="{status,sync,install}")
    sub.add_parser("status", help="what each skill and agent needs; changes nothing")
    s = sub.add_parser("sync", help="install, back-port, report conflicts")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("install", help="install only; never back-port (bootstrap)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.cmd:
        ap.print_help(sys.stderr)
        return 2
    syncer = default_syncer()
    if args.cmd == "status":
        for i in syncer.plan():
            print("%-14s %s" % (i["action"], i["key"]))
        return 0
    report = syncer.apply(dry_run=getattr(args, "dry_run", False), install_only=args.cmd == "install")
    conflicts = 0
    for r in report:
        if r["action"] in ("same", "none"):
            continue
        extra = r.get("error") or r.get("backup") or ", ".join(r.get("backups") or [])
        print("%-14s %s/%s%s" % (r["action"], r["kind"], r["name"], ("  " + extra) if extra else ""))
        conflicts += r["action"] == "conflict"
    if conflicts:
        print("%d conflict(s): both copies changed since the last sync. Backups and diffs are in %s"
              % (conflicts, os.path.join(syncer.state_dir, "plugin-backups")), file=sys.stderr)
    return 1 if conflicts or any("error" in r for r in report) else 0


if __name__ == "__main__":
    sys.exit(main())
