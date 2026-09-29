"""Languages: one JSON file per language in static/i18n/, shared by the page,
the server (default prompts, language names) and the Anki add-on (menu).

Adding a language = adding static/i18n/<code>.json (copy en.json); missing keys
fall back to English.
"""

import json
import os
from functools import cache
from pathlib import Path

I18N_DIR = Path(__file__).parent.parent / "static" / "i18n"
DEFAULT = "en"


def available() -> list[str]:
    return sorted(p.stem for p in I18N_DIR.glob("*.json"))


def resolve(*candidates: str | None) -> str | None:
    """First candidate we have a file for: "pt-BR" → "pt-br" if present, else "pt"."""
    langs = available()
    for candidate in candidates:
        if not candidate:
            continue
        code = candidate.replace("_", "-").lower()
        for option in (code, code.split("-")[0]):
            if option in langs:
                return option
    return None


def anki_language() -> str | None:
    """Language of the Anki window, when the server runs inside the Anki add-on."""
    return resolve(os.environ.get("CARTABLE_LANG")) or (DEFAULT if os.environ.get("CARTABLE_LANG") else None)


@cache
def messages(lang: str) -> dict:
    try:
        return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def get(lang: str, key: str, default=None):
    """Value at a dotted key ("meta.englishName"), falling back to English."""
    for source in (messages(lang), messages(DEFAULT)):
        value = source
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            return value
    return default


def language_name(lang: str) -> str:
    """English name of the language, for instructions sent to the AI ("French")."""
    return get(lang, "meta.englishName", "English")
