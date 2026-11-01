"""Module interface. A module is one switch the user can say yes or no to.

Each module produces Steps for apply and revert, so `plan` and `--dry-run`
print exactly what would run. apply() records the previous values it needs in
state["modules"][id]; revert() restores from that record, never from guesses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import core
from ..core import Step

APPLIED, NOT_APPLIED, PARTIAL, MANUAL, NA = "applied", "not applied", "partial", "manual", "n/a"


class Context:
    def __init__(self, facts: Dict[str, Any], state: Dict[str, Any], opts: Optional[Dict[str, str]] = None,
                 dry_run: bool = False, yes: bool = False):
        self.facts, self.state, self.opts = facts, state, dict(opts or {})
        self.dry_run, self.yes = dry_run, yes

    def opt(self, key: str, default: Any = None) -> Any:
        return self.opts.get(key, default)

    def record(self, mid: str) -> Dict[str, Any]:
        return self.state["modules"].get(mid, {})


class Module:
    id = ""
    title = ""
    category = ""
    summary = ""   # what it changes
    benefit = ""   # why bother (measured where we have numbers)
    cost = ""      # what stops working
    admin = False  # needs the admin password
    guided = False  # the user does it by hand; quietmac opens the right place
    options: Dict[str, str] = {}

    def applicable(self, facts: Dict[str, Any]) -> Tuple[bool, str]:
        return True, ""

    def status(self, facts: Dict[str, Any], st: Dict[str, Any]) -> Tuple[str, str]:
        raise NotImplementedError

    def apply_steps(self, ctx: Context) -> List[Step]:
        raise NotImplementedError

    def revert_steps(self, ctx: Context) -> List[Step]:
        raise NotImplementedError

    # helpers -------------------------------------------------------------

    def record_step(self, ctx: Context, **data: Any) -> Step:
        def fn() -> None:
            ctx.state["modules"][self.id] = dict(data, applied_at=core.now_iso())
            core.save_state(ctx.state)
            core.log_action("APPLIED %s %s" % (self.id, data if data else ""))
        return Step("record %s in quietmac state" % self.id, fn=fn)

    def forget_step(self, ctx: Context) -> Step:
        def fn() -> None:
            ctx.state["modules"].pop(self.id, None)
            core.save_state(ctx.state)
            core.log_action("REVERTED %s" % self.id)
        return Step("remove %s from quietmac state" % self.id, fn=fn)

    def describe(self) -> str:
        parts = [core.bold("%s" % self.id) + "  " + self.title]
        for label, text in (("does", self.summary), ("why", self.benefit), ("you lose", self.cost)):
            if text:
                parts.append("    %-9s %s" % (label + ":", text))
        if self.admin:
            parts.append("    %-9s %s" % ("needs:", "your admin password (a macOS dialog, or sudo in a terminal)"))
        if self.options:
            for k, v in self.options.items():
                parts.append("    %-9s --opt %s=...  %s" % ("option:", k, v))
        return "\n".join(parts)
