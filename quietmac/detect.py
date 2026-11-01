"""`quietmac detect`: everything the questions and modules need to know.

All read-only. Nothing here needs admin rights or changes the system.
"""

from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import core, reaper

MANAGED = Path("/Library/Managed Preferences")
WARP_APP = Path("/Applications/Cloudflare WARP.app")
WARP_LABEL = "com.cloudflare.1dot1dot1dot1.macos.warp.daemon"

# Per-user daemons we know how to reap, grouped as the modules group them.
KNOWN_DAEMONS = ["appstoreagent", "mediaanalysisd", "photoanalysisd", "visualintelligenced", "sirittsd",
                 "siriknowledged", "siriappintentsd", "siriactionsd", "duetexpertd"]

# Widget hosts that are really Control Center / system controls: keep them.
CONTROL_HOST_APPS = {"ControlCenter.app", "Dock.app", "WindowManager.app", "WallpaperAgent.app", "Screenshot.app"}


def managed_value(domain: str, key: str) -> Any:
    """Value forced by an installed configuration profile, or None."""
    for p in (MANAGED / core.USER / (domain + ".plist"), MANAGED / (domain + ".plist")):
        d = core.read_plist(p)
        if d is not None and key in d:
            return d[key]
    return None


def _macos() -> Dict[str, Any]:
    model = core.out(["/usr/sbin/sysctl", "-n", "hw.model"])
    laptop = "1" in core.out(["/bin/sh", "-c", "/usr/sbin/ioreg -rc AppleSmartBattery | grep -c BatteryInstalled"])
    hw = core.out(["/usr/sbin/system_profiler", "SPHardwareDataType"], timeout=20)
    name = re.search(r"Model Name: (.*)", hw)
    return {
        "version": platform.mac_ver()[0] or core.out(["/usr/bin/sw_vers", "-productVersion"]),
        "build": core.out(["/usr/bin/sw_vers", "-buildVersion"]),
        "model": model,
        "model_name": name.group(1).strip() if name else "",
        "chip": core.out(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]),
        "cores": int(core.out(["/usr/sbin/sysctl", "-n", "hw.ncpu"]) or 0),
        "laptop": laptop,
        "sip": "enabled" in core.out(["/usr/bin/csrutil", "status"]),
        "filevault": "On" in core.out(["/usr/bin/fdesetup", "status"]),
        "uptime_seconds": int(core.uptime_seconds()),
    }


def _tools() -> Dict[str, Any]:
    clt = core.out(["/usr/bin/xcode-select", "-p"])
    return {
        "clt": clt or None,
        "python": "%d.%d.%d" % tuple(__import__("sys").version_info[:3]),
        "swiftc": core.which("swiftc") if clt else None,
        "brew": core.which("brew"),
        "blueutil": core.which("blueutil"),
        "git": core.which("git") if clt else None,
    }


def _bluetooth(blueutil: Optional[str]) -> Dict[str, Any]:
    power = None
    if blueutil:
        v = core.out([blueutil, "-p"])
        power = v == "1" if v in ("0", "1") else None
    devices: List[Dict[str, Any]] = []
    r = core.run(["/usr/sbin/system_profiler", "SPBluetoothDataType", "-json"], timeout=30)
    try:
        d = json.loads(r.out)["SPBluetoothDataType"][0]
    except (ValueError, KeyError, IndexError):
        d = {}
    state = (d.get("controller_properties") or {}).get("controller_state", "")
    if power is None and state:
        power = state.endswith("_on")
    for key, connected in (("device_connected", True), ("device_not_connected", False)):
        for entry in d.get(key) or []:
            for name, props in entry.items():
                minor = str(props.get("device_minorType", "")).lower()
                devices.append({"name": name, "type": _bt_type(name, minor), "connected": connected})
    return {
        "power": power,
        "devices": devices,
        # macOS only lists paired devices while Bluetooth is on
        "devices_known": bool(power),
        "has_input_device": any(x["type"] in ("keyboard", "mouse", "trackpad") for x in devices),
    }


