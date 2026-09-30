"""Card extraction with a vision model.

A single interface, `extract_cards`, and a provider chosen in the settings
(admin page, or .env defaults — see settings.py):

- "gemini" (default): Gemini, through the official google-genai SDK.
- "anthropic": Claude, through the official SDK.
- "openai":    any OpenAI-compatible API (Ollama…).
- "fake":      canned cards, to work on the UI without a key or any cost.

For Gemini, the fallback models take over when the main model is still
overloaded after a few retries.
"""

import base64
import json
import logging
from dataclasses import dataclass

from pydantic import BaseModel

from . import i18n, settings
from .errors import AppError
from .models import Card, Deck, Revision
from .settings import Settings

log = logging.getLogger("cartable")


SYSTEM_PROMPT = """\
You create Anki flashcards from photos of a lesson (usually a pupil's notebook or \
textbook page, sometimes handwritten, sometimes photographed at an angle).

Rules:
- Follow the user's instructions to know what to extract, in which direction and in \
which languages.
- Only use what is in the lesson; do not invent content.
- Fix obvious spelling mistakes from the lesson (missing accents or letters).
- One idea per card; keep front and back short.
- The "info" field is optional: leave it empty when there is nothing useful to add.
- If the lesson naturally splits into parts (vocabulary, conjugation, sentences…) and \
the instructions don't forbid it, fill "subdeck"; otherwise leave it empty.
- Deck name: start from the suggested template and replace the parts in braces with \
what you read on the page (number, lesson title…). Without a template, suggest a short \
name like "Subject::Lesson".
"""


@dataclass
class Image:
    data: bytes
    media_type: str  # image/jpeg, image/png, image/webp or image/gif


class ExtractionError(AppError):
    """The AI provider failed; `code` is translated by the page (errors.* keys)."""

    status = 502


def _user_text(prompt: str, deck: str) -> str:
    text = f"Instructions: {prompt.strip()}"
    if deck.strip():
        text += f"\nDeck name template: {deck.strip()}"
    return text


async def extract_cards(images: list[Image], prompt: str, deck: str = "") -> Deck:
    s = settings.current()
    if s.llm == "fake":
        return _fake(images, prompt, deck)
    return await _generate(s, images, _user_text(prompt, deck), Deck)


def _revision_text(prompt: str, deck: Deck, instruction: str, lang: str) -> str:
    current = json.dumps(deck.model_dump(), ensure_ascii=False, indent=1)
    return f"""\
The cards below were made from these photos with these instructions:
{prompt.strip()}

Current cards (JSON):
{current}

Requested correction: {instruction.strip()}

Apply this request and return the complete deck (every card, not only the ones that \
change). Only change what the request is about; keep the other cards exactly as they \
are, in the same order. To add cards, use the photos. In "summary", describe in one \
short sentence, in {i18n.language_name(lang)}, what you changed."""


async def revise_cards(
    images: list[Image], prompt: str, deck: Deck, instruction: str, lang: str = i18n.DEFAULT
) -> Revision:
    """Apply a natural-language correction ("remove…", "you forgot…") to the cards.

    `lang`: language of the page, for the one-line summary."""
    s = settings.current()
    if s.llm == "fake":
        return _fake_revision(deck, instruction, lang)
    return await _generate(s, images, _revision_text(prompt, deck, instruction, lang), Revision)


async def _generate[T: BaseModel](s: Settings, images: list[Image], text: str, schema: type[T]) -> T:
    """Send photos + text to the configured provider and parse the answer as `schema`."""
    if s.llm == "gemini":
        return await _gemini(s, images, text, schema)
    if s.llm == "anthropic":
        return await _anthropic(s, images, text, schema)
    if s.llm == "openai":
        return await _openai(s, images, text, schema)
    raise ExtractionError("llm.unknown_provider", provider=s.llm)


