"""`quietmac measure`: what is using CPU right now, and since boot.

Uses `top`'s power column (Apple's energy-impact score, no sudo needed) and
cumulative CPU from `ps`. Also reports fseventsd's average cost since boot,
which is the number to watch before deciding on a dev volume.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from . import core, reaper


def sample(seconds: int = 30, top_n: int = 20) -> Dict[str, Any]:
    r = core.run(["/usr/bin/top", "-l", "2", "-s", str(max(1, seconds)), "-o", "cpu", "-n", str(top_n),
                  "-stats", "pid,command,cpu,power,user"], timeout=seconds + 60)
    blocks = r.out.split("Processes:")
    last = blocks[-1] if len(blocks) > 1 else r.out
    idle = re.search(r"CPU usage: .*?([\d.]+)% idle", last)
    rows = []
    in_table = False
    for line in last.splitlines():
        if line.startswith("PID"):
            in_table = True
            continue
        if in_table and line.strip():
            parts = line.split()
            if len(parts) >= 5 and parts[0].isdigit():
                # COMMAND can contain spaces: take fixed columns from the right
                rows.append({"pid": int(parts[0]), "command": " ".join(parts[1:-3]), "cpu": float(parts[-3]),
                             "power": float(parts[-2]), "user": parts[-1]})
    return {"seconds": seconds, "idle_percent": float(idle.group(1)) if idle else None, "top": rows}


def since_boot(top_n: int = 20) -> List[Dict[str, Any]]:
    rows = []
    for line in core.run(["/bin/ps", "-axo", "time=,user=,pid=,comm="]).out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append({"cpu_seconds": core.parse_cputime(parts[0]), "user": parts[1], "pid": int(parts[2]),
                         "command": parts[3].rsplit("/", 1)[-1]})
    rows.sort(key=lambda r: -r["cpu_seconds"])
    return rows[:top_n]


def report(seconds: int, top_n: int, boot: bool) -> Dict[str, Any]:
    from .detect import _fseventsd

    res: Dict[str, Any] = {"uptime_seconds": int(core.uptime_seconds()), "fseventsd": _fseventsd()}
    res["sample"] = sample(seconds, top_n)
    if boot:
        res["since_boot"] = since_boot(top_n)
    rs = reaper.status()
    res["reaper"] = {"running": rs["loaded"], "cpu_seconds": rs["cpu_seconds"], "gave_up": rs["gave_up"],
                     "method": rs["method"]}
    res["activity_monitor_open"] = core.run(["/usr/bin/pgrep", "-x", "Activity Monitor"]).ok
    return res


def render(res: Dict[str, Any]) -> str:
    s = res["sample"]
    lines = [core.bold("CPU over the last %ds: %s%% idle" % (s["seconds"], s["idle_percent"]))
             + core.dim("  (100% = every core idle; one busy core on a 16-core Mac is ~6%)"),
             "  %-7s %-28s %6s %6s  %s" % ("PID", "COMMAND", "%CPU", "POWER", "USER")]
    for r in s["top"]:
        lines.append("  %-7d %-28s %6.1f %6.1f  %s" % (r["pid"], r["command"][:28], r["cpu"], r["power"], r["user"]))
    f = res["fseventsd"]
    lines.append("")
    lines.append("fseventsd since boot: %.1fs CPU in %.1f h = %.2f%% of one core%s" % (
        f["cpu_seconds"], f["uptime_seconds"] / 3600.0, f["percent_of_core"],
        core.yellow("  <- high: a dev volume would help") if f["percent_of_core"] >= 2 else ""))
    rp = res["reaper"]
    if rp["running"]:
        lines.append("reaper: running (%s), %ss CPU so far" % (rp["method"] or "?", rp["cpu_seconds"]))
    for g in rp["gave_up"]:
        lines.append(core.yellow("  " + g))
    if res["activity_monitor_open"]:
        lines.append(core.yellow("Activity Monitor is open: its data collector sysmond costs ~30% of a core. Quit it "
                                 "when you're done."))
    if res.get("since_boot"):
        lines.append("")
        lines.append(core.bold("CPU since boot (mostly one-time startup work):"))
        for r in res["since_boot"]:
            lines.append("  %8.1fs  %-12s %s" % (r["cpu_seconds"], r["user"][:12], r["command"]))
    return "\n".join(lines)