def _bt_type(name: str, minor: str) -> str:
    n = name.lower()
    for word, typ in (("keyboard", "keyboard"), ("mouse", "mouse"), ("trackpad", "trackpad"),
                      ("headphone", "headphones"), ("headset", "headphones"), ("speaker", "speaker"),
                      ("watch", "watch"), ("phone", "phone"), ("gamepad", "controller")):
        if word in minor:
            return typ
    for word, typ in (("airpods", "headphones"), ("beats", "headphones"), ("watch", "watch"), ("iphone", "phone"),
                      ("ipad", "tablet"), ("keyboard", "keyboard"), ("mouse", "mouse"), ("trackpad", "trackpad"),
                      ("macbook", "computer"), ("imac", "computer"), ("mac ", "computer")):
        if word in n:
            return typ
    return minor or "other"


def _continuity() -> Dict[str, Any]:
    adv = core.defaults_read("com.apple.coreservices.useractivityd", "ActivityAdvertisingAllowed", True)
    rec = core.defaults_read("com.apple.coreservices.useractivityd", "ActivityReceivingAllowed", True)
    air = core.defaults_read("com.apple.sharingd", "DiscoverableMode")
    uc = core.defaults_read("com.apple.universalcontrol", "Disable")
    return {
        # unset means the macOS default, which is on
        "handoff": not (adv and adv[1] == "0" and rec and rec[1] == "0"),
        "airdrop": air[1] if air else "default (Contacts Only)",
        "universal_control": not (uc and uc[1] == "1"),
    }


def _spotlight() -> Dict[str, Any]:
    vols: Dict[str, str] = {}
    cur = None
    for line in core.run(["/usr/bin/mdutil", "-a", "-s"], timeout=20).out.splitlines():
        if line.endswith(":") and line.startswith("/"):
            cur = line[:-1]
        elif cur and line.strip():
            vols[cur] = "enabled" if "enabled" in line and "disabled" not in line else "disabled"
            cur = None
    data = vols.get("/System/Volumes/Data", vols.get("/"))
    return {"volumes": vols, "data_volume_enabled": data == "enabled"}


def pmset_custom() -> Dict[str, Dict[str, str]]:
    sections: Dict[str, Dict[str, str]] = {}
    cur = None
    for line in core.out(["/usr/bin/pmset", "-g", "custom"]).splitlines():
        if line.endswith("Power:"):
            cur = "battery" if line.startswith("Battery") else "ac"
            sections[cur] = {}
        elif cur and line.strip():
            parts = line.split()
            if len(parts) >= 2:
                sections[cur][parts[0]] = parts[1]
    return sections


def _power() -> Dict[str, Any]:
    p = pmset_custom()
    return {"battery": p.get("battery", {}), "ac": p.get("ac", {})}


AI_KEYS = ["allowGenmoji", "allowImagePlayground", "allowWritingTools", "allowMailSummary",
           "allowSafariSummary", "allowExternalIntelligenceIntegrations"]


def _profiles() -> Dict[str, Any]:
    restricted = {k: managed_value("com.apple.applicationaccess", k) for k in
                  AI_KEYS + ["allowAssistant", "allowDiagnosticSubmission", "allowSpotlightInternetResults",
                             "allowApplePersonalizedAdvertising"]}
    names = []
    for d in (MANAGED, MANAGED / core.USER):
        if d.is_dir():
            names.extend(p.stem for p in d.glob("*.plist") if not p.stem.startswith("com.apple")
                         and not p.stem.startswith(".") and p.stem not in ("complete",)
                         and not p.stem.startswith("group.com.apple"))
    return {
        "restrictions": {k: v for k, v in restricted.items() if v is not None},
        "apple_intelligence_restricted": all(restricted[k] is False for k in AI_KEYS)
        or managed_value("com.apple.applicationaccess", "_isAppleIntelligenceRestricted") is True,
        "siri_disabled_by_profile": restricted["allowAssistant"] is False,
        "other_managed_domains": sorted(set(names)),
    }


def _siri() -> Dict[str, Any]:
    forced = managed_value("com.apple.assistant.support", "Assistant Enabled")
    v = core.defaults_read("com.apple.assistant.support", "Assistant Enabled")
    enabled = forced if forced is not None else (v is not None and v[1] == "1")
    return {"enabled": bool(enabled), "forced_by_profile": forced is not None}


