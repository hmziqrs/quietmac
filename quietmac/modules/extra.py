"""Guided items (done by hand, quietmac explains and records) and the dev
volume as a module."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import devvol
from ..core import Step
from .base import APPLIED, MANUAL, NOT_APPLIED, PARTIAL, Context, Module


class GuidedModule(Module):
    guided = True

    def __init__(self, mid: str, title: str, category: str, instructions: List[str], summary: str, benefit: str,
                 cost: str, url: str = ""):
        self.id, self.title, self.category, self.instructions, self.url = mid, title, category, instructions, url
        self.summary, self.benefit, self.cost = summary, benefit, cost

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        rec = st["modules"].get(self.id)
        return MANUAL, ("you did this on %s" % rec["applied_at"][:10]) if rec else "not done yet (by hand)"

    def apply_steps(self, ctx: Context) -> List[Step]:
        steps = []
        if self.url:
            steps.append(Step("open the right place", argv=["/usr/bin/open", self.url], allow_fail=True))
        for i, text in enumerate(self.instructions, 1):
            steps.append(Step("YOU, step %d: %s" % (i, text), fn=lambda: None))
        steps.append(self.record_step(ctx))
        return steps

    def revert_steps(self, ctx: Context) -> List[Step]:
        return [Step("YOU: undo it the same way (nothing for quietmac to change)", fn=lambda: None),
                self.forget_step(ctx)]


class DevVolumeModule(Module):
    id = "dev-volume"
    title = "Dev volume: code and build caches without fseventsd logging or Spotlight"
    category = "Development"
    admin = True
    summary = ("creates an APFS volume (shares free space, no fixed size) mounted at ~/dev by /etc/fstab, with "
               "fseventsd's event log and Spotlight off on it. Then move folders in with `quietmac migrate`.")
    benefit = ("fseventsd and Spotlight stop doing work for node_modules/target/build churn. Worth it when many agents "
               "or builds run in parallel and fseventsd climbs over days. File watching keeps working.")
    cost = ("the volume is not encrypted with your login password (FileVault covers the main volume only), and "
            "Spotlight won't find files on it")
    options = {"name": "volume name (default Dev)", "mount": "mount folder (default ~/dev)",
               "container": "APFS container to add the volume to (default: the startup disk's)"}

    def applicable(self, facts: Dict[str, Any]) -> Tuple[bool, str]:
        return True, ""

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        s = devvol.status(st)
        if not s.get("exists"):
            return NOT_APPLIED, ""
        issues = [k for k in ("mounted", "fstab", "no_log", "spotlight_off") if s.get(k) is False]
        return (PARTIAL if issues else APPLIED), "%s at %s%s" % (s["name"], s["mount"],
                                                                  (" - problems: " + ", ".join(issues)) if issues else "")

    def apply_steps(self, ctx: Context) -> List[Step]:
        return devvol.create_steps(ctx.state, ctx.opt("name", "Dev"), ctx.opt("mount", "~/dev"), ctx.opt("container"))

    def revert_steps(self, ctx: Context) -> List[Step]:
        return devvol.destroy_steps(ctx.state, force=ctx.opt("force") == "1")


MODULES = [
    GuidedModule(
        "nc-widgets-remove", "Remove widgets from Notification Center and the desktop", "Background daemons (reaper)",
        ["click the date and time in the menu bar, scroll down, click Edit Widgets",
         "click the minus on every widget until none are left, then Done",
         "for desktop widgets: right-click each one > Remove Widget"],
        "takes the widgets away so chronod (the widget host, which can't be killed) has nothing to refresh",
        "chronod stays idle instead of waking to refresh widgets",
        "the widgets (add them back from Edit Widgets)"),
    GuidedModule(
        "activity-monitor-habit", "Quit Activity Monitor when you're done with it", "Habits",
        ["quit Activity Monitor whenever you aren't actively looking at it; use `quietmac measure` instead"],
        "nothing to install: a habit",
        "while Activity Monitor is open, its collector sysmond used ~34% of a core (more than everything else idle "
        "combined)",
        "nothing"),
    DevVolumeModule(),
]
