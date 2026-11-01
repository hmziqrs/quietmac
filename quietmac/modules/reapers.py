"""reap-* modules: groups of per-user Apple daemons the reaper kills on sight.

Measured on an M3 Max, macOS 27.0 (26A428), SIP on (2026-11-01):
  * mediaanalysisd kept ANECompilerService (root) at ~95% of a core; it went
    to 0% once mediaanalysisd was gone and didn't run at all the next boot.
  * appstoreagent ignored `launchctl disable` and survived reboots.
  * After a reboot, every group below was killed within 2 s of login and
    stayed dead (launchd `runs = 1`). duetexpertd once hit the churn guard
    shortly after boot, so it is marked borderline.
  * NOT reapable: chronod (relaunched every ~5 s), the "Siri AI" app
    (com.apple.campo, relaunched every ~10 s) and siriinferenced.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import core, reaper
from ..core import Step
from .base import APPLIED, NOT_APPLIED, PARTIAL, Context, Module

_WARNED = set()  # hand-made reapers already warned about in this run


class ReaperModule(Module):
    category = "Background daemons (reaper)"

    def __init__(self, mid: str, title: str, names: List[str], labels: List[str], summary: str, benefit: str,
                 cost: str):
        self.id, self.title, self.names, self.labels = mid, title, names, labels
        self.summary, self.benefit, self.cost = summary, benefit, cost

    def group_names(self, ctx: Context) -> List[str]:
        return list(self.names)

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        rs = facts.get("reaper") or reaper.status()
        if self.id in st["reaper"]["groups"]:
            names = st["reaper"]["groups"][self.id]
            gave = [n for n in names if any(" %s " % n in g for g in rs.get("gave_up", []))]
            if not rs.get("loaded"):
                return PARTIAL, "in the kill list, but the reaper agent isn't running (quietmac doctor)"
            return APPLIED, ("gave up on %s this login (respawn loop)" % ", ".join(gave)) if gave else ""
        for lg in rs.get("legacy") or []:
            if set(self.names) and set(self.names) <= set(lg["names"]):
                return NOT_APPLIED, "already killed by the hand-made reaper %s (quietmac adopt takes it over)" % lg["label"]
        return NOT_APPLIED, ""

    def apply_steps(self, ctx: Context) -> List[Step]:
        names = self.group_names(ctx)
        if not names:
            raise core.QuietmacError("%s: nothing to reap" % self.id)
        for n in names:
            problem = reaper.check_name(n)
            if problem:
                raise core.QuietmacError(problem)
        disabled = core.gui_disabled()
        new_labels = [l for l in self.labels if not disabled.get(l)]
        st = ctx.state
        for lg in ctx.facts.get("reaper", {}).get("legacy", []):
            if lg["loaded"] and ctx.opt("adopting") != "1" and lg["label"] not in _WARNED:
                _WARNED.add(lg["label"])  # once per run, not once per module
                core.warn("the hand-made reaper %s is still running next to quietmac's; `quietmac adopt` retires it "
                          "(it costs ~1%% of a core on its own)" % lg["label"])

        def add() -> None:
            st["reaper"]["groups"][self.id] = sorted(names)
            reaper.write_conf(st)
            core.save_state(st)
            core.log_action("reaper kill list += %s (%s)" % (", ".join(names), self.id))

        steps = [Step("add %s to the reaper's kill list (%s)" % (", ".join(names), reaper.CONF), fn=add)]
        steps += reaper.ensure_agent_steps()
        for label in new_labels:
            steps.append(Step("mark %s disabled in launchd (SIP-protected jobs ignore this; harmless backup)" % label,
                              argv=["/bin/launchctl", "disable", core.gui(label)], allow_fail=True))
        steps.append(self.record_step(ctx, names=sorted(names), disabled_labels=new_labels))
        return steps

    def revert_steps(self, ctx: Context) -> List[Step]:
        st = ctx.state
        rec = ctx.record(self.id)
        others = [g for g in st["reaper"]["groups"] if g != self.id]

        def rm() -> None:
            st["reaper"]["groups"].pop(self.id, None)
            reaper.write_conf(st)
            core.save_state(st)
            core.log_action("reaper kill list -= %s" % self.id)

        steps = [Step("remove %s's names from the reaper's kill list" % self.id, fn=rm)]
        for label in rec.get("disabled_labels", []):
            steps.append(Step("clear the launchd disabled mark on %s" % label,
                              argv=["/bin/launchctl", "enable", core.gui(label)], allow_fail=True))
        if not others and not st["reaper"]["custom"]:
            steps += reaper.remove_agent_steps()
        steps.append(self.forget_step(ctx))
        return steps


class WidgetReaper(ReaperModule):
    options = {"names": "comma-separated widget extension names (default: every app widget found by detect)"}

    def group_names(self, ctx: Context) -> List[str]:
        chosen = ctx.opt("names")
        if chosen:
            return [n.strip() for n in chosen.split(",") if n.strip()]
        return sorted(set(w["name"] for w in ctx.facts["widgets"]["installed"]))

    def applicable(self, facts: Dict[str, Any]) -> Tuple[bool, str]:
        return (bool(facts["widgets"]["installed"]), "no app widget extensions found")


MODULES = [
    ReaperModule(
        "reap-appstore", "Kill the App Store agent",
        ["appstoreagent"], ["com.apple.appstoreagent"],
        "kills appstoreagent whenever it starts",
        "it ignores `launchctl disable`, survives reboots and spawns App Store background work",
        "App Store downloads and updates. Revert this module (or `quietmac reaper pause`) before updating apps."),
    ReaperModule(
        "reap-media-analysis", "Kill photo and media analysis",
        ["mediaanalysisd", "photoanalysisd", "visualintelligenced"],
        ["com.apple.mediaanalysisd", "com.apple.photoanalysisd", "com.apple.visualintelligenced"],
        "kills mediaanalysisd, photoanalysisd and visualintelligenced",
        "mediaanalysisd kept ANECompilerService at ~95% of a core; it dropped to 0% right after",
        "Photos People, Memories and scene search; Visual Look Up; possibly Live Text in images"),
    ReaperModule(
        "reap-siri-helpers", "Kill the Siri helper daemons",
        ["sirittsd", "siriknowledged", "siriappintentsd"],
        ["com.apple.sirittsd", "com.apple.siriknowledged", "com.apple.siriappintentsd", "com.apple.Siri.agent"],
        "kills sirittsd (~275 MB), siriknowledged and siriappintentsd; marks Siri.agent disabled (it honors that)",
        "they idle in RAM and burst at login even with Siri turned off",
        "Siri. Turn Siri off first (System Settings, or the privacy-profile module). The 'Siri AI' app and "
        "siriinferenced are deliberately left alone: killing them is a respawn loop."),
    ReaperModule(
        "reap-shortcuts", "Kill the Shortcuts engine",
        ["siriactionsd"], ["com.apple.siriactionsd"],
        "kills siriactionsd",
        "it runs at login and on triggers even if you never use Shortcuts",
        "the Shortcuts app, shortcut automations, and Siri/Spotlight actions that run shortcuts"),
    ReaperModule(
        "reap-suggestions", "Kill Siri Suggestions",
        ["duetexpertd"], ["com.apple.duetexpertd"],
        "kills duetexpertd (app and action predictions)",
        "~10 s of CPU in the first minutes after each boot",
        "Siri Suggestions and app predictions. Borderline: it sometimes respawns right after boot; the churn "
        "guard stops after 10 kills in 10 minutes."),
    WidgetReaper(
        "reap-widgets", "Kill app widget extensions",
        [], [],
        "kills the widget extensions of apps (Weather, Stocks, Calendar, Photos, Find My, ...)",
        "each one holds 25-70 MB of RAM; 8 of them were ~340 MB. They stay dead (no respawn churn).",
        "desktop and Notification Center widgets. chronod (the widget host) and Control Center controls are kept."),
]
