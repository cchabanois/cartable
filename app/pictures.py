"""Pictures for cards ("front: the picture of the word"), drawn by an image model.

The AI that writes the cards says what to draw (Card.picture_prompt); an image
model draws it. Claude can't draw: the image model is a setting of its own
(`picture_model`), used through OpenRouter ("google/…", "openai/…"), Gemini
("gemini-…") or OpenAI ("gpt-image-…"), with the matching key.
"""

import asyncio
import base64
import hashlib
import io
import logging
import re
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps

from . import llm, settings, storage
from .errors import AppError
from .models import Card
from .settings import Settings

log = logging.getLogger("cartable")

OPENROUTER = "https://openrouter.ai/api/v1"
NAME = re.compile(r"^picture-[a-z0-9]+-[a-f0-9]{8}\.jpg$")  # files we write; blocks "../"
SIDE = 512  # on a card: clear enough, light to sync
QUALITY = 80
CONCURRENCY = 4
STYLE = (
    "A simple, clear picture for a child's vocabulary flashcard showing {subject}. Friendly flat "
    "illustration, bright colours, plain white background, the subject centred and filling the picture. "
    "No text, no letters, no numbers."
)


class PictureError(AppError):
    status = 502


def model(s: Settings) -> str:
    """The image model: the one set, or Gemini Flash Lite Image (fast, the cheapest)
    through the service used for the cards when it can draw, else through OpenRouter,
    else Gemini. A free Gemini key can't draw: it comes last. "" when no key allows any."""
    if s.picture_model.strip():
        return s.picture_model.strip()
    openrouter = bool(s.openai_keys.get(OPENROUTER))
    if s.llm == "gemini" and s.gemini_api_key:
        return "gemini-3.1-flash-lite-image"
    if openrouter:
        return "google/gemini-3.1-flash-lite-image"
    if s.gemini_api_key:
        return "gemini-3.1-flash-lite-image"
    return ""


async def draw(s: Settings, subject: str) -> bytes:
    """One picture (PNG/JPEG bytes from the model), its cost recorded."""
    name = model(s)
    if not name:
        raise PictureError("picture.no_model")
    prompt = STYLE.format(subject=subject.strip())
    if "/" in name:
        return await _openrouter(s, name, prompt)
    if name.startswith("gemini"):
        return await _gemini(s, name, prompt)
    if name.startswith(("gpt-image", "dall-e")):
        return await _openai(s, name, prompt)
    raise PictureError("picture.unknown_model", model=name)


async def _openrouter(s: Settings, name: str, prompt: str) -> bytes:
    import openai

    key = s.openai_keys.get(OPENROUTER)
    if not key:
        raise PictureError("picture.missing_key", service="OpenRouter")
    client = openai.AsyncOpenAI(base_url=OPENROUTER, api_key=key)
    try:
        response = await client.chat.completions.create(
            model=name,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"modalities": ["image", "text"], "usage": {"include": True}},
        )
    except openai.RateLimitError as e:
        raise PictureError("picture.quota", service="OpenRouter") from e
    except openai.OpenAIError as e:
        raise PictureError("picture.failed", detail=str(e)[:200]) from e
    if response.usage:
        cost = (response.usage.model_extra or {}).get("cost")
        await llm.record(s, "openrouter.ai", name, response.usage.prompt_tokens, response.usage.completion_tokens, cost)
    images = (response.choices[0].message.model_extra or {}).get("images") or []
    if not images:
        raise PictureError("picture.empty")
    return base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1])


