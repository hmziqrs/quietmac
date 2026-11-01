#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a tag, e.g. `release_notes.py v0.1.0`."""

import re
import sys
from pathlib import Path

version = sys.argv[1].lstrip("v")
text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text()
m = re.search(r"^## \[%s\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: )" % re.escape(version), text, re.M | re.S)
if not m:
    sys.exit("no CHANGELOG.md section for %s" % version)
print(m.group(1).strip())
