"""`quietmac battery`: battery drain history from the power-management log.

`pmset -g log` (readable without root) records the charge level at every
sleep, wake, dark wake and power-assertion change, for about the last week.
From consecutive readings taken on battery we get, per day and per unplugged
session:

  * awake drain: % of a full charge per hour, and watts
  * asleep drain: % per hour while sleeping on battery
  * hours a full charge lasts at that rate

Charge readings are whole percents, so short stretches are noisy; totals over
hours are reliable. Watts use the battery's current full-charge capacity
(NominalChargeCapacity x ~11.5 V); pass --capacity-wh to override.
"""

from __future__ import annotations

import collections
import datetime as dt
import plistlib
import re
from typing import Any, Dict, List, Optional, Tuple

from . import core

RX_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) [+-]\d{4} (\S+)\s+(.*)$")
RX_CHARGE = re.compile(r"Using (AC|Batt|BATT)\s*\(Charge:\s*(\d+)%?\)")
RX_WAKE_REQ = re.compile(r"\[\*?process=([^ ]+) request=(\S+).*?info=\"([^\"]*)\"")


def battery_info() -> Dict[str, Any]:
    r = core.run(["/usr/sbin/ioreg", "-rn", "AppleSmartBattery", "-a"])
    try:
        d = plistlib.loads(r.out.encode())[0]
    except Exception:
        return {}
    amps = d.get("InstantAmperage", 0)
    amps = amps - 2 ** 64 if amps >= 2 ** 63 else amps
    bd = d.get("BatteryData") or {}
    nominal = bd.get("NominalChargeCapacity") or d.get("NominalChargeCapacity") or d.get("AppleRawMaxCapacity")
    design = bd.get("DesignCapacity") or d.get("DesignCapacity")
    return {
        "on_battery": not d.get("ExternalConnected", True),
        "charge_percent": d.get("CurrentCapacity"),
        "cycle_count": d.get("CycleCount"),
        "design_mah": design,
        "full_charge_mah": nominal,
        "health_percent": round(100.0 * nominal / design) if nominal and design else None,
        "capacity_wh": round(nominal * 11.5 / 1000.0, 1) if nominal else None,
        "draw_watts_now": round(-amps * d.get("Voltage", 0) / 1e6, 2) if not d.get("ExternalConnected", True) else None,
    }


def parse(text: str) -> List[Tuple[dt.datetime, Optional[str], Optional[str], Optional[int], str, str]]:
    pts = []
    for line in text.splitlines():
        m = RX_LINE.match(line)
        if not m:
            continue
        t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        cat, rest = m.group(2), m.group(3)
        st = None
        if cat == "Sleep" and rest.startswith("Entering Sleep"):
            st = "asleep"
        elif cat == "Wake" and (rest.startswith("Wake from") or rest.startswith("DarkWake to FullWake")):
            st = "awake"  # note: "Wake Requests" lines are also category Wake, and are not wakes
        elif cat == "DarkWake" and "to FullWake" in rest:
            st = "awake"
        elif cat == "Start":
            st = "awake"
        c = RX_CHARGE.search(rest)
        src = ("AC" if c.group(1) == "AC" else "Batt") if c else None
        pts.append((t, st, src, int(c.group(2)) if c else None, cat, rest))
    pts.sort(key=lambda p: p[0])
    return pts


