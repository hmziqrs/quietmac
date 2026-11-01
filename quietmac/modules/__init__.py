"""Registry of all modules, in the order they're shown."""

from __future__ import annotations

from typing import Dict, List

from .base import Module
from . import continuity, extra, privacy, reapers, system, warp

ALL: List[Module] = (reapers.MODULES + continuity.MODULES + warp.MODULES + system.MODULES + privacy.MODULES
                     + extra.MODULES)
BY_ID: Dict[str, Module] = {m.id: m for m in ALL}


def get(mid: str) -> Module:
    try:
        return BY_ID[mid]
    except KeyError:
        from ..core import QuietmacError

        raise QuietmacError("unknown module %r. `quietmac list` shows them all." % mid)
