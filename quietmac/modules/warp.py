"""warp-on-demand: Cloudflare WARP runs only while its app is open.

WARP (consumer 1.1.1.1 build) installs a KeepAlive root LaunchDaemon that does
health checks and captive-portal probes even while disconnected (~0.7-1.5% of
a core here), and its menu-bar app re-adds a login item every time it starts.
There's no "only when I open it" setting, so:

  * the daemon is `launchctl disable`d, so it doesn't start at boot
  * a tiny Swift watcher (LaunchAgent dev.quietmac.warp-ondemand, 0% CPU,
    ~35 MB) observes NSWorkspace.runningApplications. When you open WARP it
    enables and loads the daemon; when you quit it, it disconnects, unloads and
    disables the daemon, and deletes the login item. A launch within 90 s of
    login counts as an auto-start, and the app is quit.
  * /etc/sudoers.d/quietmac-warp lets you run exactly those four launchctl
    commands without a password. `sudo` for anything else still asks.
  * `warp on|off|status` does the same from a terminal.

Gotchas found on the way: WARP is an LSUIElement app, so NSWorkspace
didLaunch/didTerminate notifications never arrive (hence KVO plus a UI-less
NSApplication); AppleScript `quit` returns error -128 although the app does
exit.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .. import core
from ..core import Step
from .base import APPLIED, NOT_APPLIED, PARTIAL, Context, Module

LABEL = core.LABEL_PREFIX + ".warp-ondemand"
PLIST = core.AGENTS_DIR / (LABEL + ".plist")
SRC = core.SHARE_DIR / "src" / "warp-ondemand.swift"
BINARY = core.SHARE_DIR / "bin" / "warp-ondemand"
WARP_SCRIPT = core.SHARE_DIR / "bin" / "warp"
WARP_LINK = core.BIN_DIR / "warp"
SUDOERS = Path("/etc/sudoers.d/quietmac-warp")
DAEMON = "com.cloudflare.1dot1dot1dot1.macos.warp.daemon"
DAEMON_PLIST = "/Library/LaunchDaemons/%s.plist" % DAEMON
APP = Path("/Applications/Cloudflare WARP.app")
LOG = core.LOG_DIR / "warp-ondemand.log"


def legacy_watchers() -> List[Dict[str, Any]]:
    found = []
    if core.AGENTS_DIR.is_dir():
        for p in sorted(core.AGENTS_DIR.glob("*.plist")):
            d = core.read_plist(p) or {}
            if d.get("Label") == LABEL:
                continue
            args = [str(a) for a in d.get("ProgramArguments") or []]
            if any(os.path.basename(a) == "warp-ondemand" for a in args):
                found.append({"label": d.get("Label", p.stem), "plist": str(p), "program": args[0] if args else "",
                              "loaded": core.agent_loaded(d.get("Label", p.stem))})
    return found


def legacy_sudoers() -> List[Path]:
    """Sudoers files from a hand-made setup that allow the WARP daemon commands."""
    d = Path("/etc/sudoers.d")
    found = []
    try:
        for p in d.iterdir():
            if p != SUDOERS and p.name.startswith("warp"):
                found.append(p)
    except OSError:
        pass
    return found


def sudoers_text() -> str:
    if not core.valid_username(core.USER):
        raise core.QuietmacError("unusual user name %r; refusing to write a sudoers rule for it" % core.USER)
    return (core.PAYLOAD_DIR / "sudoers-warp.template").read_text().replace("@USER@", core.USER)


def plist_data() -> Dict[str, Any]:
    return core.agent_plist(LABEL, [str(BINARY)], env={"WARP_ONDEMAND_LOG": str(LOG)}, log_name="warp-ondemand",
                            extra={"LimitLoadToSessionType": "Aqua"})


class WarpModule(Module):
    id = "warp-on-demand"
    title = "Run Cloudflare WARP only while its app is open"
    category = "Apps"
    admin = True
    summary = ("disables WARP's always-on root daemon; a watcher starts it when you open the WARP app and stops it "
               "when you quit; adds a `warp on|off|status` command")
    benefit = "nothing from WARP runs at boot or while you don't use it (it used ~0.7-1.5% of a core idle)"
    cost = "nothing, as long as you open the WARP app (or run `warp on`) when you want the VPN"
    options = {"replace_legacy": "1 to replace a hand-made watcher/sudoers rule/`warp` script from before quietmac"}

    def applicable(self, facts: Dict[str, Any]) -> Tuple[bool, str]:
        if not facts["warp"]["installed"]:
            return False, "Cloudflare WARP isn't installed"
        if not facts["tools"]["swiftc"]:
            return False, "needs the Swift compiler: run `xcode-select --install`"
        return True, ""

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        w = facts["warp"]
        if not w["installed"]:
            return NOT_APPLIED, "WARP not installed"
        have = [SUDOERS.exists(), w["watcher_loaded"], BINARY.exists(), w["daemon_disabled"] or w["daemon_loaded"]]
        if all(have):
            return APPLIED, ""
        notes = []
        if w["legacy_watchers"]:
            notes.append("hand-made watcher %s found%s" % (w["legacy_watchers"][0]["label"],
                                                          "" if w["legacy_watchers"][0]["loaded"] else " (not running)"))
        if w["daemon_disabled"] and not w["watcher_loaded"] and not any(lw["loaded"] for lw in w["legacy_watchers"]):
            notes.append(core.red("the daemon is disabled but no watcher is running: opening WARP won't connect "
                                  "(run `warp on`, or apply this module)"))
        return (PARTIAL if any(have) else NOT_APPLIED), "; ".join(notes)

    def apply_steps(self, ctx: Context) -> List[Step]:
        legacy = legacy_watchers()
        old_sudoers = legacy_sudoers()
        link_conflict = (WARP_LINK.exists() or WARP_LINK.is_symlink()) and not (
            WARP_LINK.is_symlink() and os.path.realpath(str(WARP_LINK)) == os.path.realpath(str(WARP_SCRIPT)))
        replace = ctx.opt("replace_legacy") == "1"
        if (legacy or old_sudoers or link_conflict) and not replace:
            what = [l["label"] for l in legacy] + [str(p) for p in old_sudoers] + ([str(WARP_LINK)] if link_conflict else [])
            raise core.QuietmacError("found an earlier hand-made WARP setup (%s). Re-run with --opt replace_legacy=1 to "
                                     "replace it (the old files are backed up), or `quietmac adopt`." % ", ".join(what))
        text = sudoers_text()
        tmp = core.STATE_DIR / "quietmac-warp.sudoers"

        def build() -> None:
            for d in (SRC.parent, BINARY.parent):
                d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(core.PAYLOAD_DIR / "warp-ondemand.swift"), str(SRC))
            r = core.run(["/usr/bin/xcrun", "swiftc", "-O", "-swift-version", "5", "-o", str(BINARY), str(SRC)], timeout=600)
            if not r.ok:
                raise core.StepError("swiftc failed:\n" + (r.err or r.out))
            shutil.copy2(str(core.PAYLOAD_DIR / "warp"), str(WARP_SCRIPT))
            os.chmod(str(WARP_SCRIPT), 0o755)
            core.atomic_write(tmp, text, 0o600)
            v = core.run(["/usr/sbin/visudo", "-cf", str(tmp)])
            if not v.ok:
                raise core.StepError("the generated sudoers rule doesn't parse: " + (v.err or v.out))

        steps = [Step("build the watcher (swiftc) and stage the `warp` script and sudoers rule", fn=build)]
        for lg in legacy:
            steps.append(Step("stop the hand-made watcher %s" % lg["label"],
                              argv=["/bin/launchctl", "bootout", core.gui(lg["label"])], allow_fail=True))
            steps.append(Step("back up %s" % lg["plist"], fn=_backup(Path(lg["plist"]))))
        script = ("/usr/bin/install -m 0440 -o root -g wheel %s %s\n"
                  "/usr/sbin/visudo -c >/dev/null || { /bin/rm -f %s; echo 'sudoers check failed' >&2; exit 1; }"
                  % (core.sh_quote(str(tmp)), SUDOERS, SUDOERS))
        for p in old_sudoers:
            script += "\n/bin/mv %s %s.quietmac-backup\n/bin/chmod 0400 %s.quietmac-backup" % (
                core.sh_quote(str(p)), core.sh_quote(str(core.STATE_DIR / p.name)), core.sh_quote(str(core.STATE_DIR / p.name)))
            script += "\n/usr/sbin/chown %s %s.quietmac-backup" % (core.USER, core.sh_quote(str(core.STATE_DIR / p.name)))
        steps.append(Step("install /etc/sudoers.d/quietmac-warp (exactly 4 launchctl commands, no password)",
                          script=script))

        def link() -> None:
            core.BIN_DIR.mkdir(parents=True, exist_ok=True)
            if WARP_LINK.exists() or WARP_LINK.is_symlink():
                if not (WARP_LINK.is_symlink() and os.path.realpath(str(WARP_LINK)) == os.path.realpath(str(WARP_SCRIPT))):
                    _backup(WARP_LINK)()
            if not WARP_LINK.exists():
                os.symlink(str(WARP_SCRIPT), str(WARP_LINK))
            core.write_plist(PLIST, plist_data())

        steps.append(Step("link %s -> %s and write the LaunchAgent" % (WARP_LINK, WARP_SCRIPT), fn=link))
        steps.append(Step("start the watcher (it brings the daemon in line with whether the app is open)",
                          argv=["/bin/launchctl", "bootstrap", "gui/%d" % core.UID, str(PLIST)]))
        steps.append(self.record_step(ctx, legacy=[l["label"] for l in legacy], old_sudoers=[str(p) for p in old_sudoers]))
        return steps

    def revert_steps(self, ctx: Context) -> List[Step]:
        steps = []
        if core.agent_loaded(LABEL):
            steps.append(Step("stop the watcher", argv=["/bin/launchctl", "bootout", core.gui(LABEL)], allow_fail=True))

        def rm() -> None:
            for p in (PLIST, BINARY, SRC):
                if p.exists():
                    p.unlink()
            if WARP_LINK.is_symlink() and os.path.realpath(str(WARP_LINK)) == os.path.realpath(str(WARP_SCRIPT)):
                WARP_LINK.unlink()
            if WARP_SCRIPT.exists():
                WARP_SCRIPT.unlink()
            backup = Path(str(WARP_LINK) + ".quietmac-backup")
            if backup.exists() and not WARP_LINK.exists():
                backup.rename(WARP_LINK)

        steps.append(Step("remove the watcher, its LaunchAgent and the `warp` link", fn=rm))
        steps.append(Step("remove the sudoers rule and give WARP its normal always-on daemon back", script=(
            "/bin/rm -f %s\n/bin/launchctl enable system/%s\n"
            "/bin/launchctl bootstrap system %s 2>/dev/null || true" % (SUDOERS, DAEMON, DAEMON_PLIST))))
        steps.append(self.forget_step(ctx))
        return steps


def _backup(p: Path):
    def fn() -> None:
        if p.exists() or p.is_symlink():
            dest = Path(str(p) + ".quietmac-backup")
            if dest.exists() or dest.is_symlink():
                dest = Path("%s.quietmac-backup-%s" % (p, core.now_iso().replace(":", "")))
            p.rename(dest)
            core.log_action("backed up %s -> %s" % (p, dest))
    return fn


MODULES = [WarpModule()]
