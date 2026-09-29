"""Card audio, generated with edge-tts (free Microsoft neural voices).

Each mp3 is named after the text plus a hash of (voice, rate, text), e.g.
"la-madre-3f2a1c9e.mp3". Card audio lives in the lesson's audio/ folder;
previews go to data/cache/tts/ and are reused when the lesson is exported.
"""

import asyncio
import hashlib
import logging
import re
import shutil
from pathlib import Path

import edge_tts

from . import settings, storage

log = logging.getLogger("cartable")

MAX_PARALLEL = 4

# "es_ES": {{tts}} tag read by Anki itself; "es-ES-ElviraNeural": edge-tts mp3.
ANKI_LOCALE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}$")

_voices: list[dict] | None = None


def cache_dir() -> Path:
    path = storage.data_dir() / "cache" / "tts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_anki_locale(voice: str) -> bool:
    return bool(ANKI_LOCALE.match(voice))


def rate() -> str:
    return settings.current().tts_rate  # e.g. "-10%": slightly slower, for learners


def filename(text: str, voice: str) -> str:
    # Anki keeps all media in one folder: the hash keeps names unique across
    # voices, rates and lessons, the slug keeps them readable.
    digest = hashlib.sha1(f"{voice}|{rate()}|{text}".encode()).hexdigest()[:8]
    return f"{storage.slugify(text, 30) or 'audio'}-{digest}.mp3"


async def _synthesize(text: str, voice: str, path: Path) -> None:
    tmp = path.with_suffix(".part")
    await edge_tts.Communicate(text, voice, rate=rate()).save(str(tmp))
    tmp.rename(path)  # never leave a half-written mp3 behind


async def tts(text: str, voice: str, directory: Path | None = None) -> Path:
    """mp3 of `text` in `directory` (default: preview cache), synthesized if missing."""
    name = filename(text, voice)
    path = (directory or cache_dir()) / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        cached = cache_dir() / name
        if directory and cached.exists():
            shutil.copyfile(cached, path)
        else:
            await _synthesize(text, voice, path)
    return path


async def tts_many(texts: list[str], voice: str, directory: Path) -> tuple[dict[str, Path], int]:
    """Audio for every text in `directory`. Returns {text: mp3} and the number of failures."""
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def one(text: str) -> tuple[str, Path | None]:
        async with sem:
            try:
                return text, await tts(text, voice, directory)
            except Exception as e:  # network, unknown voice…: the card goes out without sound
                log.warning("TTS %s %r: %s", voice, text, e)
                return text, None

    results = await asyncio.gather(*(one(t) for t in dict.fromkeys(texts)))
    audio = {t: p for t, p in results if p}
    return audio, len(results) - len(audio)


def prune(directory: Path, keep: set[Path]) -> None:
    """Delete mp3s no card uses any more (edited backs, changed voice)."""
    for mp3 in directory.glob("*.mp3"):
        if mp3 not in keep:
            mp3.unlink()


async def voices() -> list[dict]:
    """Available voices (short name, locale, gender), cached in memory."""
    global _voices
    if _voices is None:
        _voices = sorted(
            (
                {"voice": v["ShortName"], "locale": v["Locale"], "gender": v["Gender"]}
                for v in await edge_tts.list_voices()
            ),
            key=lambda v: v["voice"],
        )
    return _voices