def _gemini_client(s: Settings):
    from google import genai
    from google.genai import types

    if not s.gemini_api_key:
        raise ExtractionError("llm.missing_key", provider="Gemini")
    # 5xx errors (mostly 503 "model overloaded") are frequent and short-lived:
    # the SDK retries with exponential backoff before giving up.
    return genai.Client(
        api_key=s.gemini_api_key,
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=3, initial_delay=2, http_status_codes=[500, 502, 503, 504])
        ),
    )


async def _gemini[T: BaseModel](s: Settings, images: list[Image], text: str, schema: type[T]) -> T:
    from google.genai import errors, types

    client = _gemini_client(s)
    contents = [types.Part.from_bytes(data=img.data, mime_type=img.media_type) for img in images]
    contents.append(text)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_json_schema=schema.model_json_schema(),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    models = [s.model_for_provider()]
    models += [m.strip() for m in s.fallback_models.split(",") if m.strip()]

    for model in models:
        try:
            response = await client.aio.models.generate_content(model=model, contents=contents, config=config)
            break
        except errors.ClientError as e:
            log.warning("Gemini %s : %s %s", model, e.code, e.message)
            if e.code in (401, 403) or "API key" in str(e):
                raise ExtractionError("llm.invalid_key", provider="Gemini") from e
            if e.code == 429:
                raise ExtractionError("llm.quota", provider="Gemini") from e
            raise ExtractionError("llm.api_error", provider="Gemini", status=e.code, detail=e.message) from e
        except errors.APIError as e:
            log.warning("Gemini %s : %s %s", model, e.code, e.message)
            if model == models[-1]:
                raise ExtractionError("llm.overloaded", provider="Gemini", status=e.code) from e
            log.warning("Falling back to the next model")

    candidate = response.candidates[0] if response.candidates else None
    if candidate is None or not response.text:
        raise ExtractionError("llm.empty_answer", provider="Gemini")
    if candidate.finish_reason == types.FinishReason.MAX_TOKENS:
        raise ExtractionError("llm.truncated")
    try:
        return schema.model_validate_json(response.text)
    except ValueError as e:
        raise ExtractionError("llm.invalid_answer") from e


def _anthropic_client(s: Settings):
    import anthropic

    if not s.anthropic_api_key:
        raise ExtractionError("llm.missing_key", provider="Anthropic")
    return anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)


