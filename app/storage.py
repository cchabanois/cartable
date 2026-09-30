"""Plain-file storage: everything lives under CARTABLE_DATA (default ./data).

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
from datetime import datetime
from pathlib import Path

# Endpoints run in a thread pool: serialize read-modify-write cycles.
lock = threading.RLock()


def data_dir() -> Path:
    path = Path(os.environ.get("CARTABLE_DATA", "data"))
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
