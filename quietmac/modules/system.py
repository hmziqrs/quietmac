"""System-wide switches that need the admin password: Spotlight and power."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import core
from ..core import Step
from .base import APPLIED, NA, NOT_APPLIED, PARTIAL, Context, Module


class SpotlightModule(Module):
    id = "spotlight-off"
    title = "Turn off Spotlight indexing"
    category = "System"
    admin = True
    summary = "runs `mdutil -a -i off`: no more indexing of any mounted volume (mds, mds_stores, mdworker go quiet)"
    benefit = ("indexing is the classic CPU and disk hog when tools churn files: every npm/bun/cargo install and "
               "build gets indexed")
    cost = ("Spotlight file search, Finder search, Mail search and apps that use the Spotlight index (e.g. Alfred). "
            "Launching apps by name keeps working in most launchers. To keep search but skip code, use the dev "
            "volume instead (indexing is off only there).")

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        vols = facts["spotlight"]["volumes"]
        on = [v for v, s in vols.items() if s == "enabled"]
        if not vols:
            return NA, "mdutil gave no answer"
        return (APPLIED, "") if not on else ((PARTIAL if len(on) < len(vols) else NOT_APPLIED),
                                             "indexing on: " + ", ".join(on))

    def apply_steps(self, ctx: Context) -> List[Step]:
        was_on = [v for v, s in ctx.facts["spotlight"]["volumes"].items() if s == "enabled"]
        return [Step("turn indexing off on all volumes", script="/usr/bin/mdutil -a -i off >/dev/null"),
                self.record_step(ctx, previously_on=was_on)]

    def revert_steps(self, ctx: Context) -> List[Step]:
        vols = ctx.record(self.id).get("previously_on") or ["/", "/System/Volumes/Data"]
        script = "\n".join("/usr/bin/mdutil -i on %s >/dev/null || true" % core.sh_quote(v) for v in vols)
        return [Step("turn indexing back on for %s" % ", ".join(vols), script=script), self.forget_step(ctx)]


class PmsetModule(Module):
    category = "Power (battery)"
    admin = True

    def __init__(self, mid: str, title: str, key: str, value: str, summary: str, benefit: str, cost: str,
                 option: str = ""):
        self.id, self.title, self.key, self.value = mid, title, key, value
        self.summary, self.benefit, self.cost = summary, benefit, cost
        self.options = {"value": option} if option else {}

    def applicable(self, facts: Dict[str, Any]) -> Tuple[bool, str]:
        return (facts["macos"]["laptop"], "only for Macs with a battery")

    def target(self, ctx_or_none: Any = None) -> str:
        if ctx_or_none is not None and self.options:
            return str(ctx_or_none.opt("value", self.value))
        return self.value

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        cur = facts["power"]["battery"].get(self.key)
        if cur is None:
            cur = "0" if self.key == "lowpowermode" else None
        if cur is None:
            return NA, "pmset doesn't report %s on this Mac" % self.key
        want = st["modules"].get(self.id, {}).get("value", self.value)
        return (APPLIED if cur == str(want) else NOT_APPLIED), "battery %s is %s" % (self.key, cur)

    def apply_steps(self, ctx: Context) -> List[Step]:
        cur = ctx.facts["power"]["battery"].get(self.key, "0" if self.key == "lowpowermode" else None)
        val = self.target(ctx)
        if not val.isdigit():
            raise core.QuietmacError("%s: value must be a whole number" % self.id)
        return [Step("pmset -b %s %s (on battery only)" % (self.key, val),
                     script="/usr/bin/pmset -b %s %s" % (self.key, val)),
                self.record_step(ctx, previous=cur, value=val)]

    def revert_steps(self, ctx: Context) -> List[Step]:
        prev = ctx.record(self.id).get("previous")
        steps = []
        if prev is not None and str(prev).isdigit():
            steps.append(Step("restore pmset -b %s %s" % (self.key, prev), script="/usr/bin/pmset -b %s %s" % (self.key, prev)))
        steps.append(self.forget_step(ctx))
        return steps


MODULES = [
    SpotlightModule(),
    PmsetModule(
        "low-power-on-battery", "Low Power Mode on battery", "lowpowermode", "1",
        "turns on Low Power Mode whenever the Mac runs on battery",
        "the biggest single battery win on Apple silicon: lower clocks and fewer background wakeups",
        "some peak performance on battery (builds and renders are slower unplugged)"),
    PmsetModule(
        "powernap-off-on-battery", "Power Nap off on battery", "powernap", "0",
        "stops Power Nap (mail, iCloud, backups while asleep) when on battery",
        "fewer wakeups while the lid is closed",
        "mail/iCloud/Time Machine don't update while asleep on battery"),
    PmsetModule(
        "display-sleep-on-battery", "Turn the display off when idle on battery", "displaysleep", "5",
        "turns the display off after N minutes of inactivity on battery (default 5)",
        "the screen is the biggest power draw; `displaysleep 0` means it never turns off on battery",
        "the screen turns off when you're reading without touching anything",
        option="minutes of inactivity before the display turns off (default 5)"),
]
