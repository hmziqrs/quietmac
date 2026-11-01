"""Moving package and build caches onto the dev volume.

Why some caches *must* follow the projects: bun installs by cloning files
from its cache into node_modules, and pnpm hard-links from its store. Clones
and hard links only work within one volume; across volumes both silently fall
back to full copies (slower installs, more disk, more writes).
Others (Go build cache, Gradle, Xcode DerivedData) are just written on every
build, so they belong with the rest of the churn.

Toolchains themselves (rustup, Go, Flutter and Android SDKs, Homebrew) stay
on the main volume: they're written once at install/update and then only
read, and reads cost fseventsd nothing.

Mechanism: the cache folder is copied to <devvol>/.caches/<name> (or dropped
with --drop, since caches can be rebuilt), and the old path becomes a
symlink. `quietmac doctor` warns if a tool later replaces the symlink with a
real folder.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import core, devvol, migrate


def _bun() -> Optional[str]:
    return os.environ.get("BUN_INSTALL_CACHE_DIR") or os.path.expanduser("~/.bun/install/cache")


def _pnpm() -> Optional[str]:
    if not core.which("pnpm"):
        return None
    p = core.out([core.which("pnpm") or "pnpm", "store", "path"], timeout=30)
    return p or None


def _gocache() -> Optional[str]:
    go = core.which("go")
    p = core.out([go, "env", "GOCACHE"], timeout=30) if go else ""
    return p or (os.path.expanduser("~/Library/Caches/go-build") if go else None)


CACHES: Dict[str, Dict[str, Any]] = {
    "bun": {"path": _bun, "busy": ["bun"], "required": True,
            "why": "bun clones packages from this cache; clones only work on the same volume as node_modules"},
    "pnpm": {"path": _pnpm, "busy": ["pnpm"], "required": True,
             "why": "pnpm hard-links from its store; hard links only work on the same volume"},
    "go-build": {"path": _gocache, "busy": ["go"], "required": False,
                 "why": "written on every Go build"},
    "gradle": {"path": lambda: os.path.expanduser("~/.gradle/caches"), "busy": ["java"], "required": False,
               "why": "Gradle/Android builds write here constantly"},
    "xcode-derived-data": {"path": lambda: os.path.expanduser("~/Library/Developer/Xcode/DerivedData"),
                           "busy": ["Xcode", "xcodebuild"], "required": False,
                           "why": "Xcode and Flutter iOS/macOS builds write here"},
    "npm": {"path": lambda: os.path.expanduser("~/.npm/_cacache"), "busy": ["npm"], "required": False,
            "why": "small gain: npm only writes here when downloading"},
}


def known(sizes: bool = False) -> Dict[str, Any]:
    from .detect import du_bytes

    st = core.load_state()
    res = {}
    for name, c in CACHES.items():
        path = c["path"]()
        if not path:
            continue
        exists = os.path.exists(path)
        res[name] = {"path": path, "exists": exists, "moved": st["caches"].get(name, {}).get("status") == "moved",
                     "symlink": os.path.islink(path), "required_with_devvol": c["required"], "why": c["why"],
                     "bytes": du_bytes(path) if sizes and exists else None}
    return res


def _running(names: List[str]) -> List[str]:
    found = []
    for n in names:
        if core.run(["/usr/bin/pgrep", "-x", "-u", core.USER, n]).ok:
            found.append(n)
    if "java" in names and core.run(["/usr/bin/pgrep", "-f", "-u", core.USER, "GradleDaemon"]).ok:
        found.append("GradleDaemon (run `gradle --stop`)")
    return [f for f in found if f != "java"]


def move(st: Dict[str, Any], name: str, drop: bool = False) -> None:
    if name not in CACHES:
        raise core.QuietmacError("unknown cache %s; known: %s" % (name, ", ".join(CACHES)))
    dv = st.get("devvol")
    if not dv or not devvol.is_mounted_at(dv["mount"], dv["uuid"]):
        raise core.QuietmacError("the dev volume isn't set up and mounted")
    src = CACHES[name]["path"]()
    if not src:
        raise core.QuietmacError("%s isn't installed" % name)
    if os.path.islink(src):
        raise core.QuietmacError("%s is already a symlink (-> %s)" % (src, os.path.realpath(src)))
    busy = _running(CACHES[name]["busy"])
    if busy:
        raise core.QuietmacError("stop these first: " + ", ".join(busy))
    dst = os.path.join(dv["mount"], ".caches", name)
    if os.path.exists(dst):
        raise core.QuietmacError("%s already exists" % dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src) and os.listdir(src) and not drop:
        core.say("copying %s -> %s ..." % (src, dst))
        r = core.run(["/usr/bin/ditto", src, dst])
        if not r.ok:
            migrate._rmtree(dst)
            raise core.QuietmacError("ditto failed: " + (r.err or r.out).strip())
        a, _, _ = migrate.scan(src)
        b, _, _ = migrate.scan(dst)
        problems = migrate.compare(a, b)
        if problems:
            migrate._rmtree(dst)
            raise core.QuietmacError("copy didn't verify; nothing changed:\n  " + "\n  ".join(problems))
    else:
        os.makedirs(dst, exist_ok=True)
    old = src + ".quietmac-old"
    if os.path.exists(src):
        os.rename(src, old)
    os.makedirs(os.path.dirname(src), exist_ok=True)
    os.symlink(dst, src)
    if os.path.exists(old):
        migrate._rmtree(old)
    st["caches"][name] = {"status": "moved", "src": src, "dst": dst, "moved_at": core.now_iso()}
    core.save_state(st)
    core.log_action("cache %s: %s -> %s%s" % (name, src, dst, " (dropped old contents)" if drop else ""))
    core.say(core.green("moved: ") + "%s -> %s" % (src, dst))


def restore(st: Dict[str, Any], name: str, drop: bool = False) -> None:
    rec = st["caches"].get(name)
    if not rec or rec.get("status") != "moved":
        raise core.QuietmacError("%s wasn't moved by quietmac" % name)
    src, dst = rec["src"], rec["dst"]
    busy = _running(CACHES.get(name, {}).get("busy", []))
    if busy:
        raise core.QuietmacError("stop these first: " + ", ".join(busy))
    if os.path.islink(src):
        os.unlink(src)
    elif os.path.exists(src):
        raise core.QuietmacError("%s is no longer our symlink; leaving it alone" % src)
    if os.path.isdir(dst) and not drop:
        r = core.run(["/usr/bin/ditto", dst, src])
        if not r.ok:
            os.symlink(dst, src)
            raise core.QuietmacError("ditto failed; symlink put back: " + (r.err or r.out).strip())
    else:
        os.makedirs(src, exist_ok=True)
    if os.path.isdir(dst):
        migrate._rmtree(dst)
    st["caches"].pop(name, None)
    core.save_state(st)
    core.log_action("cache %s restored to %s" % (name, src))
    core.say(core.green("restored: ") + src)


def drift(st: Dict[str, Any]) -> List[str]:
    """Caches whose symlink was replaced (e.g. a tool recreated the folder)."""
    bad = []
    for name, rec in st["caches"].items():
        if rec.get("status") == "moved" and not os.path.islink(rec["src"]):
            bad.append("%s: %s is a real folder again (a tool recreated it). Re-link it: rm -rf %s && ln -s %s %s"
                       % (name, rec["src"], core.sh_quote(rec["src"]), core.sh_quote(rec["dst"]),
                          core.sh_quote(rec["src"])))
    return bad
