"""Safe migration of project folders onto the dev volume.

For each folder, e.g. ~/os -> ~/dev/os:

  1. preflight: the volume is mounted; the folder is real (not a symlink), not
     a macOS-managed folder, has room to be copied; no running process has
     its working directory inside it (shells, editors, dev servers, agents)
  2. copy with `ditto` to ~/dev/os.quietmac-partial (keeps hard links,
     extended attributes, ACLs, permissions and times)
  3. verify: every path, type, size, mode, mtime and symlink target must
     match (or every byte, with --verify checksum). Any difference stops here
     with the original untouched.
  4. switch: rename the copy into place, rename the original to
     ~/os.quietmac-backup-<date>, and put a symlink ~/os -> ~/dev/os, so
     every old path keeps working
  5. Claude Code: link ~/.claude/projects entries for the new physical path
     to the existing ones, so per-project history and memory carry over

Every step is journaled in state["migrations"], so an interrupted run can be
resumed or rolled back. Backups stay until `quietmac migrate cleanup`.
Rollback restores the original (or copies your changes back with
--keep-changes).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import time
from typing import Any, Dict, List, Optional, Tuple

from . import core, devvol

PROTECTED = {"", "Library", "Desktop", "Documents", "Downloads", "Pictures", "Music", "Movies", "Public",
             "Applications", ".Trash", ".local", ".config", ".ssh", ".claude"}
IGNORE_DIFF = {".DS_Store"}


def inside(path: str, root: str) -> bool:
    """True if path is root or anywhere below it (not just sharing a prefix)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _home_rel(p: str) -> Optional[str]:
    home = os.environ["QUIETMAC_ROOT"] if core.SANDBOX else str(core.HOME)
    if p == home:
        return ""
    if p.startswith(home + "/"):
        return p[len(home) + 1:]
    return None


def busy(path: str) -> List[Tuple[int, str, str]]:
    """Processes whose working directory is inside path."""
    r = core.run(["/usr/sbin/lsof", "-nP", "-a", "-d", "cwd", "-u", core.USER, "-Fpcn"], timeout=60)
    found, pid, cmd = [], None, ""
    real = os.path.realpath(path)
    for line in r.out.splitlines():
        if line.startswith("p"):
            pid, cmd = int(line[1:]), ""
        elif line.startswith("c"):
            cmd = line[1:]
        elif line.startswith("n") and pid is not None:
            cwd = line[1:]
            if pid != os.getpid() and (inside(cwd, real) or inside(cwd, path)):
                found.append((pid, cmd, cwd))
    return found


def scan(root: str) -> Tuple[Dict[str, Tuple], int, int]:
    """{relpath: (kind, size, mode, mtime, link)}, file count, byte count."""
    entries: Dict[str, Tuple] = {}
    files = total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            try:
                s = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISLNK(s.st_mode):
                entries[rel] = ("link", 0, 0, 0, os.readlink(full))
            elif stat.S_ISDIR(s.st_mode):
                entries[rel] = ("dir", 0, stat.S_IMODE(s.st_mode), 0, "")
            elif stat.S_ISREG(s.st_mode):
                entries[rel] = ("file", s.st_size, stat.S_IMODE(s.st_mode), int(s.st_mtime), "")
                files += 1
                total += s.st_size
            else:
                entries[rel] = ("other", 0, stat.S_IMODE(s.st_mode), 0, "")
        # don't descend into symlinked dirs (os.walk doesn't by default)
    return entries, files, total


def compare(a: Dict[str, Tuple], b: Dict[str, Tuple], limit: int = 20) -> List[str]:
    problems = []
    for rel in sorted(set(a) | set(b)):
        if os.path.basename(rel) in IGNORE_DIFF:
            continue
        x, y = a.get(rel), b.get(rel)
        if x is None:
            problems.append("only in copy: " + rel)
        elif y is None:
            problems.append("missing from copy: " + rel)
        elif x != y:
            problems.append("differs: %s (%s vs %s)" % (rel, x[:4], y[:4]))
        if len(problems) >= limit:
            problems.append("... (stopped after %d differences)" % limit)
            break
    return problems


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def checksum_compare(src: str, dst: str, entries: Dict[str, Tuple], limit: int = 20) -> List[str]:
    problems = []
    for rel, e in sorted(entries.items()):
        if e[0] != "file" or os.path.basename(rel) in IGNORE_DIFF:
            continue
        try:
            if _sha(os.path.join(src, rel)) != _sha(os.path.join(dst, rel)):
                problems.append("content differs: " + rel)
        except OSError as ex:
            problems.append("can't read %s: %s" % (rel, ex))
        if len(problems) >= limit:
            break
    return problems


