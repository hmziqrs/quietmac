"""Managing the reaper LaunchAgent, its kill list, and `probe`.

The kill list lives in CONFIG_DIR/reaper.conf and is generated from state:
every applied reap-* module contributes its names, plus any custom names added
with `quietmac reaper add`. The running reaper re-reads the file when it
changes, so editing the list never needs a restart.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import core
from .core import Step

LABEL = core.LABEL_PREFIX + ".reaper"
PLIST = core.AGENTS_DIR / (LABEL + ".plist")
CONF = core.CONFIG_DIR / "reaper.conf"
SCRIPT = core.SHARE_DIR / "bin" / "daemon-reaper.py"
LOG = core.LOG_DIR / "reaper.log"
PYTHON = "/usr/bin/python3"

# Same list as payload/daemon-reaper.py (a test keeps them identical). The
# reaper itself also refuses these; checking here gives a clear error early.
NEVER_KILL = frozenset("""
launchd loginwindow WindowServer Finder Dock SystemUIServer ControlCenter
NotificationCenter WindowManager chronod fseventsd cfprefsd distnoted
usernoted UserEventAgent lsd trustd secd securityd tccd sharedfilelistd
siriinferenced talagentd coreservicesd useractivityd
StartupDiskWidgetExtension BluetoothSettingsAppIntentsWidgetExtension
Python python3 python3.9 bash zsh sh login sudo
""".split()) | frozenset(["Siri AI"])

WHY_NEVER = {
    "chronod": "the widget host; launchd relaunches it about every 5 s, so killing it is a respawn loop",
    "Siri AI": "launchd keeps it alive (label com.apple.campo); each relaunch drags siriinferenced "
               "and mediaanalysisd back, so killing it costs more CPU than it saves",
    "siriinferenced": "the Siri AI app keeps calling it; killing it was a respawn loop",
    "fseventsd": "root-owned, and every file watcher and Spotlight depend on it",
    "StartupDiskWidgetExtension": "backs a Control Center control",
    "BluetoothSettingsAppIntentsWidgetExtension": "backs the Bluetooth toggle in Control Center",
}


def names_from_state(st: Dict[str, Any]) -> List[str]:
    names = set()
    for group_names in st["reaper"]["groups"].values():
        names.update(group_names)
    names.update(st["reaper"]["custom"])
    return sorted(names)


def render_conf(st: Dict[str, Any]) -> str:
    lines = ["# quietmac reaper kill list. Generated; edit with `quietmac reaper add/remove`",
             "# or by applying/reverting reap-* modules. One executable name per line.", ""]
    for gid, names in sorted(st["reaper"]["groups"].items()):
        lines.append("# module %s" % gid)
        lines.extend(sorted(names))
        lines.append("")
    if st["reaper"]["custom"]:
        lines.append("# added with `quietmac reaper add`")
        lines.extend(sorted(st["reaper"]["custom"]))
        lines.append("")
    return "\n".join(lines)


def write_conf(st: Dict[str, Any]) -> None:
    core.atomic_write(CONF, render_conf(st))


def plist_data() -> Dict[str, Any]:
    return core.agent_plist(
        LABEL, [PYTHON, "-E", "-s", str(SCRIPT)],
        env={"REAPER_CONF": str(CONF), "REAPER_LOG": str(LOG), "REAPER_POLL": "5"},
        log_name="reaper")


def _install_files() -> None:
    SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(core.PAYLOAD_DIR / "daemon-reaper.py"), str(SCRIPT))
    os.chmod(str(SCRIPT), 0o755)
    core.write_plist(PLIST, plist_data())
    core.log_action("installed %s and %s" % (SCRIPT, PLIST))


def agent_current() -> bool:
    """True if the installed agent matches this checkout (script + plist)."""
    try:
        same_script = SCRIPT.read_bytes() == (core.PAYLOAD_DIR / "daemon-reaper.py").read_bytes()
    except OSError:
        return False
    return same_script and core.read_plist(PLIST) == plist_data()


def ensure_agent_steps() -> List[Step]:
    """Steps that install/refresh and (re)load the reaper agent."""
    steps = []
    loaded = core.agent_loaded(LABEL)
    if loaded and agent_current():
        return steps
    steps.append(Step("install reaper script and LaunchAgent (%s)" % LABEL, fn=_install_files))
    if loaded:
        steps.append(Step("reload the reaper", argv=["/bin/launchctl", "bootout", core.gui(LABEL)], allow_fail=True))
    steps.append(Step("start the reaper", fn=_bootstrap))
    return steps


def _bootstrap() -> None:
    r = core.launchctl("bootstrap", "gui/%d" % core.UID, str(PLIST))
    if not r.ok and "already" not in (r.err + r.out).lower():
        # launchd can need a moment after a bootout
        time.sleep(1)
        r = core.launchctl("bootstrap", "gui/%d" % core.UID, str(PLIST))
        if not r.ok:
            raise core.StepError("launchctl bootstrap %s failed: %s" % (LABEL, (r.err or r.out).strip()))


def remove_agent_steps() -> List[Step]:
    steps = []
    if core.agent_loaded(LABEL):
        steps.append(Step("stop the reaper", argv=["/bin/launchctl", "bootout", core.gui(LABEL)], allow_fail=True))
    if PLIST.exists() or SCRIPT.exists():
        def rm() -> None:
            for p in (PLIST, SCRIPT, CONF):
                if p.exists():
                    p.unlink()
            core.log_action("removed reaper agent, script and config")
        steps.append(Step("remove reaper LaunchAgent, script and kill list", fn=rm))
    return steps


def check_name(name: str) -> Optional[str]:
    """Why a name must not be reaped, or None."""
    if name in NEVER_KILL:
        return "%s is on the never-kill list: %s" % (name, WHY_NEVER.get(name, "the session depends on it"))
    if "/" in name or not name.strip():
        return "give the executable's file name only (e.g. mediaanalysisd), not a path"
    for pid, user, comm in processes_named(name):
        if user != core.USER:
            return "%s runs as %s, not as you; the reaper only touches your own processes" % (name, user)
    return None


def processes_named(name: str) -> List[Tuple[int, str, str]]:
    r = core.run(["/bin/ps", "-axo", "pid=,user=,comm="])
    found = []
    for line in r.out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and os.path.basename(parts[2]) == name:
            found.append((int(parts[0]), parts[1], parts[2]))
    return found


# --------------------------------------------------------------------------
# status


def log_since_start() -> List[str]:
    try:
        lines = LOG.read_text(errors="replace").splitlines()
    except OSError:
        return []
    start = 0
    for i, l in enumerate(lines):
        if " reaper started " in l:
            start = i
    return lines[start:]


def status() -> Dict[str, Any]:
    pid = core.agent_pid(LABEL) if not core.SANDBOX else None
    lines = log_since_start()
    cpu = None
    if pid:
        cpu = core.parse_cputime(core.out(["/bin/ps", "-o", "time=", "-p", str(pid)]))
    return {
        "label": LABEL,
        "installed": PLIST.exists(),
        "loaded": core.agent_loaded(LABEL),
        "current": agent_current(),
        "pid": pid,
        "cpu_seconds": cpu,
        "names": sorted(_read_conf_names()),
        "gave_up": [l for l in lines if "GAVE UP" in l],
        "watching": [l for l in lines if " CHURN on " in l],
        "method": next((m.group(1) for m in (re.search(r"method=(\w+)", l) for l in lines) if m), None),
        "log": str(LOG),
    }


def _read_conf_names() -> List[str]:
    try:
        return [l.split("#", 1)[0].strip() for l in CONF.read_text().splitlines()
                if l.split("#", 1)[0].strip()]
    except OSError:
        return []


# --------------------------------------------------------------------------
# legacy (hand-made) reapers from before quietmac, e.g. com.<you>.daemon-reaper


def legacy_agents() -> List[Dict[str, Any]]:
    found = []
    if not core.AGENTS_DIR.is_dir():
        return found
    for p in sorted(core.AGENTS_DIR.glob("*.plist")):
        d = core.read_plist(p) or {}
        label = d.get("Label", "")
        if label == LABEL:
            continue
        args = d.get("ProgramArguments") or []
        if any(os.path.basename(str(a)) == "daemon-reaper" for a in args):
            env = d.get("EnvironmentVariables") or {}
            names = [n.strip() for n in str(env.get("REAPER_NAMES", "appstoreagent,mediaanalysisd")).split(",") if n.strip()]
            found.append({"label": label, "plist": str(p), "names": names,
                          "script": next((str(a) for a in args if os.path.basename(str(a)) == "daemon-reaper"), None),
                          "loaded": core.agent_loaded(label)})
    return found


# --------------------------------------------------------------------------
# probe: would this daemon stay dead if we reaped it?


def probe(name: str, seconds: int = 90) -> Dict[str, Any]:
    """Kill every instance of `name` for `seconds`, the way the reaper would,
    and count how often it comes back. Prints progress; returns a verdict."""
    problem = check_name(name)
    if problem:
        raise core.QuietmacError(problem)
    procs = [p for p in processes_named(name) if p[1] == core.USER]
    if not procs:
        raise core.QuietmacError("%s isn't running right now, so it can't be probed. Many daemons start on "
                                 "demand; try again after using the Mac for a while, or after a reboot." % name)
    services = core.gui_services()
    label = next((services[p[0]] for p in procs if p[0] in services), None)
    runs0 = core.launchd_job_field(core.gui(label), "runs") if label else None
    launchd0 = core.parse_cputime(core.out(["/bin/ps", "-o", "time=", "-p", "1"]))
    core.say("probing %s (launchd label %s) for %ds; it will be killed on sight, like the reaper does" % (
        name, label or "unknown", seconds))
    seen = set()
    respawns = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        for pid, user, _ in processes_named(name):
            if user != core.USER or pid in seen:
                continue
            if seen:
                respawns.append(round(time.time() - t0, 1))
                core.say("  %5.1fs  came back as pid %d" % (time.time() - t0, pid))
            seen.add(pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1)
    runs1 = core.launchd_job_field(core.gui(label), "runs") if label else None
    launchd1 = core.parse_cputime(core.out(["/bin/ps", "-o", "time=", "-p", "1"]))
    n = len(respawns)
    per_10min = n * 600.0 / seconds
    if n == 0:
        verdict, ok = "stayed dead: safe to reap", True
    elif per_10min < 10:
        verdict, ok = ("came back %d time(s) in %ds; OK to reap (the churn guard stops at 10 kills per "
                       "10 minutes), but watch `quietmac reaper status` for GAVE UP" % (n, seconds)), True
    else:
        verdict, ok = ("respawn loop: came back %d times in %ds; something keeps relaunching it. If you add it "
                       "anyway, the reaper switches it to watch mode after 10 kills (it may run while idle and is "
                       "killed only when it burns CPU). Add it only if it uses real CPU while alive." % (n, seconds)), False
    result = {"name": name, "label": label, "seconds": seconds, "respawns": respawns, "ok": ok,
              "verdict": verdict, "launchd_runs": [runs0, runs1],
              "launchd_cpu_delta": round(launchd1 - launchd0, 2)}
    core.log_action("probe %s: %s" % (name, verdict))
    return result
