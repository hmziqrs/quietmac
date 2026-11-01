"""quietmac command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional

from . import battery, cache, core, detect, devvol, measure, migrate, questions, reaper
from .modules import ALL, BY_ID, get
from .modules.base import APPLIED, MANUAL, PARTIAL, Context

BOOT = "import sys; sys.path.insert(0, sys.argv.pop(1)); from quietmac.cli import main; sys.exit(main())"


def parse_opts(raw: Optional[List[str]]) -> Dict[str, str]:
    opts = {}
    for item in raw or []:
        if "=" not in item:
            raise core.QuietmacError("--opt takes key=value (or module.key=value), got %r" % item)
        k, v = item.split("=", 1)
        opts[k.strip()] = v.strip()
    return opts


def opts_for(opts: Dict[str, str], mid: str) -> Dict[str, str]:
    res = {k: v for k, v in opts.items() if "." not in k}
    for k, v in opts.items():
        if "." in k and k.split(".", 1)[0] == mid:
            res[k.split(".", 1)[1]] = v
    return res


def _status_word(s: str) -> str:
    return {APPLIED: core.green(s), PARTIAL: core.yellow(s), MANUAL: core.dim(s)}.get(s, s)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


# --------------------------------------------------------------------------


def cmd_detect(a: argparse.Namespace) -> int:
    f = detect.detect(sizes=a.sizes)
    if a.json:
        _print_json(f)
    else:
        core.say(detect.render(f))
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    f = detect.detect()
    st = core.load_state()
    rows = []
    for m in ALL:
        ok, why = m.applicable(f)
        s, detail = m.status(f, st)
        rows.append({"id": m.id, "title": m.title, "category": m.category, "status": s, "detail": detail,
                     "applicable": ok, "why_not": why, "admin": m.admin, "guided": m.guided,
                     "summary": m.summary, "benefit": m.benefit, "cost": m.cost, "options": m.options})
    if a.json:
        _print_json(rows)
        return 0
    order: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        order.setdefault(r["category"], []).append(r)
    for cat, group in order.items():
        core.say("\n" + core.bold(cat))
        for r in group:
            _list_row(r)
    core.say("\n`quietmac plan <id>` explains a module and shows exactly what it would run.")
    return 0


def _list_row(r: Dict[str, Any]) -> None:
    flag = "" if r["applicable"] else core.dim("  (n/a: %s)" % r["why_not"])
    core.say("  %-26s %-12s %s%s" % (r["id"], _status_word(r["status"]), r["title"], flag))
    if r["detail"]:
        core.say("  %-26s %-12s %s" % ("", "", core.dim(r["detail"])))


def cmd_plan(a: argparse.Namespace) -> int:
    a.dry_run, a.yes = True, True
    return cmd_apply(a, plan_only=True)


def _run_modules(a: argparse.Namespace, revert: bool, plan_only: bool = False) -> int:
    f = detect.detect()
    st = core.load_state()
    opts = parse_opts(a.opt)
    done = []
    failed = []
    for m in [get(mid) for mid in a.modules]:  # unknown ids fail before anything runs
        mid = m.id
        ctx = Context(f, st, opts_for(opts, mid), dry_run=a.dry_run, yes=a.yes)
        if not revert:
            ok, why = m.applicable(f)
            if not ok:
                core.warn("%s: not applicable here (%s)" % (mid, why))
                failed.append(mid)
                continue
        elif mid not in st["modules"] and m.status(f, st)[0] not in (APPLIED, PARTIAL):
            core.warn("%s isn't applied; nothing to revert" % mid)
            continue
        try:
            steps = m.revert_steps(ctx) if revert else m.apply_steps(ctx)
        except core.QuietmacError as e:
            print(core.red("quietmac: ") + "%s: %s" % (mid, e), file=sys.stderr)
            failed.append(mid)
            continue
        core.say("")
        core.say(m.describe())
        core.say("  %s:" % ("revert would" if revert and a.dry_run else "revert" if revert else
                            "apply would" if a.dry_run else "apply"))
        for s in steps:
            core.say("    - " + s.render().replace("\n", "\n    "))
        if a.dry_run:
            continue
        if not core.confirm("%s %s?" % ("Revert" if revert else "Apply", mid), default=True, assume_yes=a.yes):
            core.say("skipped " + mid)
            continue
        core.say(core.bold("%s %s" % ("reverting" if revert else "applying", mid)))
        try:
            core.run_steps(steps, m.title)
        except core.QuietmacError as e:
            print(core.red("quietmac: ") + "%s stopped partway: %s\n  `quietmac status` shows what took effect; "
                  "`quietmac revert %s` undoes it." % (mid, e, mid), file=sys.stderr)
            failed.append(mid)
            continue
        done.append(m)
    if done:
        f2 = detect.detect()
        core.say("")
        for m in done:
            s, detail = m.status(f2, core.load_state())
            core.say("%-26s %s %s" % (m.id, _status_word(s), core.dim(detail)))
    elif plan_only or a.dry_run:
        core.say(core.dim("\n(dry run: nothing was changed)"))
    if failed:
        core.warn("not done: " + ", ".join(failed))
        return 1
    return 0


def cmd_apply(a: argparse.Namespace, plan_only: bool = False) -> int:
    return _run_modules(a, revert=False, plan_only=plan_only)


def cmd_revert(a: argparse.Namespace) -> int:
    return _run_modules(a, revert=True)


def cmd_status(a: argparse.Namespace) -> int:
    f = detect.detect()
    st = core.load_state()
    applied = []
    for m in ALL:
        s, detail = m.status(f, st)
        if m.id in st["modules"] or s in (APPLIED, PARTIAL):
            applied.append({"id": m.id, "status": s, "detail": detail,
                            "applied_at": st["modules"].get(m.id, {}).get("applied_at")})
    res = {"modules": applied, "reaper": f["reaper"], "devvol": f["devvol"], "migrations": st["migrations"],
           "caches": st["caches"], "drift": drift(f, st)}
    if a.json:
        _print_json(res)
        return 0
    core.say(core.bold("Modules in effect"))
    for r in applied:
        by = "" if r["applied_at"] else core.dim(" (not applied by quietmac)")
        core.say("  %-26s %s %s%s" % (r["id"], _status_word(r["status"]), core.dim(r["detail"]), by))
    if not applied:
        core.say("  none")
    rp = f["reaper"]
    core.say(core.bold("\nReaper") + ": %s, method %s, killing: %s" % (
        "running" if rp["loaded"] else "not running", rp["method"] or "-", ", ".join(rp["names"]) or "nothing"))
    for g in rp["gave_up"]:
        core.say("  " + core.yellow(g))
    dv = f["devvol"]
    if dv.get("exists"):
        core.say(core.bold("\nDev volume") + ": %s at %s, %s, fseventsd log %s, Spotlight %s" % (
            dv["name"], dv["mount"], "mounted" if dv["mounted"] else core.red("NOT mounted"),
            "off" if dv["no_log"] else core.red("on"), "off" if dv["spotlight_off"] else core.red("on")))
    for src, m in st["migrations"].items():
        core.say("  migration %s -> %s: %s" % (src, m.get("dst"), m.get("status")))
    for n, c in st["caches"].items():
        core.say("  cache %s: %s -> %s" % (n, c["src"], c["dst"]))
    for d in res["drift"]:
        core.say(core.yellow("drift: ") + d)
    core.say("\nThe ledger of every change is %s" % core.ACTIONS_LOG)
    return 0


def drift(f: Dict[str, Any], st: Dict[str, Any]) -> List[str]:
    out = []
    for mid in st["modules"]:
        m = BY_ID.get(mid)
        if not m or m.guided:
            continue
        s, detail = m.status(f, st)
        if s not in (APPLIED, MANUAL):
            out.append("%s is %s%s; macOS updates sometimes reset settings: `quietmac apply %s`" % (
                mid, s, (" (%s)" % detail) if detail else "", mid))
    if st["modules"] and any(mid.startswith("reap-") for mid in st["modules"]) and not f["reaper"]["loaded"]:
        out.append("the reaper isn't running: `quietmac doctor --fix`")
    elif f["reaper"]["installed"] and not f["reaper"]["current"]:
        out.append("the installed reaper differs from this checkout: `quietmac doctor --fix` refreshes it")
    dv = st.get("devvol")
    if dv and not devvol.is_mounted_at(dv["mount"], dv["uuid"]):
        out.append("the dev volume isn't mounted at %s: `quietmac devvol mount`" % dv["mount"])
    out += cache.drift(st)
    for src, m in st["migrations"].items():
        if m.get("status") not in ("done", "rolledback"):
            out.append("migration of %s stopped at '%s': `quietmac migrate run %s` resumes, "
                       "`quietmac migrate rollback %s` undoes" % (src, m.get("status"), src, src))
    w = f["warp"]
    if w["installed"] and w["daemon_disabled"] and not w["watcher_loaded"] and not any(
            x["loaded"] for x in w["legacy_watchers"]):
        out.append("Cloudflare WARP's daemon is disabled but no watcher runs, so opening WARP won't connect: "
                   "`quietmac apply warp-on-demand --opt replace_legacy=1` (or `warp on`)")
    return out


def cmd_doctor(a: argparse.Namespace) -> int:
    f = detect.detect()
    st = core.load_state()
    t = f["tools"]
    core.say(core.bold("Prerequisites"))
    core.say("  Xcode command line tools: %s" % (t["clt"] or core.red("missing: run `xcode-select --install`")))
    core.say("  python: %s %s" % (t["python"], "" if tuple(map(int, t["python"].split(".")[:2])) >= (3, 9) else core.red("(need 3.9+)")))
    core.say("  swiftc (warp-on-demand): %s" % (t["swiftc"] or "missing"))
    core.say("  Homebrew (bluetooth-off): %s; blueutil: %s" % (t["brew"] or "missing", t["blueutil"] or "missing"))
    core.say("  SIP: %s" % ("on (good: quietmac never needs it off)" if f["macos"]["sip"] else core.yellow("off")))
    issues = drift(f, st)
    core.say(core.bold("\nDrift"))
    for d in issues:
        core.say("  " + core.yellow(d))
    if not issues:
        core.say("  none: everything quietmac applied is still in effect")
    if f["daemons"]["activity_monitor_open"]:
        core.say(core.yellow("\nActivity Monitor is open: its collector sysmond costs ~30% of a core. Quit it when done."))
    if a.fix:
        needs = any(mid.startswith("reap-") for mid in st["modules"]) or st["reaper"]["custom"]
        if needs and (not f["reaper"]["loaded"] or not f["reaper"]["current"]):
            core.say(core.bold("\nfixing the reaper"))
            reaper.write_conf(st)
            steps = reaper.ensure_agent_steps() or [core.Step("start the reaper", fn=reaper._bootstrap)]
            core.run_steps(steps, "refresh the reaper")
    return 1 if issues and not a.fix else 0


def cmd_questions(a: argparse.Namespace) -> int:
    f = detect.detect()
    qs = questions.evaluate(f)
    if a.json:
        _print_json(qs)
        return 0
    for q in qs:
        mark = "ask " if q["ask"] else core.dim("skip")
        core.say("%s [%d] %-18s %s" % (mark, q["batch"], q["id"], q["question"] if q["ask"] else core.dim(q["skip_reason"])))
    return 0


def cmd_setup(a: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        raise core.QuietmacError("setup is interactive. Agents: follow INSTALL.md (quietmac questions --json, then "
                                 "quietmac apply ...).")
    f = detect.detect()
    core.say(detect.render(f))
    core.say("")
    mods: List[str] = []
    opts: Dict[str, str] = {}
    folders: List[str] = []
    caches: List[str] = []
    groups: List[str] = []
    for q in questions.evaluate(f):
        if not q["ask"]:
            continue
        if q["id"] in ("migrate", "caches") and "dev-volume" not in mods and not f["devvol"].get("exists"):
            continue  # no dev volume, nothing to move onto
        core.say("")
        if q["multi"]:
            core.say(core.bold(q["question"]))
            for i, o in enumerate(q["options"], 1):
                core.say("  %d) %s  %s" % (i, o["label"], core.dim(o.get("description", ""))))
            raw = input("numbers separated by commas, or Enter for none > ").strip()
            picked = [q["options"][int(x) - 1] for x in raw.replace(" ", "").split(",")
                      if x.isdigit() and 1 <= int(x) <= len(q["options"])]
        else:
            d = q["recommended"] if q["recommended"] is not None else len(q["options"]) - 1
            picked = [q["options"][core.choose(q["question"], [o["label"] for o in q["options"]], d)]]
        for o in picked:
            for mid in o.get("apply", []):
                if mid not in mods:
                    mods.append(mid)
            for k, v in (o.get("opts") or {}).items():
                if k == "privacy-profile.groups":
                    groups += v.split(",")
                else:
                    opts[k] = v
            folders += o.get("migrate", [])
            caches += o.get("cache", [])
    if groups:
        opts["privacy-profile.groups"] = ",".join(dict.fromkeys(groups))
    if "dev-volume" not in mods and not f["devvol"].get("exists"):
        folders, caches = [], []
    core.say(core.bold("\nPlan"))
    for mid in mods:
        core.say("  apply  %-26s %s" % (mid, BY_ID[mid].title))
    for p in folders:
        core.say("  move   %s onto the dev volume" % p)
    for c in caches:
        core.say("  move   the %s cache onto the dev volume" % c)
    if not (mods or folders or caches):
        core.say("  nothing to do")
        return 0
    if not core.confirm("Go ahead? You'll see each module's exact steps and can skip any", default=True):
        return 0
    ns = argparse.Namespace(modules=mods, opt=["%s=%s" % kv for kv in opts.items()], dry_run=a.dry_run, yes=False)
    _run_modules(ns, revert=False)
    if a.dry_run:
        return 0
    st = core.load_state()
    if folders and st.get("devvol"):
        _relocate_if_inside(folders)
        for p in folders:
            migrate.run_one(st, p)
    for c in caches:
        if st.get("devvol"):
            cache.move(st, c)
    return 0


def cmd_reaper(a: argparse.Namespace) -> int:
    st = core.load_state()
    if a.action == "status":
        s = reaper.status()
        if a.json:
            _print_json(dict(s, legacy=reaper.legacy_agents()))
            return 0
        core.say("reaper %s: %s, pid %s, %s CPU s, method %s" % (reaper.LABEL, "running" if s["loaded"] else "not running",
                                                                s["pid"], s["cpu_seconds"], s["method"]))
        core.say("kill list (%s): %s" % (reaper.CONF, ", ".join(s["names"]) or "empty"))
        for g in s["gave_up"] + s["watching"]:
            core.say(core.yellow("  " + g))
        for lg in reaper.legacy_agents():
            core.say("legacy reaper %s (%s): %s" % (lg["label"], "loaded" if lg["loaded"] else "not loaded", ", ".join(lg["names"])))
        return 0
    if a.action == "log":
        lines = reaper.log_since_start()
        core.say("\n".join(lines[-(a.lines or 40):]) or "(no log yet: %s)" % reaper.LOG)
        return 0
    if a.action in ("add", "remove"):
        if not a.names:
            raise core.QuietmacError("give one or more executable names")
        for n in a.names:
            if a.action == "add":
                problem = reaper.check_name(n)
                if problem:
                    raise core.QuietmacError(problem)
                if n not in st["reaper"]["custom"]:
                    st["reaper"]["custom"].append(n)
            else:
                owner = [g for g, names in st["reaper"]["groups"].items() if n in names]
                if owner:
                    raise core.QuietmacError("%s belongs to module %s; `quietmac revert %s` removes the whole group"
                                             % (n, owner[0], owner[0]))
                if n in st["reaper"]["custom"]:
                    st["reaper"]["custom"].remove(n)
        if a.action == "add" and not a.yes:
            core.say("tip: `quietmac probe <name>` first tells you whether it stays dead or respawns in a loop")
        reaper.write_conf(st)
        core.save_state(st)
        core.log_action("reaper %s %s" % (a.action, " ".join(a.names)))
        if a.action == "add":
            core.run_steps(reaper.ensure_agent_steps(), "start the reaper")
        elif not reaper.names_from_state(st):
            core.run_steps(reaper.remove_agent_steps(), "stop the reaper")
        core.say("kill list: " + (", ".join(reaper.names_from_state(st)) or "empty"))
        return 0
    if a.action == "pause":
        core.run_steps([core.Step("stop the reaper until `quietmac reaper resume` or next login",
                                  argv=["/bin/launchctl", "bootout", core.gui(reaper.LABEL)], allow_fail=True)], "pause")
        return 0
    if a.action in ("pause", "resume") and not reaper.PLIST.exists():
        raise core.QuietmacError("quietmac's reaper isn't installed; apply a reap-* module (or `quietmac adopt`) first")
    if a.action == "resume":
        if core.agent_loaded(reaper.LABEL):
            core.say("the reaper is already running")
            return 0
        core.run_steps([core.Step("start the reaper", fn=reaper._bootstrap)], "resume")
        return 0
    return 2


def cmd_probe(a: argparse.Namespace) -> int:
    if not core.confirm("This kills every %s process for %ds to see whether it comes back. OK?" % (a.name, a.seconds),
                        default=True, assume_yes=a.yes):
        return 0
    res = reaper.probe(a.name, a.seconds)
    if a.json:
        _print_json(res)
    else:
        core.say((core.green if res["ok"] else core.red)("verdict: ") + res["verdict"])
        core.say("launchd runs: %s -> %s; launchd CPU during probe: +%.2fs" % (
            res["launchd_runs"][0], res["launchd_runs"][1], res["launchd_cpu_delta"]))
        if res["ok"]:
            core.say("add it with: quietmac reaper add %s" % a.name)
    return 0 if res["ok"] else 1


def cmd_measure(a: argparse.Namespace) -> int:
    res = measure.report(a.seconds, a.top, a.since_boot)
    if a.json:
        _print_json(res)
    else:
        core.say(measure.render(res))
    return 0


def cmd_battery(a: argparse.Namespace) -> int:
    res = battery.report(a.capacity_wh)
    if a.json:
        _print_json(res)
    else:
        core.say(battery.render(res))
    return 0


def cmd_devvol(a: argparse.Namespace) -> int:
    st = core.load_state()
    if a.action == "status":
        s = devvol.status(st)
        if a.json:
            _print_json(s)
        elif not s.get("exists"):
            core.say("no dev volume (create one with `quietmac devvol create`)")
        else:
            for k in ("name", "mount", "uuid", "device", "mounted", "fstab", "no_log", "spotlight_off", "free_bytes"):
                core.say("  %-14s %s" % (k, s.get(k)))
        return 0
    if a.action == "create":
        ns = argparse.Namespace(modules=["dev-volume"], dry_run=a.dry_run, yes=a.yes,
                                opt=["name=%s" % a.name, "mount=%s" % a.mount] + (["container=%s" % a.container] if a.container else []))
        return _run_modules(ns, revert=False)
    if a.action == "mount":
        core.run_steps(devvol.mount_steps(st), "mount the dev volume", dry_run=a.dry_run)
        return 0
    if a.action == "destroy":
        steps = devvol.destroy_steps(st, force=a.force)
        for s in steps:
            core.say("  - " + s.render())
        if a.dry_run or not core.confirm("Delete the dev volume%s?" % (" AND ITS FILES" if a.force else ""), assume_yes=a.yes):
            return 0
        core.run_steps(steps, "delete the dev volume")
        return 0
    return 2


def _relocate_if_inside(paths: List[str]) -> None:
    """If this checkout lives inside a folder being moved, re-run from a temp copy."""
    if os.environ.get("QUIETMAC_RELOCATED"):
        return
    root = os.path.realpath(str(core.REPO_ROOT))
    for p in paths:
        rp = os.path.realpath(os.path.abspath(os.path.expanduser(p)))
        if root == rp or root.startswith(rp + "/"):
            tmp = tempfile.mkdtemp(prefix="quietmac-run-")
            for sub in ("quietmac", "payload"):
                shutil.copytree(os.path.join(root, sub), os.path.join(tmp, sub))
            core.say(core.dim("quietmac lives inside %s, so it's re-running from a temporary copy (%s)" % (p, tmp)))
            os.execve(sys.executable, [sys.executable, "-E", "-s", "-c", BOOT, tmp] + sys.argv[1:],
                      dict(os.environ, QUIETMAC_RELOCATED="1"))


def cmd_migrate(a: argparse.Namespace) -> int:
    st = core.load_state()
    if a.action == "status":
        if a.json:
            _print_json(st["migrations"])
            return 0
        for src, m in st["migrations"].items():
            core.say("%s -> %s: %s%s" % (src, m.get("dst"), m.get("status"),
                                         ("; backup %s" % m["backup"]) if m.get("backup") else ""))
        if not st["migrations"]:
            core.say("no migrations")
        return 0
    if a.action == "cleanup":
        migrate.cleanup(st, yes=a.yes)
        return 0
    if not a.folders:
        raise core.QuietmacError("name the folders, e.g. quietmac migrate %s ~/os ~/ps" % a.action)
    if a.action == "plan":
        for p in a.folders:
            core.say(migrate.plan_text(migrate.preflight(st, p)))
        return 0
    if a.action == "run":
        _relocate_if_inside(a.folders)
        for p in a.folders:
            src = os.path.abspath(os.path.expanduser(p)).rstrip("/")
            if st["migrations"].get(src, {}).get("status") == "switched":
                core.say("finishing the interrupted migration of %s" % src)
                migrate.run_one(st, p, aliases=not a.no_claude_aliases)
                continue
            info = migrate.preflight(st, p)
            if not a.yes:
                core.say(migrate.plan_text(info))
                if info["problems"] or not core.confirm("Move %s onto the dev volume?" % info["src"], default=True):
                    continue
            migrate.run_one(st, p, verify=a.verify, ignore_busy=a.ignore_busy, aliases=not a.no_claude_aliases)
        return 0
    if a.action == "rollback":
        dsts = [st["migrations"].get(os.path.abspath(os.path.expanduser(p)).rstrip("/"), {}).get("dst") for p in a.folders]
        _relocate_if_inside(a.folders + [d for d in dsts if d])
        for p in a.folders:
            if core.confirm("Roll back %s?" % p, default=True, assume_yes=a.yes):
                migrate.rollback(st, p, keep_changes=a.keep_changes)
        return 0
    return 2


def cmd_cache(a: argparse.Namespace) -> int:
    st = core.load_state()
    if a.action == "list":
        k = cache.known(sizes=a.sizes)
        if a.json:
            _print_json(k)
            return 0
        for n, c in k.items():
            core.say("  %-20s %-9s %s%s  %s" % (n, "moved" if c["moved"] else ("present" if c["exists"] else "absent"),
                                                c["path"], (" (%s)" % core.human_bytes(c["bytes"])) if c["bytes"] else "",
                                                core.dim(c["why"])))
        return 0
    if not a.names:
        raise core.QuietmacError("name the caches: " + ", ".join(cache.CACHES))
    for n in a.names:
        if a.action == "move":
            cache.move(st, n, drop=a.drop)
        else:
            cache.restore(st, n, drop=a.drop)
    return 0


def cmd_adopt(a: argparse.Namespace) -> int:
    """Take over a hand-made setup from before quietmac (e.g. com.<you>.daemon-reaper
    and a WARP watcher), replacing it with the equivalent modules."""
    from .modules import reapers

    f = detect.detect()
    st = core.load_state()
    legacy = reaper.legacy_agents()
    plan_mods: List[str] = []
    opts: List[str] = []
    custom: List[str] = []
    widget_names = set(w["name"] for w in f["widgets"]["installed"])
    for lg in legacy:
        names = set(lg["names"])
        for m in reapers.MODULES:
            if m.id != "reap-widgets" and m.names and set(m.names) <= names:
                plan_mods.append(m.id)
                names -= set(m.names)
        wn = sorted(names & widget_names)
        if wn:
            plan_mods.append("reap-widgets")
            opts.append("reap-widgets.names=" + ",".join(wn))
            names -= set(wn)
        custom += sorted(n for n in names if not reaper.check_name(n))
    w = f["warp"]
    adopt_warp = w["installed"] and (w["legacy_watchers"] or w["legacy_sudoers"])
    if not legacy and not adopt_warp:
        core.say("no hand-made reaper or WARP watcher found; nothing to adopt")
        return 0
    core.say(core.bold("Found an earlier hand-made setup:"))
    for lg in legacy:
        core.say("  reaper %s (%s): %s" % (lg["label"], lg["plist"], ", ".join(lg["names"])))
    for lw in w["legacy_watchers"]:
        core.say("  WARP watcher %s (%s)" % (lw["label"], lw["plist"]))
    for s in w["legacy_sudoers"]:
        core.say("  sudoers rule %s" % s)
    core.say(core.bold("quietmac will:"))
    for mid in dict.fromkeys(plan_mods):
        core.say("  apply %s" % mid)
    if custom:
        core.say("  add to the kill list: %s" % ", ".join(custom))
    if adopt_warp:
        core.say("  apply warp-on-demand, replacing the old watcher and sudoers rule (backed up)")
    for lg in legacy:
        core.say("  stop %s and back up its plist to %s.quietmac-backup" % (lg["label"], lg["plist"]))
    if a.dry_run or not core.confirm("Adopt it?", default=True, assume_yes=a.yes):
        return 0
    if custom:
        st["reaper"]["custom"] = sorted(set(st["reaper"]["custom"]) | set(custom))
        core.save_state(st)
    mods = list(dict.fromkeys(plan_mods))
    rc = 0
    if mods:
        # adopting=1 tells the reaper modules not to warn about the hand-made reaper: it's retired below
        rc = _run_modules(argparse.Namespace(modules=mods, opt=opts + ["adopting=1"], dry_run=False, yes=True),
                          revert=False)
    elif custom:
        reaper.write_conf(core.load_state())
        core.run_steps(reaper.ensure_agent_steps(), "start the reaper")
    if rc:
        core.warn("a reaper module failed, so the hand-made reaper keeps running. Fix the error above and run "
                  "`quietmac adopt` again.")
    else:
        for lg in legacy:
            steps = [core.Step("stop %s" % lg["label"], argv=["/bin/launchctl", "bootout", core.gui(lg["label"])],
                               allow_fail=True),
                     core.Step("back up %s" % lg["plist"], fn=lambda p=lg["plist"]: os.rename(p, p + ".quietmac-backup"))]
            core.run_steps(steps, "retire the hand-made reaper")
    if adopt_warp:
        rc = _run_modules(argparse.Namespace(modules=["warp-on-demand"], opt=["replace_legacy=1"], dry_run=False,
                                             yes=True), revert=False) or rc
    if rc:
        core.say(core.yellow("\nadopt finished with errors (see above)."))
        return 1
    core.say(core.green("\nadopted.") + " The old scripts in ~/.local/bin are left in place but no longer run.")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quietmac", description=(
        "Lower idle CPU and longer battery life on macOS, without disabling SIP. Every change is a module you can "
        "plan, apply and revert. Start with: quietmac detect, quietmac list, quietmac setup."))
    from . import __version__

    p.add_argument("--version", action="version", version="quietmac " + __version__)
    sub = p.add_subparsers(dest="cmd")

    def add(name: str, fn: Any, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=help)
        sp.set_defaults(fn=fn)
        return sp

    s = add("detect", cmd_detect, "what this Mac has and how it's set (read-only)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--sizes", action="store_true", help="also measure folder sizes (slower)")
    s = add("list", cmd_list, "all modules with their current status")
    s.add_argument("--json", action="store_true")
    s = add("status", cmd_status, "what quietmac has applied, and drift")
    s.add_argument("--json", action="store_true")
    for name, fn, help in (("plan", cmd_plan, "explain modules and show exactly what apply would run"),
                           ("apply", cmd_apply, "apply modules"), ("revert", cmd_revert, "undo modules")):
        s = add(name, fn, help)
        s.add_argument("modules", nargs="+")
        s.add_argument("--opt", action="append", metavar="KEY=VALUE", help="module option, or module.key=value")
        if name != "plan":
            s.add_argument("--yes", action="store_true", help="don't ask (use after the user agreed)")
            s.add_argument("--dry-run", action="store_true")
    s = add("setup", cmd_setup, "interactive: ask questions, then apply what you choose")
    s.add_argument("--dry-run", action="store_true")
    s = add("questions", cmd_questions, "the setup questions for this Mac (agents: --json)")
    s.add_argument("--json", action="store_true")
    s = add("doctor", cmd_doctor, "check prerequisites and drift (re-run after macOS updates)")
    s.add_argument("--fix", action="store_true", help="restart/refresh the reaper if needed")
    s = add("reaper", cmd_reaper, "the daemon reaper: status, log, add, remove, pause, resume")
    s.add_argument("action", choices=["status", "log", "add", "remove", "pause", "resume"])
    s.add_argument("names", nargs="*")
    s.add_argument("--json", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--lines", type=int)
    s = add("probe", cmd_probe, "test whether a daemon stays dead if reaped, before adding it")
    s.add_argument("name")
    s.add_argument("--seconds", type=int, default=90)
    s.add_argument("--yes", action="store_true")
    s.add_argument("--json", action="store_true")
    s = add("measure", cmd_measure, "CPU now (and since boot), fseventsd's cost, reaper health")
    s.add_argument("--seconds", type=int, default=30)
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--since-boot", action="store_true")
    s.add_argument("--json", action="store_true")
    s = add("battery", cmd_battery, "battery drain history from the power log: per day, per unplugged session")
    s.add_argument("--capacity-wh", type=float, help="battery capacity to convert %%/hour to watts")
    s.add_argument("--json", action="store_true")
    s = add("devvol", cmd_devvol, "the dev volume: status, create, mount, destroy")
    s.add_argument("action", choices=["status", "create", "mount", "destroy"])
    s.add_argument("--name", default="Dev")
    s.add_argument("--mount", default="~/dev")
    s.add_argument("--container", help=argparse.SUPPRESS)
    s.add_argument("--force", action="store_true", help="destroy: also delete files left on the volume")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    s = add("migrate", cmd_migrate, "move project folders onto the dev volume, safely")
    s.add_argument("action", choices=["plan", "run", "status", "rollback", "cleanup"])
    s.add_argument("folders", nargs="*")
    s.add_argument("--verify", choices=["quick", "checksum"], default="quick")
    s.add_argument("--ignore-busy", action="store_true")
    s.add_argument("--keep-changes", action="store_true", help="rollback: copy current files back")
    s.add_argument("--no-claude-aliases", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--json", action="store_true")
    s = add("cache", cmd_cache, "move package/build caches onto the dev volume")
    s.add_argument("action", choices=["list", "move", "restore"])
    s.add_argument("names", nargs="*")
    s.add_argument("--drop", action="store_true", help="don't copy contents (caches rebuild themselves)")
    s.add_argument("--sizes", action="store_true")
    s.add_argument("--json", action="store_true")
    s = add("adopt", cmd_adopt, "take over a hand-made setup from before quietmac")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    p = build_parser()
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 0
    try:
        return a.fn(a) or 0
    except core.QuietmacError as e:
        print(core.red("quietmac: ") + str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
