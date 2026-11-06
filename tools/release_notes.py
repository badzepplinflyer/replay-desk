"""Print the CHANGELOG.md section for a version (used as the GitHub release notes,
which the app shows under "What's new")."""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")   # notes may contain emoji; Windows console defaults to cp1252

ROOT = Path(__file__).resolve().parent.parent
if sys.argv[1:] == ["--version"]:          # print the app version from rd/version.py
    print(re.search(r'__version__ = "(.+?)"', ROOT.joinpath("rd", "version.py").read_text("utf-8")).group(1))
    sys.exit(0)
version = sys.argv[1].lstrip("v") if len(sys.argv) > 1 else ""
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text("utf-8")
m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
print(m.group(1).strip() if m else "Improvements and fixes.")
