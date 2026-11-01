"""The questions to ask before changing anything, and what each answer applies.

One source of truth for:
  * `quietmac setup` (interactive, for a human in a terminal)
  * `quietmac questions --json` (for an agent following INSTALL.md: it asks
    these with its own UI, then runs `quietmac apply ...`)

Every question is filtered by `detect` facts: no Bluetooth question on a Mac
whose Bluetooth is already off, no WARP question without WARP, and so on.
Option labels stay short (they're shown as buttons); `apply` lists the
module ids to apply and `opts` any --opt values.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Tuple

from .modules import privacy

Facts = Dict[str, Any]


def _applied(f: Facts, mid: str) -> bool:
    return mid in f["quietmac"]["applied"]


def _bt_question(f: Facts) -> str:
    devs = f["bluetooth"]["devices"]
    if devs:
        names = ", ".join("%s (%s)" % (d["name"], d["type"]) for d in devs[:6])
        return ("Bluetooth uses 1-3%% of a core even with nothing connected. Paired: %s. Turn it off and switch it on "
                "from Control Center only when you need these?" % names)
    return "Bluetooth uses 1-3% of a core even with nothing connected. Turn it off (switch it on from Control Center when needed)?"


QUESTIONS: List[Dict[str, Any]] = [
    # ---- wireless
    {"id": "bluetooth", "batch": 1, "header": "Bluetooth",
     "when": lambda f: (f["bluetooth"]["power"] is True, "Bluetooth is already off"),
     "question": _bt_question,
     "options": [
         {"label": "Turn it off", "description": "AirPods, Watch unlock and BT keyboards/mice stop until you turn it on",
          "apply": ["bluetooth-off"]},
         {"label": "Keep it on", "description": "I use Bluetooth devices daily", "apply": []}],
     "recommended": None,
     "notes": "If a Bluetooth keyboard/mouse/trackpad is paired and the Mac has no built-in keyboard, don't offer "
              "'Turn it off' unless they confirm a wired one."},
    {"id": "handoff", "batch": 1, "header": "Handoff",
     "when": lambda f: (f["continuity"]["handoff"], "Handoff is already off"),
     "question": "Do you use Handoff or Universal Clipboard (copy on iPhone, paste on the Mac)?",
     "options": [
         {"label": "No, turn it off", "description": "useractivityd and rapportd go quiet", "apply": ["handoff-off"]},
         {"label": "Yes, keep it", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "airdrop", "batch": 1, "header": "AirDrop",
     "when": lambda f: (f["continuity"]["airdrop"] != "Off", "AirDrop is already No One"),
     "question": "Do you receive files with AirDrop? (Sending keeps working either way.)",
     "options": [
         {"label": "No, set No One", "description": "sharingd stops scanning and advertising", "apply": ["airdrop-off"]},
         {"label": "Yes, keep it", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "universal-control", "batch": 1, "header": "Univ. Ctrl",
     "when": lambda f: (f["continuity"]["universal_control"], "Universal Control is already off"),
     "question": "Do you control an iPad or another Mac with this Mac's keyboard and trackpad (Universal Control)?",
     "options": [
         {"label": "No, turn it off", "description": "", "apply": ["universal-control-off"]},
         {"label": "Yes, keep it", "description": "", "apply": []}],
     "recommended": 0},
    # ---- Apple features
    {"id": "siri", "batch": 2, "header": "Siri",
     "when": lambda f: (not _applied(f, "reap-siri-helpers"), "already applied"),
     "question": "Do you use Siri? If not, its helper daemons can be killed on sight (sirittsd alone holds ~275 MB).",
     "options": [
         {"label": "No, kill its helpers", "description": "also turn Siri off in the privacy profile",
          "apply": ["reap-siri-helpers"]},
         {"label": "Yes, I use Siri", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "apple-intelligence", "batch": 2, "header": "AI & privacy", "multi": True,
     "when": lambda f: (not all(privacy.group_enforced(g) for g in privacy.DEFAULT_GROUPS),
                        "a configuration profile already enforces all of this"),
     "question": "Which of these should a configuration profile turn off? (You click Install in System Settings.)",
     "options": [
         {"label": "Apple Intelligence", "description": privacy.GROUPS["apple-intelligence"]["about"],
          "apply": ["privacy-profile"], "opts": {"privacy-profile.groups": "apple-intelligence"}},
         {"label": "AI model downloads", "description": privacy.GROUPS["ai-model-downloads"]["about"],
          "apply": ["privacy-profile"], "opts": {"privacy-profile.groups": "ai-model-downloads"}},
         {"label": "Siri", "description": privacy.GROUPS["siri"]["about"],
          "apply": ["privacy-profile"], "opts": {"privacy-profile.groups": "siri"}},
         {"label": "Analytics, web, ads", "description": "Mac Analytics, Spotlight web suggestions, personalized ads",
          "apply": ["privacy-profile"], "opts": {"privacy-profile.groups": "analytics,spotlight-web,ads"}}],
     "recommended": None,
     "notes": "Merge the chosen options' groups into one --opt privacy-profile.groups=a,b,c."},
    {"id": "shortcuts", "batch": 2, "header": "Shortcuts",
     "when": lambda f: (not _applied(f, "reap-shortcuts"), "already applied"),
     "question": "Do you use the Shortcuts app or shortcut automations?",
     "options": [
         {"label": "No, kill its engine", "description": "siriactionsd stops starting at login", "apply": ["reap-shortcuts"]},
         {"label": "Yes", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "suggestions", "batch": 2, "header": "Suggestions",
     "when": lambda f: (not _applied(f, "reap-suggestions"), "already applied"),
     "question": "Do you rely on Siri Suggestions (app and action predictions in Spotlight and the share sheet)?",
     "options": [
         {"label": "No, kill duetexpertd", "description": "borderline: it sometimes respawns after boot; capped by the churn guard",
          "apply": ["reap-suggestions"]},
         {"label": "Yes", "description": "", "apply": []}],
     "recommended": 0},
    # ---- background daemons
    {"id": "appstore", "batch": 3, "header": "App Store",
     "when": lambda f: (not _applied(f, "reap-appstore"), "already applied"),
     "question": "Do you install or update apps from the Mac App Store often? appstoreagent survives reboots even when disabled.",
     "options": [
         {"label": "Rarely, kill it", "description": "pause the reaper (`quietmac reaper pause`) when you do update",
          "apply": ["reap-appstore"]},
         {"label": "Often, keep it", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "photos", "batch": 3, "header": "Photos",
     "when": lambda f: (not _applied(f, "reap-media-analysis"), "already applied"),
     "question": "Do you use Photos' People, Memories and search, or Visual Look Up? mediaanalysisd once kept the Neural "
                 "Engine compiler at 95% of a core.",
     "options": [
         {"label": "No, kill analysis", "description": "mediaanalysisd, photoanalysisd, visualintelligenced",
          "apply": ["reap-media-analysis"]},
         {"label": "Yes, keep it", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "widgets", "batch": 3, "header": "Widgets",
     "when": lambda f: (bool(f["widgets"]["installed"]) and not _applied(f, "reap-widgets"), "nothing to do"),
     "question": lambda f: "Do you use desktop or Notification Center widgets? Widget processes running now: %s." % (
         ", ".join(f["widgets"]["running"]) or "none"),
     "options": [
         {"label": "No, kill them", "description": "every app widget extension; chronod and Control Center stay",
          "apply": ["reap-widgets", "nc-widgets-remove"]},
         {"label": "Yes, keep them", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "spotlight", "batch": 3, "header": "Spotlight",
     "when": lambda f: (f["spotlight"]["data_volume_enabled"], "Spotlight indexing is already off"),
     "question": "Do you search for files with Spotlight, Finder or Mail search?",
     "options": [
         {"label": "No, stop indexing", "description": "mds/mdworker go quiet; file search stops working",
          "apply": ["spotlight-off"]},
         {"label": "Yes, keep it", "description": "tip: a dev volume keeps code out of the index instead", "apply": []}],
     "recommended": None},
    # ---- power and apps
    {"id": "low-power", "batch": 4, "header": "Low Power",
     "when": lambda f: (f["macos"]["laptop"] and f["power"]["battery"].get("lowpowermode", "0") != "1",
                        "not a laptop, or already on"),
     "question": "Turn on Low Power Mode automatically whenever you're on battery? (The biggest single battery win.)",
     "options": [
         {"label": "Yes, on battery", "description": "builds are slower unplugged", "apply": ["low-power-on-battery"]},
         {"label": "No", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "display-sleep", "batch": 4, "header": "Display",
     "when": lambda f: (f["macos"]["laptop"] and f["power"]["battery"].get("displaysleep") == "0",
                        "the display already turns off when idle"),
     "question": "On battery your display never turns off when idle (displaysleep 0). Turn it off after 5 minutes of inactivity?",
     "options": [
         {"label": "After 5 minutes", "description": "", "apply": ["display-sleep-on-battery"],
          "opts": {"display-sleep-on-battery.value": "5"}},
         {"label": "After 10 minutes", "description": "", "apply": ["display-sleep-on-battery"],
          "opts": {"display-sleep-on-battery.value": "10"}},
         {"label": "Never (keep)", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "powernap", "batch": 4, "header": "Power Nap",
     "when": lambda f: (f["macos"]["laptop"] and f["power"]["battery"].get("powernap") == "1", "already off"),
     "question": "Turn off Power Nap on battery (no mail/iCloud updates while asleep unplugged)?",
     "options": [
         {"label": "Yes, turn it off", "description": "", "apply": ["powernap-off-on-battery"]},
         {"label": "No", "description": "", "apply": []}],
     "recommended": 0},
    {"id": "warp", "batch": 4, "header": "WARP",
     "when": lambda f: (f["warp"]["installed"] and not f["warp"]["watcher_loaded"], "WARP not installed, or already on demand"),
     "question": "Cloudflare WARP's daemon runs all the time, even disconnected. Run it only while the WARP app is open?",
     "options": [
         {"label": "Yes, on demand", "description": "no password needed later (a 4-command sudoers rule)",
          "apply": ["warp-on-demand"]},
         {"label": "No, leave it", "description": "", "apply": []}],
     "recommended": 0},
    # ---- development
    {"id": "dev-volume", "batch": 5, "header": "Dev volume",
     "when": lambda f: (bool(f["dev"]["projects"]) and not f["devvol"].get("exists"), "no projects found, or it exists"),
     "question": lambda f: ("fseventsd has averaged %.2f%% of a core since boot. With many agents/builds in parallel it "
                            "can climb over days. Create a dev volume (~/dev) with fseventsd logging and Spotlight off, "
                            "and move projects onto it? It's not covered by FileVault." % f["fseventsd"]["percent_of_core"]),
     "options": [
         {"label": "Yes, at ~/dev", "description": "then pick folders to move", "apply": ["dev-volume"],
          "opts": {"dev-volume.mount": "~/dev"}},
         {"label": "Not now", "description": "re-check with `quietmac measure` after a few days", "apply": []}],
     "recommended": 1,
     "notes": "Recommend 'Not now' unless fseventsd is above ~2% of a core or they run many agents in parallel. "
              "If yes, follow up with the migrate and caches questions."},
    {"id": "migrate", "batch": 5, "header": "Move folders", "multi": True,
     "when": lambda f: (bool(f["dev"]["projects"]), "no project folders found"),
     "question": "Which folders should move onto the dev volume? (Old paths keep working as links.)",
     "options": lambda f: [{"label": os.path.basename(p["path"]), "description": "%d git repos" % p["repos"],
                            "migrate": [p["path"]]} for p in f["dev"]["projects"][:4]],
     "recommended": None,
     "notes": "Only after the dev volume exists. Run `quietmac migrate run <folders>` from ~ with nothing open inside them."},
    {"id": "caches", "batch": 5, "header": "Caches", "multi": True,
     "when": lambda f: (any(c["exists"] for c in f["dev"]["caches"].values()), "no caches found"),
     "question": "Which caches should follow the projects onto the dev volume?",
     "options": lambda f: [{"label": n, "description": c["why"], "cache": [n]}
                           for n, c in sorted(f["dev"]["caches"].items(), key=lambda kv: not kv[1]["required_with_devvol"])
                           if c["exists"]][:4],
     "recommended": None,
     "notes": "bun and pnpm are required if their projects move (clones/hard links only work within one volume)."},
]


def evaluate(f: Facts) -> List[Dict[str, Any]]:
    """Questions for this Mac with dynamic text resolved; JSON-safe."""
    out = []
    for q in QUESTIONS:
        ok, reason = q["when"](f)
        text = q["question"](f) if callable(q["question"]) else q["question"]
        opts = q["options"](f) if callable(q["options"]) else q["options"]
        out.append({"id": q["id"], "batch": q["batch"], "header": q["header"], "question": text,
                    "multi": q.get("multi", False), "ask": bool(ok), "skip_reason": "" if ok else reason,
                    "recommended": q.get("recommended"), "options": opts, "notes": q.get("notes", "")})
    return out
