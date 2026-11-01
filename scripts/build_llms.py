#!/usr/bin/env python3
"""Build llms-full.txt: every doc linked from llms.txt, concatenated.

    python3 scripts/build_llms.py           # rewrite llms-full.txt
    python3 scripts/build_llms.py --check   # exit 1 if it's out of date (CI)
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "INSTALL.md", "docs/MODULES.md", "docs/DEV-VOLUME.md", "docs/REFERENCE.md", "AGENTS.md",
        "docs/BACKGROUND.md", "docs/measurements/README.md", "SECURITY.md"]


def build() -> str:
    head = ROOT.joinpath("llms.txt").read_text().split("\n## ")[0].rstrip()
    parts = [head, "", "This file contains the full text of every document listed in llms.txt, in that order.", ""]
    for rel in DOCS:
        parts += ["", "---", "", "<!-- file: %s -->" % rel, "", ROOT.joinpath(rel).read_text().rstrip(), ""]
    return "\n".join(parts) + "\n"


def main() -> int:
    out = ROOT / "llms-full.txt"
    text = build()
    if "--check" in sys.argv:
        if not out.exists() or out.read_text() != text:
            print("llms-full.txt is out of date: run python3 scripts/build_llms.py", file=sys.stderr)
            return 1
        return 0
    out.write_text(text)
    print("wrote %s (%d lines)" % (out.relative_to(ROOT), text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