async def _anthropic[T: BaseModel](s: Settings, images: list[Image], text: str, schema: type[T]) -> T:
    import anthropic

    client = _anthropic_client(s)
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.media_type,
                "data": base64.standard_b64encode(img.data).decode(),
            },
        }
        for img in images
    ]
    content.append({"type": "text", "text": text})

    try:
        response = await client.messages.parse(
            model=s.model_for_provider(),
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            # If the model refuses, the API reruns the request on a fallback model.
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
    except anthropic.AuthenticationError as e:
        raise ExtractionError("llm.invalid_key", provider="Anthropic") from e
    except anthropic.RateLimitError as e:
        raise ExtractionError("llm.quota", provider="Anthropic") from e
    except anthropic.APIStatusError as e:
        raise ExtractionError("llm.api_error", provider="Anthropic", status=e.status_code, detail=e.message) from e
    except anthropic.APIConnectionError as e:
        raise ExtractionError("llm.unreachable", provider="Anthropic") from e

    if response.stop_reason == "refusal":
        raise ExtractionError("llm.refused")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("llm.truncated")
    if response.parsed_output is None:
        raise ExtractionError("llm.invalid_answer")
    return response.parsed_output


def _openai_service(s: Settings) -> str:
    """Short name of the OpenAI-compatible service for messages: its host."""
    from urllib.parse import urlparse

    return urlparse(s.openai_base_url).netloc or s.openai_base_url


def _openai_base(s: Settings):
    import openai

    if not s.openai_base_url.strip():
        raise ExtractionError("llm.missing_url")
    # Local servers (Ollama, LM Studio) ignore the key but the SDK requires one.
    return openai.AsyncOpenAI(base_url=s.openai_base_url, api_key=s.openai_key() or "none")


def _openai_client(s: Settings):
    if not s.model_for_provider():
        raise ExtractionError("llm.missing_model")
    return _openai_base(s)


def _error_message(body) -> str | None:
    """The human message in an error body, unwrapping {"error": {...}} and JSON
    nested in strings (Ollama: {"message": "{\"error\": {\"message\": …}}"})."""
    for _ in range(5):
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                return body
        if isinstance(body, dict):
            body = body.get("error") or body.get("message")
        else:
            return None
    return None


def _openai_error(e: Exception, s: Settings) -> ExtractionError:
    """Readable error for an OpenAI-compatible service failure."""
    import openai

    service = _openai_service(s)
    if isinstance(e, openai.APIConnectionError):
        return ExtractionError("llm.unreachable", provider=service)
    if isinstance(e, openai.AuthenticationError):
        return ExtractionError("llm.invalid_key", provider=service)
    if isinstance(e, openai.RateLimitError):
        return ExtractionError("llm.quota", provider=service)
    if isinstance(e, openai.APIStatusError):
        return ExtractionError(
            "llm.api_error", provider=service, status=e.status_code, detail=_error_message(e.body) or e.message
        )
    return ExtractionError("llm.api_error", provider=service, status="", detail=str(e))


def _usable(m) -> bool | None:
    """Whether an OpenAI-compatible /models entry fits Cartable (image input, and
    structured output when the service lists parameters); None if it says nothing."""
    extra = m.model_extra or {}
    parameters = extra.get("supported_parameters")  # OpenRouter
    if parameters is not None and "structured_outputs" not in parameters:
        return False  # can't be forced to answer in our JSON schema
    modalities = (extra.get("architecture") or {}).get("input_modalities")  # OpenRouter
    if modalities is not None:
        return "image" in modalities
    vision = (extra.get("capabilities") or {}).get("vision")  # Mistral
    return None if vision is None else bool(vision)


async def _local_vision_models(s: Settings) -> list[str] | None:
    """LM Studio and Ollama only describe their models in their own API, next to
    the OpenAI-compatible one: the vision models there, or None if not such a server."""
    from urllib.parse import urlparse

    import httpx

    root = s.openai_base_url.strip().rstrip("/").removesuffix("/v1")
    if urlparse(root).scheme != "http":  # local servers; cloud services answer in /models
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        try:  # LM Studio: GET /api/v1/models → models[].capabilities.vision
            data = (await client.get(f"{root}/api/v1/models")).json()
            if isinstance(data.get("models"), list):
                return [m["key"] for m in data["models"] if (m.get("capabilities") or {}).get("vision")]
        except (httpx.HTTPError, ValueError, AttributeError, KeyError):
            pass
        try:  # Ollama: GET /api/tags, then POST /api/show per model → capabilities
            tags = (await client.get(f"{root}/api/tags")).json()["models"]
            vision = []
            for tag in tags:
                show = (await client.post(f"{root}/api/show", json={"model": tag["name"]})).json()
                if "vision" in show.get("capabilities", []):
                    vision.append(tag["name"])
            return vision
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None


async def list_models(s: Settings) -> dict:
    """Models of the OpenAI-compatible service, keeping those accepting images
    when the service tells (OpenRouter, Mistral, LM Studio, Ollama) — and
    structured output when it tells that too (OpenRouter).

    Returns {"models": [...], "vision_only": bool}; vision_only is False when
    the service doesn't say (OpenAI): the admin test then tells for sure."""
    import openai

    client = _openai_base(s)
    try:
        models = [m async for m in client.models.list()]
    except openai.OpenAIError as e:
        raise _openai_error(e, s) from e

    known = [_usable(m) for m in models]
    if any(k is not None for k in known):
        return {"models": sorted(m.id for m, k in zip(models, known, strict=True) if k), "vision_only": True}
    local = await _local_vision_models(s)
    if local is not None:
        return {"models": sorted(local), "vision_only": True}
    return {"models": sorted(m.id for m in models), "vision_only": False}


async def _openai[T: BaseModel](s: Settings, images: list[Image], text: str, schema: type[T]) -> T:
    """OpenAI-compatible providers: Ollama (qwen2.5vl, gemma3…), etc."""
    import openai

    client = _openai_client(s)
    content = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{img.media_type};base64,{base64.standard_b64encode(img.data).decode()}"},
        }
        for img in images
    ]
    content.append({"type": "text", "text": text})

    try:
        response = await client.chat.completions.create(
            model=s.model_for_provider(),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema.__name__.lower(), "schema": schema.model_json_schema()},
            },
        )
        return schema.model_validate(json.loads(response.choices[0].message.content or ""))
    except openai.OpenAIError as e:
        raise _openai_error(e, s) from e
    except ValueError as e:  # invalid JSON, or JSON not matching the schema
        raise ExtractionError("llm.invalid_answer") from e