def claude_key(path: str) -> str:
    """Claude Code names its per-project folders after the path, with every
    character that isn't a letter or digit replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def claude_aliases(src: str, dst: str) -> List[Tuple[str, str]]:
    """(alias_name, existing_name) pairs to create in ~/.claude/projects."""
    d = core.CLAUDE_PROJECTS
    if not d.is_dir():
        return []
    old, new = claude_key(src), claude_key(dst)
    pairs = []
    for e in sorted(d.iterdir()):
        if e.is_symlink() or not e.is_dir():
            continue
        if e.name == old or e.name.startswith(old + "-"):
            alias = new + e.name[len(old):]
            if not (d / alias).exists():
                pairs.append((alias, e.name))
    return pairs


def _rmtree(path: str) -> None:
    def onerror(func: Any, p: str, exc: Any) -> None:
        try:
            os.chmod(os.path.dirname(p), 0o755)
            os.chmod(p, 0o755)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


# --------------------------------------------------------------------------


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def preflight(st: Dict[str, Any], folder: str) -> Dict[str, Any]:
    dv = st.get("devvol")
    src = os.path.abspath(os.path.expanduser(folder)).rstrip("/")
    info: Dict[str, Any] = {"src": src, "problems": [], "busy": []}
    if not dv:
        info["problems"].append("no dev volume yet: run `quietmac devvol create` first")
        return info
    mount = dv["mount"]
    dst = os.path.join(mount, os.path.basename(src))
    info["dst"] = dst
    rel = _home_rel(src)
    rec = st["migrations"].get(src)
    if rec and rec.get("status") == "done":
        info["problems"].append("already migrated to %s" % rec["dst"])
        return info
    if rel is None:
        info["problems"].append("only folders inside your home folder can be migrated")
    elif rel.split("/")[0] in PROTECTED:
        info["problems"].append("%s is managed by macOS or holds config; leave it where it is" % src)
    if os.path.islink(src):
        info["problems"].append("%s is already a symlink (-> %s)" % (src, os.path.realpath(src)))
    elif not os.path.isdir(src):
        info["problems"].append("%s isn't a folder" % src)
    if not devvol.is_mounted_at(mount, dv["uuid"]):
        info["problems"].append("the dev volume isn't mounted at %s (quietmac devvol mount)" % mount)
    if inside(mount, src):
        info["problems"].append("the dev volume is mounted inside %s" % src)
    if os.path.exists(dst) and not (rec and rec.get("status") == "switched"):
        info["problems"].append("%s already exists on the dev volume" % dst)
    cwd = os.getcwd()
    if inside(cwd, src):
        info["problems"].append("you're inside %s; `cd ~` and run quietmac from there" % src)
    if info["problems"]:
        return info
    entries, files, total = scan(src)
    info.update(entries=entries, files=files, bytes=total)
    free = devvol.container_free(dv.get("container") or devvol.data_container())
    if free is not None and free < total * 1.1 + 5 * 1024 ** 3:
        info["problems"].append("not enough free space: need %s plus margin, have %s (the original stays as a backup "
                                "until cleanup)" % (core.human_bytes(total), core.human_bytes(free)))
    info["busy"] = busy(src)
    return info


def plan_text(info: Dict[str, Any]) -> str:
    lines = ["%s -> %s" % (info["src"], info.get("dst", "?"))]
    if "files" in info:
        lines.append("    %d files, %s" % (info["files"], core.human_bytes(info["bytes"])))
    for p in info["problems"]:
        lines.append("    " + core.red("blocked: ") + p)
    for pid, cmd, cwd in info["busy"]:
        lines.append("    " + core.yellow("busy: ") + "%s (pid %d) is working in %s" % (cmd, pid, cwd))
    return "\n".join(lines)


def run_one(st: Dict[str, Any], folder: str, verify: str = "quick", ignore_busy: bool = False,
            aliases: bool = True) -> None:
    info = preflight(st, folder)
    src = info["src"]
    rec = st["migrations"].get(src)
    if rec and rec.get("status") == "switched":
        _finish(st, rec, aliases)
        return
    core.say(plan_text(info))
    if info["problems"]:
        raise core.QuietmacError("can't migrate %s" % src)
    if info["busy"] and not ignore_busy:
        raise core.QuietmacError("close the programs above (or stop those dev servers/agents), then retry. "
                                 "--ignore-busy skips this check, but files written during the copy can be lost.")
    dst = info["dst"]
    partial = dst + ".quietmac-partial"
    if os.path.exists(partial):
        core.say("  removing an unfinished copy from an earlier run: " + partial)
        _rmtree(partial)
    rec = {"src": src, "dst": dst, "status": "copying", "started_at": core.now_iso(),
           "files": info["files"], "bytes": info["bytes"]}
    st["migrations"][src] = rec
    core.save_state(st)
    core.log_action("migrate %s -> %s: copying %d files (%s)" % (src, dst, info["files"], core.human_bytes(info["bytes"])))
    t0 = time.time()
    core.say("  copying with ditto (%d files, %s)..." % (info["files"], core.human_bytes(info["bytes"])))
    r = core.run(["/usr/bin/ditto", src, partial])
    if not r.ok:
        _rmtree(partial)
        rec["status"] = "failed"
        core.save_state(st)
        raise core.QuietmacError("ditto failed, original untouched: " + (r.err or r.out).strip())
    core.say("  copied in %.0fs; verifying (%s)..." % (time.time() - t0, verify))
    copy_entries, _, _ = scan(partial)
    problems = compare(info["entries"], copy_entries)
    if not problems and verify == "checksum":
        problems = checksum_compare(src, partial, info["entries"])
    if problems:
        rec["status"] = "verify-failed"
        rec["problems"] = problems
        core.save_state(st)
        raise core.QuietmacError("the copy doesn't match the original, so nothing was switched. The original is "
                                 "untouched; the copy is at %s for inspection.\n  " % partial + "\n  ".join(problems))
    # switch: two renames on two volumes, then a symlink
    os.rename(partial, dst)
    backup = "%s.quietmac-backup-%s" % (src, _stamp())
    try:
        os.rename(src, backup)
    except OSError as e:
        os.rename(dst, partial)  # put things back as they were: original in place, copy unswitched
        rec["status"] = "failed"
        core.save_state(st)
        raise core.QuietmacError("couldn't move the original aside (%s); nothing was switched. The verified copy is "
                                 "at %s." % (e, partial))
    rec.update(status="switched", backup=backup)
    core.save_state(st)
    core.log_action("migrate %s: original renamed to %s" % (src, backup))
    _finish(st, rec, aliases)


def _finish(st: Dict[str, Any], rec: Dict[str, Any], aliases: bool) -> None:
    src, dst = rec["src"], rec["dst"]
    if not os.path.islink(src):
        os.symlink(dst, src)
    made = []
    if aliases:
        for alias, existing in claude_aliases(src, os.path.realpath(dst)):
            os.symlink(existing, str(core.CLAUDE_PROJECTS / alias))
            made.append(alias)
    rec.update(status="done", finished_at=core.now_iso(), claude_aliases=made)
    core.save_state(st)
    core.log_action("migrate %s -> %s: done (symlink in place, %d Claude Code aliases)" % (src, dst, len(made)))
    core.say(core.green("  done: ") + "%s now lives on the dev volume; %s is a link to it. The original is kept at "
             "%s until `quietmac migrate cleanup`." % (os.path.basename(src), src, rec["backup"]))


def rollback(st: Dict[str, Any], folder: str, keep_changes: bool = False) -> None:
    src = os.path.abspath(os.path.expanduser(folder)).rstrip("/")
    rec = st["migrations"].get(src)
    if not rec:
        raise core.QuietmacError("no migration recorded for %s" % src)
    dst, backup = rec["dst"], rec.get("backup")
    status = rec.get("status")
    if status in ("copying", "failed", "verify-failed"):
        partial = dst + ".quietmac-partial"
        if os.path.exists(partial):
            _rmtree(partial)
        st["migrations"].pop(src)
        core.save_state(st)
        core.say("removed the unfinished copy; %s was never switched" % src)
        return
    if status not in ("switched", "done"):
        raise core.QuietmacError("migration of %s is in state %r; nothing to roll back" % (src, status))
    cwd = os.getcwd()
    if inside(cwd, dst) or inside(cwd, src):
        raise core.QuietmacError("`cd ~` first")
    b = busy(dst)
    if b:
        raise core.QuietmacError("these are working inside %s: %s" % (dst, ", ".join("%s (%d)" % (c, p) for p, c, _ in b)))
    backup_ok = bool(backup) and os.path.isdir(backup)
    if not keep_changes and backup_ok:
        old, _, _ = scan(backup)
        new, _, _ = scan(dst)
        diff = compare(old, new, limit=5)
        if diff:
            raise core.QuietmacError("files changed on the dev volume since the migration (%s ...). Use --keep-changes "
                                     "to copy the current files back, or move them yourself." % "; ".join(diff[:3]))
    if keep_changes or not backup_ok:
        restore = src + ".quietmac-restore"
        core.say("copying %s back to %s ..." % (dst, src))
        if os.path.exists(restore):
            _rmtree(restore)
        r = core.run(["/usr/bin/ditto", dst, restore])
        if not r.ok:
            raise core.QuietmacError("ditto failed: " + (r.err or r.out).strip())
        a, _, _ = scan(dst)
        bb, _, _ = scan(restore)
        problems = compare(a, bb)
        if problems:
            raise core.QuietmacError("copy-back didn't verify; nothing switched:\n  " + "\n  ".join(problems))
        if os.path.islink(src):
            os.unlink(src)
        os.rename(restore, src)
    else:
        if os.path.islink(src):
            os.unlink(src)
        os.rename(backup, src)
        rec["backup"] = None
    parked = "%s.rolledback-%s" % (dst, _stamp())
    os.rename(dst, parked)
    for alias in rec.get("claude_aliases", []):
        p = core.CLAUDE_PROJECTS / alias
        if p.is_symlink():
            p.unlink()
    rec.update(status="rolledback", parked=parked, rolledback_at=core.now_iso())
    core.save_state(st)
    core.log_action("rollback %s: restored; volume copy parked at %s" % (src, parked))
    core.say(core.green("rolled back: ") + "%s is a normal folder again. The dev-volume copy is parked at %s "
             "(delete it with `quietmac migrate cleanup`)." % (src, parked))


def leftovers(st: Dict[str, Any]) -> List[Tuple[str, str, Optional[int]]]:
    """(what, path, bytes) of backups and parked copies cleanup can delete."""
    from .detect import du_bytes

    out = []
    for src, rec in st["migrations"].items():
        for key in ("backup", "parked"):
            p = rec.get(key)
            if p and os.path.exists(p) and not (key == "backup" and rec.get("status") not in ("done", "rolledback")):
                out.append(("%s of %s" % (key, src), p, du_bytes(p)))
    return out


def cleanup(st: Dict[str, Any], yes: bool = False) -> None:
    items = leftovers(st)
    if not items:
        core.say("nothing to clean up")
        return
    for what, p, n in items:
        core.say("  %s: %s (%s)" % (what, p, core.human_bytes(n) if n else "?"))
    if not core.confirm("Delete these %d folders permanently?" % len(items), assume_yes=yes):
        return
    for what, p, _ in items:
        _rmtree(p)
        for rec in st["migrations"].values():
            for key in ("backup", "parked"):
                if rec.get(key) == p:
                    rec[key] = None
        core.log_action("cleanup: deleted %s (%s)" % (p, what))
    core.save_state(st)
    core.say(core.green("cleaned up"))