async def _gemini(s: Settings, name: str, prompt: str) -> bytes:
    from google import genai
    from google.genai import errors, types

    if not s.gemini_api_key:
        raise PictureError("picture.missing_key", service="Gemini")
    client = genai.Client(api_key=s.gemini_api_key)
    try:
        response = await client.aio.models.generate_content(
            model=name, contents=prompt, config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"])
        )
    except errors.APIError as e:
        if e.code == 429:
            raise PictureError("picture.quota", service="Gemini") from e
        raise PictureError("picture.failed", detail=f"{e.code} {e.message}"[:200]) from e
    usage = response.usage_metadata
    if usage:
        await llm.record(s, "gemini", name, usage.prompt_token_count, usage.candidates_token_count)
    for part in (response.candidates[0].content.parts if response.candidates else []) or []:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data
    raise PictureError("picture.empty")


async def _openai(s: Settings, name: str, prompt: str) -> bytes:
    import openai

    key = s.openai_keys.get("https://api.openai.com/v1") or s.openai_api_key
    if not key:
        raise PictureError("picture.missing_key", service="OpenAI")
    client = openai.AsyncOpenAI(api_key=key)
    try:
        response = await client.images.generate(model=name, prompt=prompt, size="1024x1024", quality="low")
    except openai.OpenAIError as e:
        raise PictureError("picture.failed", detail=str(e)[:200]) from e
    if response.usage:
        await llm.record(s, "openai", name, response.usage.input_tokens, response.usage.output_tokens)
    return base64.b64decode(response.data[0].b64_json)


def card_size(data: bytes) -> bytes:
    """A picture (from a model, or a photo) as a card shows it: upright, light JPEG."""
    image = ImageOps.exif_transpose(PILImage.open(io.BytesIO(data))).convert("RGB")
    image.thumbnail((SIDE, SIDE))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=QUALITY, optimize=True)
    return out.getvalue()


def save(folder: Path, card: Card, data: bytes) -> str:
    """The picture in the lesson's images/ folder (card size); returns its file name."""
    jpeg = card_size(data)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"picture-{card.id}-{hashlib.sha1(jpeg).hexdigest()[:8]}.jpg"
    (folder / name).write_bytes(jpeg)
    return name


# --- Cache: a subject drawn once is reused by the next lessons, for free ----------


def _cache_path(s: Settings, subject: str) -> Path:
    key = hashlib.sha1(f"{model(s)}|{STYLE}|{subject.strip().lower()}".encode()).hexdigest()
    return storage.data_dir() / "cache" / "pictures" / f"{key}.jpg"


async def picture(s: Settings, subject: str, fresh: bool = False) -> bytes:
    """The card-size picture of a subject: from the cache, or drawn (then cached).
    `fresh`: draw it again (the user didn't like it); the new one replaces it in the cache."""
    path = _cache_path(s, subject)
    if not fresh and path.is_file():
        return path.read_bytes()
    jpeg = card_size(await draw(s, subject))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg)
    return jpeg


async def draw_all(folder: Path, cards: list[Card]) -> tuple[int, dict | None]:
    """Draw the missing pictures (cards with a picture_prompt and no picture), a few at
    a time. Returns how many failed and why the first did (an error's detail, for the
    page): a card without its picture is still a card."""
    s = settings.current()
    todo = [c for c in cards if c.picture_prompt.strip() and not c.picture]
    sem = asyncio.Semaphore(CONCURRENCY)
    failures: list[dict] = []

    async def one(card: Card) -> None:
        async with sem:
            try:
                card.picture = save(folder, card, await picture(s, card.picture_prompt))
            except AppError as e:
                failures.append(e.detail())
                log.warning("Picture for %r: %s", card.picture_prompt, e)
            except (OSError, ValueError) as e:  # not an image
                failures.append(PictureError("picture.empty").detail())
                log.warning("Picture for %r: %s", card.picture_prompt, e)

    await asyncio.gather(*(one(c) for c in todo))
    return len(failures), (failures[0] if failures else None)


def prune(folder: Path, cards: list[Card]) -> None:
    """Remove the pictures no card uses any more."""
    keep = {c.picture for c in cards if c.picture}
    if folder.is_dir():
        for path in folder.glob("picture-*.jpg"):
            if path.name not in keep:
                path.unlink(missing_ok=True)