def _warp() -> Dict[str, Any]:
    from .modules import warp as warpmod

    installed = WARP_APP.is_dir()
    return {
        "installed": installed,
        "cli": "/usr/local/bin/warp-cli" if os.path.exists("/usr/local/bin/warp-cli") else None,
        "daemon_loaded": core.run(["/bin/launchctl", "print", "system/" + WARP_LABEL]).ok if installed else False,
        "daemon_disabled": core.system_disabled().get(WARP_LABEL, False) if installed else False,
        "watcher_loaded": core.agent_loaded(warpmod.LABEL),
        "legacy_watchers": warpmod.legacy_watchers(),
        "legacy_sudoers": [str(p) for p in warpmod.legacy_sudoers()],
    }


def _daemons() -> Dict[str, Any]:
    procs = {}
    for line in core.run(["/bin/ps", "-axo", "uid=,time=,comm="]).out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] == str(core.UID):
            base = os.path.basename(parts[2])
            if base in KNOWN_DAEMONS or base in ("Siri AI", "siriinferenced", "chronod", "Activity Monitor"):
                procs[base] = round(procs.get(base, 0.0) + core.parse_cputime(parts[1]), 1)
    pid = core.out(["/usr/bin/pgrep", "-x", "sysmond"])
    sysmond_cpu = core.parse_cputime(core.out(["/bin/ps", "-o", "time=", "-p", pid])) if pid else 0.0
    am_open = core.run(["/usr/bin/pgrep", "-x", "Activity Monitor"]).ok
    return {"running": procs, "activity_monitor_open": am_open, "sysmond_cpu_seconds": round(sysmond_cpu, 1)}


def widget_extensions() -> List[Dict[str, str]]:
    """Installed WidgetKit widget extensions inside apps, minus Control Center
    controls. The executable name is what the reaper matches."""
    found = []
    roots = [Path("/System/Applications"), Path("/System/Applications/Utilities"),
             Path("/System/Library/CoreServices"), Path("/Applications")]
    for root in roots:
        if not root.is_dir():
            continue
        for app in sorted(root.glob("*.app")):
            if app.name in CONTROL_HOST_APPS:
                continue
            plugins = app / "Contents" / "PlugIns"
            if not plugins.is_dir():
                continue
            for appex in sorted(plugins.glob("*.appex")):
                info = core.read_plist(appex / "Contents" / "Info.plist") or {}
                if (info.get("NSExtension") or {}).get("NSExtensionPointIdentifier") != "com.apple.widgetkit-extension":
                    continue
                exe = info.get("CFBundleExecutable")
                if not exe or "Control" in exe or exe in reaper.NEVER_KILL:
                    continue
                found.append({"name": exe, "app": app.name[:-4], "path": str(appex)})
    return found


def _widgets() -> Dict[str, Any]:
    installed = widget_extensions()
    running = set(os.path.basename(c) for c in core.out(["/bin/ps", "-axo", "comm="]).splitlines())
    return {"installed": installed, "running": sorted(w["name"] for w in installed if w["name"] in running)}


TOOLCHAINS = {
    "rustup": "~/.rustup", "cargo": "~/.cargo", "go": "~/go", "flutter": "~/flutter",
    "android-sdk": "~/Library/Android/sdk", "bun": "~/.bun", "node (nvm)": "~/.nvm",
    "pub-cache": "~/.pub-cache", "gradle": "~/.gradle", "xcode-derived-data": "~/Library/Developer/Xcode/DerivedData",
}


