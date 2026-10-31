"""Print the CHANGELOG.md section for a version (used as the GitHub release notes,
which the app shows under "What's new")."""
import re
import sys
from pathlib import Path

version = sys.argv[1].lstrip("v") if len(sys.argv) > 1 else ""
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text("utf-8")
m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
print(m.group(1).strip() if m else "Improvements and fixes.")
