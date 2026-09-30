"""Print the release notes of a version, from CHANGELOG.md.

Usage: python tools/changelog_section.py 0.1.0
The section "## [0.1.0]" if the changelog has it (release being finalized),
otherwise "## [Unreleased]" (changes merged since the last release).
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(title: str, text: str) -> str | None:
    match = re.search(rf"^## \[{re.escape(title)}\][^\n]*\n(.*?)(?=^## |^\[[^\]]+\]: |\Z)", text, re.M | re.S)
    return match.group(1).strip() if match else None


def notes(version: str) -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    found = section(version, text)
    if found is None:
        found = section("Unreleased", text)
    return found or "No changes listed yet."


if __name__ == "__main__":
    print(notes(sys.argv[1]))
