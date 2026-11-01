"""privacy-profile: Apple Intelligence, Siri, analytics and ads off, via a
configuration profile (.mobileconfig).

A profile is Apple's supported switch for this and works with SIP on. The
alternative some tools take (rayhanadev/mds-bye) deletes model packs and
replaces system bundles, which needs SIP's filesystem protection off, and
re-applies itself every 5 minutes. The restriction keys below are the ones
macOS 15+ honors for manually installed profiles; the same set is used by
removemacai (io.github.omlahore.removemacai), which this was checked against.

Installing a profile always ends with you clicking Install in System
Settings: macOS doesn't allow it silently. quietmac writes the file, opens
it, and opens the right Settings pane.
"""

from __future__ import annotations

import plistlib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .. import core
from ..core import Step
from ..detect import managed_value
from .base import APPLIED, NOT_APPLIED, PARTIAL, Context, Module

IDENTIFIER = core.LABEL_PREFIX + ".privacy"
PROFILE = core.SHARE_DIR / "profiles" / "quietmac-privacy.mobileconfig"
RESTRICTIONS = "com.apple.applicationaccess"
BLOCKED = "https://127.0.0.1:9/quietmac-blocked/"

# group -> {"restrictions": {...}, "prefs": {domain: {...}}, "about": str}
GROUPS: Dict[str, Dict[str, Any]] = {
    "apple-intelligence": {
        "about": "Writing Tools, Genmoji, Image Playground, Image Wand, Mail/Safari/Notes/Messages summaries, "
                 "smart replies, math solving, ChatGPT and other external model integrations",
        "restrictions": {k: False for k in (
            "allowGenmoji", "allowImagePlayground", "allowImageWand", "allowWritingTools", "allowMailSmartReplies",
            "allowMailSummary", "allowNotesTranscriptionSummary", "allowSafariSummary",
            "allowExternalIntelligenceIntegrations", "allowExternalIntelligenceIntegrationsSignIn",
            "allowKeyboardMathSolving", "allowMathPaperSolving")},
        "prefs": {
            "com.apple.MobileSMS": {"messageSummarizationEnabled": False},
            "group.com.apple.mail": {"DisableAutomaticMessageSummarization": True, "PersonalizedSmartReplies": False},
            "group.com.apple.usernoted": {"summarize_previews": False},
            "com.apple.icloud.managed": {"DisableImagePlaygroundiCloudSetting": True},
        },
    },
    "ai-model-downloads": {
        "about": "stops macOS downloading Apple Intelligence model packs (several GB, plus Neural Engine compile "
                 "work when they arrive). Existing models stay until Apple Intelligence is turned off.",
        "restrictions": {},
        "prefs": {"com.apple.MobileAsset": {"DownloadServerBaseURLOverride-com.apple.MobileAsset." + a: BLOCKED for a in (
            "UAF.FM.CodeLM", "UAF.FM.GenerativeModels", "UAF.FM.Visual", "UAF.Photos.MagicCleanup",
            "UAF.Photos.SpatialPhotosRelive")}},
    },
    "siri": {
        "about": "Siri, 'Hey Siri', the Siri menu-bar item and Siri server logging",
        "restrictions": {"allowAssistant": False, "allowAssistantUserGeneratedContent": False},
        "prefs": {
            "com.apple.assistant.support": {"Assistant Enabled": False},
            "com.apple.Siri": {"StatusMenuVisible": False, "VoiceTriggerUserEnabled": False},
            "com.apple.assistant.managedconfiguration": {"allowSiriServerLogging": False},
        },
    },
    "spotlight-web": {
        "about": "Spotlight and Look Up internet suggestions (your typing isn't sent to Apple)",
        "restrictions": {"allowSpotlightInternetResults": False},
        "prefs": {"com.apple.lookup.shared": {"LookupSuggestionsDisabled": True}},
    },
    "analytics": {
        "about": "Mac Analytics, and 'Improve Siri & Dictation' and 'Improve Search' sharing",
        "restrictions": {"allowDiagnosticSubmission": False},
        "prefs": {
            "com.apple.SubmitDiagInfo": {"AutoSubmit": False},
            "com.apple.assistant.support": {"Siri Data Sharing Opt-In Status": 2,
                                            "Search Queries Data Sharing Status": 2},
        },
    },
    "ads": {
        "about": "Apple personalized ads",
        "restrictions": {"allowApplePersonalizedAdvertising": False},
        "prefs": {"com.apple.AdLib": {"allowApplePersonalizedAdvertising": False, "allowIdentifierForAdvertising": False}},
    },
}
DEFAULT_GROUPS = ["apple-intelligence", "siri", "spotlight-web", "analytics", "ads"]


def checks(group: str) -> List[Tuple[str, str, Any]]:
    g = GROUPS[group]
    out = [(RESTRICTIONS, k, v) for k, v in g["restrictions"].items()]
    if not out:
        out = [(d, k, v) for d, kv in g["prefs"].items() for k, v in kv.items()]
    return out


