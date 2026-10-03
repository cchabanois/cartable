"""Plain-file storage: everything lives under NOTOSAURUS_DATA (default ./data).

data/
  prompts.json
  lessons/<yyyy-mm-dd-deck-slug>/lesson.json, page-N.jpg, audio/*.mp3
  cache/tts/            voice previews, safe to delete
"""

import json
import os
import re
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .errors import AppError

# Endpoints run in a thread pool: serialize read-modify-write cycles.
lock = threading.RLock()


def data_dir() -> Path:
    path = Path(os.environ.get("NOTOSAURUS_DATA", "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify(text: str, max_length: int = 40) -> str:
    """ "Espagnol::Leçon 5 - La famille" → "espagnol-lecon-5-la-famille"."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_length].rstrip("-")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class DataTooNew(AppError):
    """Data written by a newer Notosaurus: not read, so this older one can't damage it."""

    status = 409

    def __init__(self, what: str, version: int, known: int):
        super().__init__("data.too_new", what=what)
        self.detail_text = f"{what}: format {version}, this Notosaurus knows up to {known}"


def migrate(data: dict, what: str, current: int, steps: dict[int, Callable[[dict], dict]]) -> dict:
    """Bring data read from a file up to the `current` format, one step at a time:
    steps[n] turns format n into n + 1. Files without "format" are format 0 (written
    before formats). Data from a newer format is refused (DataTooNew)."""
    version = data.get("format", 0)
    if not isinstance(version, int) or version > current:
        raise DataTooNew(what, version, current)
    while version < current:
        data = steps[version](data)
        version += 1
    return {**data, "format": current}


def write_json(path: Path, data) -> None:
    """Write through a temporary file then rename, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
