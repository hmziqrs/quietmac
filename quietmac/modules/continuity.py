"""Wireless/Continuity switches: Handoff, AirDrop, Universal Control, Bluetooth.

Measured (M3 Max, macOS 27): bluetoothd, the Wi-Fi driver, rapportd, nearbyd,
sharingd, milod, wifip2pd and UniversalControl together used ~7-11% of one
core at idle. With these four off they used ~1.2%, most of it the Wi-Fi
driver. Killing these daemons instead backfires: many clients reconnect to
them, so launchd restarts them over and over.

The preference keys were checked against the macOS 27 binaries with `strings`
(useractivityd: ActivityAdvertisingAllowed/ActivityReceivingAllowed; sharingd:
DiscoverableMode; UniversalControl: Disable).
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .. import core
from ..core import Step
from .base import APPLIED, NA, NOT_APPLIED, PARTIAL, Context, Module


class DefaultsModule(Module):
    category = "Wireless & Continuity"

    def __init__(self, mid: str, title: str, writes: Sequence[Tuple[str, str, str, str, bool]],
                 restart: Sequence[Sequence[str]], summary: str, benefit: str, cost: str):
        self.id, self.title, self.writes, self.restart = mid, title, list(writes), [list(r) for r in restart]
        self.summary, self.benefit, self.cost = summary, benefit, cost

    @staticmethod
    def _norm(typ: str, value: str) -> str:
        if typ == "boolean":
            return "1" if str(value).lower() in ("1", "true", "yes") else "0"
        return str(value)

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        hits = 0
        for domain, key, typ, value, ch in self.writes:
            cur = core.defaults_read(domain, key, ch)
            if cur and self._norm(typ, cur[1]) == self._norm(typ, value):
                hits += 1
        if hits == len(self.writes):
            return APPLIED, ""
        return (PARTIAL if hits else NOT_APPLIED), ""

    def apply_steps(self, ctx: Context) -> List[Step]:
        prev = []
        steps = []
        for domain, key, typ, value, ch in self.writes:
            cur = core.defaults_read(domain, key, ch)
            prev.append([domain, key, ch, cur[0] if cur else None, cur[1] if cur else None])
            steps.append(Step("set %s %s = %s" % (domain, key, value),
                              argv=core.defaults_write_argv(domain, key, typ, value, ch)))
        for argv in self.restart:
            steps.append(Step("restart %s so it reads the new setting" % argv[-1], argv=argv, allow_fail=True))
        steps.append(self.record_step(ctx, previous=prev))
        return steps

    def revert_steps(self, ctx: Context) -> List[Step]:
        rec = ctx.record(self.id)
        steps = []
        prev = rec.get("previous")
        if prev is None:
            # never applied by quietmac: go back to the macOS default
            prev = [[d, k, ch, None, None] for d, k, _, _, ch in self.writes]
        for domain, key, ch, typ, value in prev:
            if typ is None:
                steps.append(Step("remove %s %s (back to the macOS default)" % (domain, key),
                                  argv=core.defaults_delete_argv(domain, key, ch), allow_fail=True))
            else:
                steps.append(Step("restore %s %s = %s" % (domain, key, value),
                                  argv=core.defaults_write_argv(domain, key, typ, value, ch)))
        for argv in self.restart:
            steps.append(Step("restart %s so it reads the setting" % argv[-1], argv=argv, allow_fail=True))
        steps.append(self.forget_step(ctx))
        return steps


UA = "com.apple.coreservices.useractivityd"


class BluetoothModule(Module):
    id = "bluetooth-off"
    title = "Turn Bluetooth off"
    category = "Wireless & Continuity"
    summary = "turns Bluetooth off with blueutil (installed with Homebrew if missing); stays off across reboots"
    benefit = "bluetoothd used 1-3% of a core at idle even with nothing connected"
    cost = ("AirPods, Apple Watch unlock, Bluetooth keyboards/mice/trackpads, AirDrop, Continuity Camera, Sidecar. "
            "Turn it back on from Control Center when you need it.")
    options = {"force": "1 to proceed even if a Bluetooth keyboard/mouse/trackpad is paired on a desktop Mac"}

    def applicable(self, facts: Dict[str, Any]) -> Tuple[bool, str]:
        if not facts["tools"]["blueutil"] and not facts["tools"]["brew"]:
            return False, "needs blueutil; install Homebrew first, or toggle Bluetooth in Control Center"
        return True, ""

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        p = facts["bluetooth"]["power"]
        if p is None:
            return NA, "can't read Bluetooth state"
        return (APPLIED if p is False else NOT_APPLIED), ""

    def apply_steps(self, ctx: Context) -> List[Step]:
        f = ctx.facts
        bt = f["bluetooth"]
        if bt["has_input_device"] and not f["macos"]["laptop"] and ctx.opt("force") != "1":
            raise core.QuietmacError(
                "a Bluetooth keyboard, mouse or trackpad is paired and this Mac has no built-in keyboard. Turning "
                "Bluetooth off could leave you without input. Re-run with --opt force=1 if you have a wired one.")
        steps = []
        blueutil = f["tools"]["blueutil"]
        if not blueutil:
            steps.append(Step("install blueutil (Homebrew formula toy/blueutil)",
                              argv=[f["tools"]["brew"], "install", "blueutil"]))
            blueutil = "/opt/homebrew/bin/blueutil" if f["tools"]["brew"].startswith("/opt/homebrew") else "/usr/local/bin/blueutil"
        steps.append(Step("turn Bluetooth off", argv=[blueutil, "-p", "0"]))
        steps.append(self.record_step(ctx, previous_power=bt["power"], blueutil=blueutil,
                                      installed_blueutil=not f["tools"]["blueutil"]))
        return steps

    def revert_steps(self, ctx: Context) -> List[Step]:
        rec = ctx.record(self.id)
        blueutil = rec.get("blueutil") or ctx.facts["tools"]["blueutil"] or "blueutil"
        steps = []
        if rec.get("previous_power", True) is not False:
            steps.append(Step("turn Bluetooth back on", argv=[blueutil, "-p", "1"]))
        steps.append(self.forget_step(ctx))
        return steps


MODULES = [
    DefaultsModule(
        "handoff-off", "Turn off Handoff",
        [(UA, "ActivityAdvertisingAllowed", "boolean", "false", True),
         (UA, "ActivityReceivingAllowed", "boolean", "false", True)],
        [["/usr/bin/killall", "useractivityd"]],
        "stops advertising and receiving Handoff activities (same as the System Settings toggle)",
        "part of the ~7-11% Continuity group; useractivityd and rapportd go quiet",
        "Handoff and Universal Clipboard (copy on iPhone, paste on Mac)"),
    DefaultsModule(
        "airdrop-off", "Set AirDrop to No One",
        [("com.apple.sharingd", "DiscoverableMode", "string", "Off", False)],
        [["/usr/bin/killall", "sharingd"]],
        "sets AirDrop discoverability to No One (you can still send)",
        "sharingd stops scanning and advertising",
        "receiving AirDrops until you set it back"),
    DefaultsModule(
        "universal-control-off", "Turn off Universal Control",
        [("com.apple.universalcontrol", "Disable", "boolean", "true", False)],
        [["/usr/bin/pkill", "-x", "UniversalControl"]],
        "turns off sharing your keyboard and pointer with nearby Macs and iPads",
        "UniversalControl stops looking for peers. It ignores the quit signal, so it idles at 0% until next login.",
        "Universal Control"),
    BluetoothModule(),
]