def group_enforced(group: str) -> bool:
    return all(managed_value(d, k) == v for d, k, v in checks(group))


def _uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, IDENTIFIER + "." + name)).upper()


def build_profile(groups: List[str]) -> Dict[str, Any]:
    restrictions: Dict[str, Any] = {}
    prefs: Dict[str, Dict[str, Any]] = {}
    for g in groups:
        restrictions.update(GROUPS[g]["restrictions"])
        for domain, kv in GROUPS[g]["prefs"].items():
            prefs.setdefault(domain, {}).update(kv)
    content = []
    if restrictions:
        content.append(dict(restrictions, PayloadType=RESTRICTIONS, PayloadVersion=1,
                            PayloadIdentifier=IDENTIFIER + ".restrictions", PayloadUUID=_uuid("restrictions"),
                            PayloadDisplayName="Restrictions"))
    if prefs:
        content.append({
            "PayloadType": "com.apple.ManagedClient.preferences", "PayloadVersion": 1,
            "PayloadIdentifier": IDENTIFIER + ".preferences", "PayloadUUID": _uuid("preferences"),
            "PayloadDisplayName": "Preferences",
            "PayloadContent": {d: {"Forced": [{"mcx_preference_settings": kv}]} for d, kv in sorted(prefs.items())},
        })
    return {
        "PayloadType": "Configuration", "PayloadVersion": 1, "PayloadScope": "System",
        "PayloadIdentifier": IDENTIFIER, "PayloadUUID": _uuid("profile"),
        "PayloadDisplayName": "quietmac: " + ", ".join(groups),
        "PayloadDescription": "Generated by quietmac. Turns off: " + "; ".join(GROUPS[g]["about"] for g in groups)
        + ". Remove it in System Settings > General > Device Management.",
        "PayloadOrganization": "quietmac (local)",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": content,
    }


class PrivacyProfile(Module):
    id = "privacy-profile"
    title = "Turn off Apple Intelligence, Siri, analytics and ads (configuration profile)"
    category = "Apple Intelligence & privacy"
    guided = True
    summary = "writes a .mobileconfig with the groups you pick, opens it, and opens System Settings so you click Install"
    benefit = ("stops the AI/Siri feature stack and model downloads at the source, the supported way, with SIP on. "
               "Pair it with reap-siri-helpers for the daemons that still start.")
    cost = "the features in the groups you choose (see `quietmac plan privacy-profile`)"
    options = {"groups": "comma-separated: " + ", ".join(GROUPS) + " (default: all but ai-model-downloads)"}

    def groups(self, ctx: Context) -> List[str]:
        raw = ctx.opt("groups")
        chosen = [g.strip() for g in raw.split(",")] if raw else list(DEFAULT_GROUPS)
        bad = [g for g in chosen if g not in GROUPS]
        if bad:
            raise core.QuietmacError("unknown group(s) %s; choose from %s" % (", ".join(bad), ", ".join(GROUPS)))
        return chosen

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        wanted = st["modules"].get(self.id, {}).get("groups", DEFAULT_GROUPS)
        done = [g for g in wanted if group_enforced(g)]
        others = [g for g in GROUPS if g not in wanted and group_enforced(g)]
        detail = "enforced: %s" % (", ".join(done + others) or "none")
        if len(done) == len(wanted):
            return APPLIED, detail + ("" if self.id in st["modules"] else " (by a profile installed outside quietmac)")
        if self.id in st["modules"]:
            return PARTIAL, detail + "; waiting for you to click Install in System Settings > General > Device Management"
        return (PARTIAL if done else NOT_APPLIED), detail

    def apply_steps(self, ctx: Context) -> List[Step]:
        groups = self.groups(ctx)
        data = build_profile(groups)

        def write() -> None:
            PROFILE.parent.mkdir(parents=True, exist_ok=True)
            with open(PROFILE, "wb") as fh:
                plistlib.dump(data, fh)
            core.log_action("wrote %s (groups: %s)" % (PROFILE, ", ".join(groups)))

        return [
            Step("write %s with: %s" % (PROFILE, ", ".join(groups)), fn=write),
            Step("hand the profile to System Settings", argv=["/usr/bin/open", str(PROFILE)]),
            Step("open System Settings > General > Device Management",
                 argv=["/usr/bin/open", "x-apple.systempreferences:com.apple.Profiles-Settings.extension"], allow_fail=True),
            Step("YOU: double-click 'quietmac: ...' under Downloaded, click Install, enter your password", fn=lambda: None),
            self.record_step(ctx, groups=groups, identifier=IDENTIFIER, file=str(PROFILE)),
        ]

    def revert_steps(self, ctx: Context) -> List[Step]:
        return [
            Step("remove the profile %s" % IDENTIFIER,
                 script="/usr/bin/profiles remove -identifier %s >/dev/null 2>&1 || "
                        "echo 'remove it in System Settings > General > Device Management'" % IDENTIFIER),
            self.forget_step(ctx),
        ]


MODULES = [PrivacyProfile()]