def _fake(images: list[Image], prompt: str, deck: str) -> Deck:
    return Deck(
        deck="Espagnol::Leçon 5 - La famille",
        cards=[
            Card(front="la mère", back="la madre", info="nom féminin", subdeck="Vocabulaire"),
            Card(front="le père", back="el padre", info="nom masculin", subdeck="Vocabulaire"),
            Card(front="les parents", back="los padres", info="masculin pluriel", subdeck="Vocabulaire"),
            Card(front="la sœur", back="la hermana", info="nom féminin", subdeck="Vocabulaire"),
            Card(front="Comment t'appelles-tu ?", back="¿Cómo te llamas?", subdeck="Phrases"),
            Card(
                front=f"({len(images)} photo(s) received)",
                back="demo mode (fake provider)",
                info=prompt[:80],
            ),
        ],
    )


def _fake_revision(deck: Deck, instruction: str, lang: str) -> Revision:
    """Demo mode: a request mentioning removal drops the last card, anything else adds one."""
    if any(w in instruction.lower() for w in ("supprime", "remove", "delete")) and deck.cards:
        return Revision(deck=deck.deck, cards=deck.cards[:-1], summary=i18n.get(lang, "demo.removed"))
    added = Card(front=i18n.get(lang, "demo.addedFront"), back=instruction[:60], subdeck="Demo")
    return Revision(deck=deck.deck, cards=[*deck.cards, added], summary=i18n.get(lang, "demo.added"))


def _test_image() -> bytes:
    """A 32×32 plain red PNG, built without an image library."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    size = 32
    rows = b"".join(b"\x00" + b"\xdc\x14\x14" * size for _ in range(size))  # filter byte + RGB pixels
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class _CheckAnswer(BaseModel):
    color: str


async def check(s: Settings) -> dict:
    """Check what Cartable needs from the model: reading an image and answering
    in the requested JSON format. Sends a tiny red image (a fraction of a cent).

    Returns {"vision": bool, "json": bool, "answer": str}; raises ExtractionError
    when the service itself fails (key, address, model name…)."""
    if s.llm == "fake":
        return {"vision": True, "json": True, "answer": "red"}
    only_main_model = s.model_copy(update={"fallback_models": ""})  # test the chosen model, not a fallback
    prompt = "What is the main colour of this image? Answer with one lowercase English word."
    try:
        answer = await _generate(only_main_model, [Image(_test_image(), "image/png")], prompt, _CheckAnswer)
    except ExtractionError as e:
        if e.code in ("llm.invalid_answer", "llm.truncated"):
            return {"vision": False, "json": False, "answer": ""}  # answered, but not in the JSON format
        detail = str(e.params.get("detail", "")).lower()
        if e.params.get("status") == 400 and any(w in detail for w in ("image", "multimodal", "vision")):
            # e.g. Ollama: "model does not support multimodal requests"
            return {"vision": False, "json": None, "answer": "", "refused": e.params["detail"]}
        raise
    return {"vision": "red" in answer.color.lower(), "json": True, "answer": answer.color.strip()[:40]}