def project_folders(home: Path = core.HOME, depth: int = 3) -> List[Dict[str, Any]]:
    """Top-level folders in home that contain git repositories."""
    skip = {"Library", "Applications", "Movies", "Music", "Pictures", "Public", ".Trash"}
    counts: Dict[str, int] = {}
    for top in sorted(home.iterdir()) if home.is_dir() else []:
        if top.name in skip or top.name.startswith(".") or not top.is_dir() or top.is_symlink():
            continue
        n = 0
        stack = [(top, 0)]
        while stack:
            d, lvl = stack.pop()
            try:
                entries = list(os.scandir(str(d)))
            except OSError:
                continue
            if any(e.name == ".git" for e in entries):
                n += 1
                if lvl > 0:
                    continue
            if lvl >= depth - 1:
                continue
            for e in entries:
                if e.is_dir(follow_symlinks=False) and e.name not in ("node_modules", ".git", "target", "build"):
                    stack.append((Path(e.path), lvl + 1))
        if n:
            counts[str(top)] = n
    return [{"path": p, "repos": n} for p, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def du_bytes(path: str) -> Optional[int]:
    r = core.run(["/usr/bin/du", "-sk", path], timeout=600)
    try:
        return int(r.out.split()[0]) * 1024
    except (ValueError, IndexError):
        return None


def _dev(sizes: bool) -> Dict[str, Any]:
    from . import cache

    chains = {}
    for name, p in TOOLCHAINS.items():
        full = os.path.expanduser(p)
        if os.path.exists(full):
            chains[name] = {"path": full, "bytes": du_bytes(full) if sizes else None}
    tools = {t: core.which(t) for t in ("node", "npm", "pnpm", "bun", "yarn", "cargo", "go", "flutter", "gradle")}
    projects = project_folders()
    if sizes:
        for p in projects:
            p["bytes"] = du_bytes(p["path"])
    return {"toolchains": chains, "tools": {k: v for k, v in tools.items() if v},
            "projects": projects, "caches": cache.known(sizes=sizes)}


def _fseventsd() -> Dict[str, Any]:
    pid = core.out(["/usr/bin/pgrep", "-x", "fseventsd"])
    cpu = core.parse_cputime(core.out(["/bin/ps", "-o", "time=", "-p", pid])) if pid else 0.0
    up = core.uptime_seconds()
    return {"cpu_seconds": round(cpu, 1), "uptime_seconds": int(up), "percent_of_core": round(100.0 * cpu / up, 2)}


def _timemachine() -> bool:
    r = core.run(["/usr/bin/tmutil", "destinationinfo"])
    return r.ok and "No destinations" not in r.out


def detect(sizes: bool = False) -> Dict[str, Any]:
    from . import devvol

    tools = _tools()
    st = core.load_state()
    return {
        "user": core.USER,
        "home": str(core.HOME),
        "macos": _macos(),
        "tools": tools,
        "bluetooth": _bluetooth(tools["blueutil"]),
        "continuity": _continuity(),
        "spotlight": _spotlight(),
        "power": _power(),
        "profiles": _profiles(),
        "siri": _siri(),
        "warp": _warp(),
        "reaper": dict(reaper.status(), legacy=reaper.legacy_agents()),
        "daemons": _daemons(),
        "widgets": _widgets(),
        "dev": _dev(sizes),
        "devvol": devvol.status(st),
        "fseventsd": _fseventsd(),
        "timemachine": _timemachine(),
        "quietmac": {"applied": sorted(st["modules"].keys()), "state_file": str(core.STATE_FILE)},
    }


def render(f: Dict[str, Any]) -> str:
    m = f["macos"]
    b = f["bluetooth"]
    c = f["continuity"]
    w = f["warp"]
    r = f["reaper"]
    lines = [
        core.bold("This Mac"),
        "  %s (%s), %s, %d cores, macOS %s (%s)%s" % (m["model_name"] or m["model"], m["model"], m["chip"], m["cores"],
                                                     m["version"], m["build"], ", laptop" if m["laptop"] else ""),
        "  SIP %s, FileVault %s, up %.1f h" % ("on" if m["sip"] else "OFF", "on" if m["filevault"] else "off",
                                               m["uptime_seconds"] / 3600.0),
        "  tools: " + ", ".join("%s=%s" % (k, "yes" if v else "no") for k, v in f["tools"].items() if k != "python")
        + ", python " + f["tools"]["python"],
        "",
        core.bold("Wireless"),
        "  Bluetooth: %s; paired devices: %s" % (
            {True: "on", False: "off", None: "unknown"}[b["power"]],
            ", ".join("%s (%s%s)" % (d["name"], d["type"], ", connected" if d["connected"] else "") for d in b["devices"])
            if b["devices"] else ("none" if b["devices_known"] else "unknown while Bluetooth is off")),
        "  Handoff %s, AirDrop %s, Universal Control %s" % (
            "on" if c["handoff"] else "off", c["airdrop"], "on" if c["universal_control"] else "off"),
        "",
        core.bold("System"),
        "  Spotlight indexing on the main volume: %s" % ("on" if f["spotlight"]["data_volume_enabled"] else "off"),
        "  Siri: %s%s" % ("on" if f["siri"]["enabled"] else "off", " (forced by a profile)" if f["siri"]["forced_by_profile"] else ""),
        "  Apple Intelligence restricted by a profile: %s" % ("yes" if f["profiles"]["apple_intelligence_restricted"] else "no"),
        "  battery power settings: %s" % (", ".join("%s=%s" % kv for kv in sorted(f["power"]["battery"].items())
                                                  if kv[0] in ("lowpowermode", "powernap", "displaysleep", "sleep")) or "n/a"),
        "  fseventsd: %.1fs CPU in %.1f h (%.2f%% of a core)" % (
            f["fseventsd"]["cpu_seconds"], f["fseventsd"]["uptime_seconds"] / 3600.0, f["fseventsd"]["percent_of_core"]),
        "  Activity Monitor: %s; its collector sysmond has used %.0f min of CPU since boot%s" % (
            core.yellow("OPEN (sysmond costs ~30% of a core while it is)") if f["daemons"]["activity_monitor_open"]
            else "closed", f["daemons"]["sysmond_cpu_seconds"] / 60.0,
            core.dim(" (that's Activity Monitor being open)") if f["daemons"]["sysmond_cpu_seconds"] > 300 else ""),
        "",
        core.bold("Background daemons"),
        "  reaper: %s%s" % ("running" if r["loaded"] else ("installed, not running" if r["installed"] else "not installed"),
                            (", killing " + ", ".join(r["names"])) if r["names"] else ""),
    ]
    for g in r["gave_up"] + r.get("watching", []):
        lines.append("  " + core.yellow(g))
    for lg in r["legacy"]:
        lines.append("  legacy reaper %s (%s): %s" % (lg["label"], "loaded" if lg["loaded"] else "not loaded",
                                                     ", ".join(lg["names"])))
    running = f["daemons"]["running"]
    lines.append("  known targets running now (CPU s since start): %s" % (
        ", ".join("%s=%s" % kv for kv in sorted(running.items())) or "none"))
    lines.append("  widget extensions installed: %d, running now: %s" % (
        len(f["widgets"]["installed"]), ", ".join(f["widgets"]["running"]) or "none"))
    lines += ["", core.bold("Apps")]
    if w["installed"]:
        lines.append("  Cloudflare WARP: daemon %s%s, on-demand watcher %s" % (
            "loaded" if w["daemon_loaded"] else "not loaded", " (disabled at boot)" if w["daemon_disabled"] else "",
            "running" if w["watcher_loaded"] else "not running"))
        for lw in w["legacy_watchers"]:
            lines.append("  legacy WARP watcher %s (%s)" % (lw["label"], "loaded" if lw["loaded"] else "not loaded"))
    else:
        lines.append("  Cloudflare WARP: not installed")
    d = f["dev"]
    lines += ["", core.bold("Development")]
    lines.append("  toolchains: " + (", ".join(sorted(d["toolchains"])) or "none found"))
    lines.append("  project folders: " + (", ".join("%s (%d repos)" % (os.path.basename(p["path"]), p["repos"])
                                                     for p in d["projects"]) or "none found"))
    dv = f["devvol"]
    lines.append("  dev volume: " + ("%s at %s (%s)" % (dv["name"], dv["mount"], "mounted" if dv["mounted"] else "NOT mounted")
                                     if dv.get("exists") else "none"))
    lines.append("")
    lines.append(core.bold("quietmac") + ": applied modules: " + (", ".join(f["quietmac"]["applied"]) or "none"))
    return "\n".join(lines)