def analyze(text: str) -> Dict[str, Any]:
    pts = parse(text)
    days: Dict[str, Dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
    sessions: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    state, last = "awake", None
    wake_requests: Dict[str, int] = collections.Counter()
    darkwakes: Dict[str, int] = collections.Counter()

    def add(t0: dt.datetime, t1: dt.datetime, s: str, pct: float) -> None:
        total = (t1 - t0).total_seconds()
        while t0 < t1:
            nxt = min(t1, dt.datetime.combine(t0.date() + dt.timedelta(days=1), dt.time()))
            f = (nxt - t0).total_seconds() / total
            d = days[t0.date().isoformat()]
            d[s + "_h"] += (nxt - t0).total_seconds() / 3600.0
            d[s + "_pct"] += pct * f
            t0 = nxt

    for t, st, src, chg, cat, rest in pts:
        if cat == "Wake" and rest.startswith("Requests"):
            m = RX_WAKE_REQ.search(rest)
            if m:
                who = m.group(3).split(":")[-1] if m.group(3).startswith("com.apple.dasd") else m.group(1)
                wake_requests[who] += 1
        if cat == "DarkWake" and " due to " in rest:
            darkwakes[rest.split(" due to ", 1)[1].split("/")[0].split(" Using")[0].strip()[:60]] += 1
        if src is not None and last is not None:
            t0, s0, src0, c0 = last
            span = (t - t0).total_seconds()
            if src0 == "Batt" and src == "Batt" and 0 < span < 12 * 3600:
                add(t0, t, s0, c0 - chg)
                if cur is None:
                    cur = {"start": t0, "start_percent": c0, "awake_h": 0.0, "awake_pct": 0.0, "asleep_h": 0.0, "asleep_pct": 0.0}
                cur["end"], cur["end_percent"] = t, chg
                cur[s0 + "_h"] += span / 3600.0
                cur[s0 + "_pct"] += c0 - chg
            elif cur is not None:
                sessions.append(cur)
                cur = None
        if st:
            state = st
        if src is not None:
            last = (t, state, src, chg)
    if cur:
        sessions.append(cur)
    return {"days": {k: dict(v) for k, v in sorted(days.items())}, "sessions": sessions,
            "first": pts[0][0] if pts else None, "last": pts[-1][0] if pts else None,
            "wake_requests": wake_requests.most_common(8), "darkwake_reasons": darkwakes.most_common(6)}


def rate(pct: float, hours: float) -> Optional[float]:
    return pct / hours if hours > 0.3 else None


def report(capacity_wh: Optional[float] = None) -> Dict[str, Any]:
    info = battery_info()
    cap = capacity_wh or info.get("capacity_wh") or 60.0
    r = core.run(["/usr/bin/pmset", "-g", "log"], timeout=120)
    a = analyze(r.out)
    tot = collections.defaultdict(float)
    for d in a["days"].values():
        for k, v in d.items():
            tot[k] += v
    awake_rate = rate(tot["awake_pct"], tot["awake_h"])
    asleep_rate = rate(tot["asleep_pct"], tot["asleep_h"])
    return {"battery": info, "capacity_wh_used": cap, "analysis": a, "totals": dict(tot),
            "awake_percent_per_hour": awake_rate, "awake_watts": awake_rate * cap / 100 if awake_rate else None,
            "hours_per_charge_awake": 100.0 / awake_rate if awake_rate else None,
            "asleep_percent_per_hour": asleep_rate}


def render(res: Dict[str, Any]) -> str:
    cap = res["capacity_wh_used"]
    b = res["battery"]
    a = res["analysis"]
    w = lambda pct, h: ("%.1f W" % (pct / h * cap / 100)) if h > 0.3 else "-"
    if not b:
        return core.bold("Battery") + ": none found (a desktop Mac, or a virtual machine)"
    lines = [core.bold("Battery") + ": %s%% charged, %s cycles, health %s%% (%s of %s mAh) = ~%.0f Wh%s" % (
        b.get("charge_percent"), b.get("cycle_count"), b.get("health_percent"), b.get("full_charge_mah"),
        b.get("design_mah"), cap, (", drawing %.1f W now" % b["draw_watts_now"]) if b.get("draw_watts_now") else ", plugged in")]
    if a["first"]:
        lines.append(core.dim("power log covers %s to %s" % (a["first"].strftime("%a %b %d %H:%M"), a["last"].strftime("%a %b %d %H:%M"))))
    lines += ["", core.bold("On battery, per day") + "          awake: hours  used  %/hour   watts | asleep: hours  used  %/hour"]
    for day, d in a["days"].items():
        ah, ap, sh, sp = d.get("awake_h", 0), d.get("awake_pct", 0), d.get("asleep_h", 0), d.get("asleep_pct", 0)
        if ah + sh < 0.05:
            continue
        lines.append("  %-26s %8.1f %5.0f%% %7s %7s | %13.1f %4.0f%% %7s" % (
            dt.date.fromisoformat(day).strftime("%a %b %d"), ah, ap, "%.1f" % (ap / ah) if ah > .3 else "-", w(ap, ah),
            sh, sp, "%.2f" % (sp / sh) if sh > .3 else "-"))
    t = res["totals"]
    if res["awake_percent_per_hour"]:
        lines.append("  %-26s %8.1f %5.0f%% %7.1f %7s | %13.1f %4.0f%% %7s" % (
            "total", t["awake_h"], t["awake_pct"], res["awake_percent_per_hour"], w(t["awake_pct"], t["awake_h"]),
            t.get("asleep_h", 0), t.get("asleep_pct", 0), "%.2f" % res["asleep_percent_per_hour"] if res["asleep_percent_per_hour"] else "-"))
        lines.append("  => awake on battery, a full charge lasts ~%.1f h at this rate" % res["hours_per_charge_awake"])
    lines += ["", core.bold("Unplugged sessions") + " (20+ minutes awake)"]
    for s in a["sessions"]:
        if s["awake_h"] < 0.33:
            continue
        lines.append("  %s-%s  %3d%% -> %3d%%  awake %.1f h, %3.0f%% used = %4.1f %%/h (%s)%s" % (
            s["start"].strftime("%a %b %d %H:%M"), s["end"].strftime("%H:%M"), s["start_percent"], s["end_percent"],
            s["awake_h"], s["awake_pct"], s["awake_pct"] / s["awake_h"], w(s["awake_pct"], s["awake_h"]),
            ("; asleep %.1f h, %.0f%%" % (s["asleep_h"], s["asleep_pct"])) if s["asleep_h"] > 0.1 else ""))
    if a["wake_requests"]:
        lines += ["", core.bold("Who schedules wakes from sleep") + " (count over the log)"]
        for who, n in a["wake_requests"]:
            lines.append("  %5d  %s" % (n, who))
    return "\n".join(lines)
