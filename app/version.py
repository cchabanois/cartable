"""Notosaurus's version, read from pyproject.toml (packaged with the server)."""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

try:
    VERSION: str = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
except (OSError, KeyError, tomllib.TOMLDecodeError):
    VERSION = "unknown"
