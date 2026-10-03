"""Card extraction with a vision model.

A single interface, `extract_cards`, and a provider chosen in the settings
(admin page, or .env defaults — see settings.py):

- "gemini" (default): Gemini, through the official google-genai SDK.
- "anthropic": Claude, through the official SDK.
- "openai":     OpenAI itself (GPT).
- "openrouter": OpenRouter, every provider's models with one key.
- "compatible": any other OpenAI-compatible service at its address (Ollama, LM Studio, Mistral…).
- "fake":      canned cards, to work on the UI without a key or any cost.

For Gemini, the fallback models take over when the main model is still
overloaded after a few retries.
"""

import base64
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import BaseModel

from . import diagrams, i18n, prices, settings, storage
from .errors import AppError
from .models import AiCall, Card, Deck, Extraction, Frame, Mask, Revision
from .settings import Settings

log = logging.getLogger("cartable")


SYSTEM_PROMPT = """\
You create Anki flashcards for a pupil, usually from photos of a lesson (a notebook or \
textbook page, sometimes handwritten, sometimes photographed at an angle), sometimes \
from the user's instructions alone.

Rules:
- Follow the user's instructions to know what to extract, in which direction and in \
which languages.
- With photos, only use what is in the lesson; do not invent content.
- Without photos, the instructions are the lesson: when they give the content (a list \
of words, sentences…), use exactly that content; when they ask you to provide it (a \
topic, "the most common irregular verbs"…), make it accurate and suited to a pupil.
- Fix obvious spelling mistakes from the lesson (missing accents or letters).
- Maths: write formulas, equations and symbols plain text shows badly (fractions, \
powers, roots, indices, Greek letters, vectors…) in MathJax, as Anki displays it: \
\\( … \\) within a sentence, \\[ … \\] for a formula on its own (e.g. \
"\\(\\frac{a+b}{2}\\)", "\\(x^2\\)"). Keep plain text for simple things ("2 + 3 = 5").
- Cloze (text with gaps): when the instructions ask for gaps, fill-in-the-blanks or \
cloze cards, the front is a sentence of the lesson with the words to learn hidden in \
Anki's cloze syntax: "{{c1::1789}}", "{{c2::la Bastille}}", e.g. "La Révolution \
française commence en {{c1::1789}} avec la prise de {{c2::la Bastille}}." Each \
number becomes a card (the other gaps shown); the same number hides gaps together. \
A hint may follow: "{{c1::1789::année}}". Hide what matters (dates, names, key \
words), not small words. The back is empty, or a short extra shown with the answer. \
In a gap, never write "}}" inside a formula: add a space ("} }"). Other cards have no \
gaps.
- Plain text in every field: no HTML tags and no Markdown (no <b>, no **bold**), even \
for words in bold on the page; only the MathJax and cloze syntaxes above.
- One idea per card; keep front and back short.
- The "info" field is optional: leave it empty when there is nothing useful to add.
- If the lesson naturally splits into parts (vocabulary, conjugation, sentences…) and \
the instructions don't forbid it, fill "subdeck"; otherwise leave it empty.
- Deck name: start from the suggested template and replace the parts in braces with \
what you read on the page (number, lesson title…). Without a template, suggest a short \
name like "Subject::Lesson". When the existing decks are listed, reuse the names that \
match, written exactly the same: the subject's deck under the name already used \
("Maths" if it exists, not a new "Mathématiques"), and the lesson's own deck if it is \
already there. When none matches, make a new name: never squeeze a lesson into an \
unrelated deck.
- Diagrams: when the instructions ask to learn the labels of a diagram (a diagram to \
complete, its labels hidden, one card per label or arrow, "the diagram without the \
names"…), make one card per label naming a \
part of the diagram (not titles, legends or instructions). Number the labels 1, 2, 3… \
on each photo, in reading order. Front: a short question asking what the numbered part \
is, e.g. "What is (2)?", in the language of the instructions. Back: the label's text. \
Fill "mask": page = the photo's number, n = the label's number, box = the tight \
bounding box of the label's text on that photo, in the format given with the request. \
For every other card, "mask" is null. Also give, in "frames", the box of each such \
diagram as a whole (its drawing and all its labels, nothing else of the page). Boxes \
always refer to the photo as sent, even when it is rotated.
- Pictures: when the instructions ask for a picture on the card (an image of the word, \
a drawing…), fill "picture_prompt", in English, with what to draw: one concrete \
subject a child recognises at once (e.g. "a red apple", "a dog sitting"). Leave it \
empty for words that can't be drawn clearly (abstract words). The front is then the \
text shown with the picture, as the instructions say (a question like "How do you say \
it in English?", or empty if they want the picture alone). Never put the answer in \
the picture's description. Leave "picture" and "id" empty.
- Figures: when a card is about a figure, fill "figure" with a precise description of \
it, in the language of the instructions: the shapes, their proportions, which angles \
are right, and every label with its exact text. A card is about a figure when it is \
about geometry: a formula of a figure (the area of a triangle: the triangle with its \
base b and height h drawn), a theorem (Pythagoras: the right triangle with its sides \
a, b, c), a notion (a tangent, a perpendicular bisector), a figure with measures, a \
simple labelled diagram. The figure's letters are those of the card. Unless the \
instructions ask for no figures. It \
is drawn as a clean, exact figure (not by an image model). Use "picture_prompt" for \
objects, animals and scenes, "figure" for these figures; leave the other empty.
- Where a picture or figure goes: on the front when it is needed to answer ("What is \
the side opposite the right angle called?"): then never put the answer on it (a side \
to name stays unlabelled, or gets a letter only). On the back, "picture_on_back": true, \
when it shows or belongs to the answer (a definition: "What is a tangent to a circle?", \
a property): then it may show and label everything.
- Text lines: for each photo, its longest line of printed text (a title, a sentence): \
the box of its first word and the box of its last word, in reading order, in the same \
format as the diagram boxes. On a photo taken sideways or upside down, the first word \
is still the one you start reading with.
"""


# The AI calls of the request being handled (kind, list), set by `recording`.
_recording: ContextVar[tuple[str, list[AiCall]] | None] = ContextVar("cartable_ai_calls", default=None)


@contextmanager
def recording(kind: str) -> Iterator[list[AiCall]]:
    """Collect the AI calls made inside, with their model, tokens and cost, to keep
    them with the lesson ("extract" or "revise"). Calls that failed after the model
    answered are kept too: they are paid for."""
    calls: list[AiCall] = []
    token = _recording.set((kind, calls))
    try:
        yield calls
    finally:
        _recording.reset(token)


async def record(
    s: Settings,
    provider: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cost: float | None = None,
) -> None:
    """Note an answered call; without a cost from the service, estimate it."""
    current = _recording.get()
    if current is None:  # e.g. the settings page's connection test
        return
    kind, calls = current
    exact = cost is not None
    if cost is None and input_tokens is not None:
        cost = await prices.estimate(s.llm, model, input_tokens, output_tokens or 0)
    calls.append(
        AiCall(
            at=storage.now(),
            kind=kind,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            exact=exact,
        )
    )


@dataclass
class Image:
    data: bytes
    media_type: str  # image/jpeg, image/png, image/webp or image/gif


class ExtractionError(AppError):
    """The AI provider failed; `code` is translated by the page (errors.* keys)."""

    status = 502


def standing_instructions(s: Settings, profile: str | None) -> str:
    """What the parent set in the settings, for everyone and for this Anki profile:
    added before the request, never replacing the fixed rules."""
    parts = []
    if s.instructions.strip():
        parts.append(f"Standing instructions, for every lesson:\n{s.instructions.strip()}")
    own = s.profile_instructions.get(profile or "", "").strip()
    if own:
        parts.append(f"Standing instructions for this pupil ({profile}):\n{own}")
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n(The request below wins if it says otherwise.)\n\n"


def _user_text(
    prompt: str,
    deck: str,
    photos: int,
    sizes: list[tuple[int, int] | None] = (),
    fmt: str = "",
    decks: list[str] = (),
) -> str:
    text = f"Instructions: {prompt.strip()}"
    if deck.strip():
        text += f"\nDeck name template: {deck.strip()}"
    if decks:
        text += "\nExisting decks: " + "; ".join(decks)
    if not photos:
        text += "\nThere is no photo: create the cards from these instructions alone."
    elif fmt:
        text += f"\nDiagram label boxes, if any: {diagrams.FORMATS[fmt]}."
        if fmt == "pixels":
            known = [f"photo {i}: {size[0]}x{size[1]}" for i, size in enumerate(sizes, 1) if size]
            text += "\nPhoto sizes: " + ", ".join(known) + "."
    return text


def _prepare(images: list[Image]) -> tuple[list[Image], list[tuple[int, int] | None]]:
    """Photos as the model will see them, and their sizes (for boxes in pixels)."""
    prepared, sizes = [], []
    for img in images:
        data, media_type, size = diagrams.prepare(img.data, img.media_type)
        prepared.append(Image(data, media_type))
        sizes.append(size)
    return prepared, sizes


@dataclass
class Extracted:
    deck: Deck
    turns: list[int]  # clockwise turn that puts each photo upright
    frames: list[Frame]  # diagram frames, as fractions of the photos (not turned yet)
    back_language: str = ""  # "es-ES": for a prompt whose voice is "auto"
    choice: str = ""  # what the AI chose to make (prompt "Automatic")


async def extract_cards(
    images: list[Image], prompt: str, deck: str = "", profile: str | None = None, decks: list[str] = ()
) -> Extracted:
    """`profile`: the open Anki profile, for its standing instructions; `decks`: the
    decks that already exist, to reuse their names."""
    s = settings.current()
    if s.llm == "fake":
        await record(s, "fake", "fake", 0, 0, cost=0.0)
        choice = "Vocabulaire d'espagnol : français → espagnol" if _lets_choose(prompt) else ""
        return Extracted(_fake(images, prompt, deck), [0] * len(images), [], "es-ES", choice)
    fmt = diagrams.box_format(s.model_for_provider())
    images, sizes = _prepare(images)
    text = standing_instructions(s, profile) + _user_text(prompt, deck, len(images), sizes, fmt, decks)
    result = await _generate(s, images, text, Extraction)
    diagrams.normalize(result.cards, sizes, fmt)
    return Extracted(
        Deck(deck=result.deck, cards=result.cards),
        diagrams.turns(result.text_lines, sizes, fmt),
        diagrams.frames(result.frames, sizes, fmt),
        result.back_language.strip(),
        result.choice.strip(),
    )


async def draw_figure(s: Settings, description: str) -> str:
    """The SVG of a figure, drawn by the cards' AI with the drawing rules (figures.py)."""
    from . import figures

    if s.llm == "fake":
        await record(s, "fake", "fake", 0, 0, cost=0.0)
        return _fake_figure(description)
    drawing = await _generate(
        s, [], f"Figure to draw: {description.strip()}", figures.Drawing, figures.RULES, light=True
    )
    return drawing.svg


def _revision_text(
    prompt: str,
    deck: Deck,
    instruction: str,
    lang: str,
    photos: int,
    sizes: list[tuple[int, int] | None] = (),
    fmt: str = "",
    labels: dict[int, list[int]] | None = None,
) -> str:
    """`labels`: numbers of the diagram labels already hidden, per photo."""
    current = json.dumps(deck.model_dump(), ensure_ascii=False, indent=1)
    source = "these photos with these instructions" if photos else "these instructions (no photo)"
    add = "To add cards, use the photos." if photos else "To add cards, follow the instructions."
    if photos and fmt:
        add += (
            ' Cards about labels already hidden on a diagram have "mask": null here: keep it null.'
            ' To add a card about another label of a diagram (a title, a part…), fill its "mask": page,'
            " the next free number n on that photo, and the box of the label's text,"
            f" {diagrams.FORMATS[fmt]}."
        )
        if labels:
            add += " Numbers already used: " + "; ".join(f"photo {p}: {sorted(ns)}" for p, ns in labels.items()) + "."
        if fmt == "pixels":
            known = [f"photo {i}: {size[0]}x{size[1]}" for i, size in enumerate(sizes, 1) if size]
            add += " Photo sizes: " + ", ".join(known) + "."
    return f"""\
The cards below were made from {source}:
{prompt.strip()}

Current cards (JSON):
{current}

Requested correction: {instruction.strip()}

Apply this request and return the complete deck (every card, not only the ones that \
change). Only change what the request is about; keep the other cards exactly as they \
are, in the same order. {add} In "summary", describe in one short sentence, in \
{i18n.language_name(lang)}, what you changed."""


async def revise_cards(
    images: list[Image],
    prompt: str,
    deck: Deck,
    instruction: str,
    lang: str = i18n.DEFAULT,
    profile: str | None = None,
) -> Revision:
    """Apply a natural-language correction ("remove…", "you forgot…") to the cards.

    `lang`: language of the page, for the one-line summary."""
    s = settings.current()
    if s.llm == "fake":
        await record(s, "fake", "fake", 0, 0, cost=0.0)
        return _fake_revision(deck, instruction, lang)
    # The existing masks stay out of the conversation (their boxes are in our own
    # format): the revised cards get back the mask of the card they were. New cards
    # about a diagram label come with a mask in the model's format.
    fmt = diagrams.box_format(s.model_for_provider())
    images, sizes = _prepare(images)
    labels: dict[int, list[int]] = {}
    for card in deck.cards:
        if card.mask:
            labels.setdefault(card.mask.page, []).append(card.mask.n)
    plain = Deck(
        deck=deck.deck, cards=[c.model_copy(update={"mask": None, "picture": "", "id": ""}) for c in deck.cards]
    )
    text = standing_instructions(s, profile) + _revision_text(
        prompt, plain, instruction, lang, len(images), sizes, fmt, labels
    )
    revision = await _generate(s, images, text, Revision)
    _keep_masks(revision.cards, deck.cards, sizes, fmt)
    return revision


def _keep_ids(revised: list[Card], before: list[Card]) -> None:
    """A revised card keeps the id and the picture of the card it was (same front and
    back, else same back, else same front): Anki updates its note, the picture stays —
    unless what to draw changed (the picture's subject, the figure's description)."""
    left = list(before)
    for card in revised:
        match = next((c for c in left if (c.front, c.back) == (card.front, card.back)), None)
        match = match or next((c for c in left if c.back == card.back), None)
        match = match or next((c for c in left if c.front == card.front and c.front), None)
        if match:
            card.id = match.id
            unchanged = (card.picture_prompt, card.figure) == (match.picture_prompt, match.figure)
            if unchanged or not (card.picture_prompt or card.figure):
                card.picture, card.picture_prompt, card.figure = match.picture, match.picture_prompt, match.figure
                card.picture_on_back = card.picture_on_back if unchanged else match.picture_on_back
            left.remove(match)
        else:
            card.id, card.picture = "", ""  # a new card: its id comes when saved


def _keep_masks(
    revised: list[Card], before: list[Card], sizes: list[tuple[int, int] | None] = (), fmt: str = "pixels"
) -> None:
    """Give each revised card the mask of the same card before (same front and back,
    else same front; a changed answer keeps its place on the diagram). Other cards keep
    the mask the model gave (a label added by the correction), placed like at
    extraction, with a number not used yet on its photo."""
    masked = [c for c in before if c.mask]
    added = []
    for card in revised:
        match = next((c for c in masked if (c.front, c.back) == (card.front, card.back)), None)
        match = match or next((c for c in masked if c.front == card.front), None)
        if match:
            card.mask = match.mask
            masked.remove(match)
        elif card.mask:
            added.append(card)
    _keep_ids(revised, before)
    diagrams.normalize(added, sizes, fmt)
    used = {(c.mask.page, c.mask.n) for c in revised if c.mask and c not in added}
    for card in (c for c in added if c.mask):
        if (card.mask.page, card.mask.n) in used:
            card.mask.n = max((n for page, n in used if page == card.mask.page), default=0) + 1
        used.add((card.mask.page, card.mask.n))


async def _generate[T: BaseModel](
    s: Settings, images: list[Image], text: str, schema: type[T], system: str = SYSTEM_PROMPT, light: bool = False
) -> T:
    """Send photos + text to the configured provider and parse the answer as `schema`.
    `system`: the fixed rules (the cards' by default). `light`: a task that needs little
    thinking (drawing a figure described precisely): the model thinks as little as it
    can, where the service lets us say so — cheaper and faster."""
    if s.llm == "gemini":
        return await _gemini(s, images, text, schema, system, light)
    if s.llm == "anthropic":
        return await _anthropic(s, images, text, schema, system)  # Claude only thinks when asked to
    if s.llm in settings.OPENAI_LIKE:
        return await _openai(s, images, text, schema, system, light)
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


async def _gemini[T: BaseModel](
    s: Settings, images: list[Image], text: str, schema: type[T], system: str = SYSTEM_PROMPT, light: bool = False
) -> T:
    from google.genai import errors, types

    client = _gemini_client(s)
    contents = [types.Part.from_bytes(data=img.data, mime_type=img.media_type) for img in images]
    contents.append(text)
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema.model_json_schema(),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    if light and s.model_for_provider().startswith("gemini-3"):  # older models set thinking otherwise
        config.thinking_config = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
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

    usage = response.usage_metadata
    if usage:
        output = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)  # thinking is billed too
        await record(s, "gemini", model, usage.prompt_token_count, output)
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


async def _anthropic[T: BaseModel](
    s: Settings, images: list[Image], text: str, schema: type[T], system: str = SYSTEM_PROMPT
) -> T:
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
            system=system,
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

    if response.usage:
        await record(s, "anthropic", response.model, response.usage.input_tokens, response.usage.output_tokens)
    if response.stop_reason == "refusal":
        raise ExtractionError("llm.refused")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("llm.truncated")
    if response.parsed_output is None:
        raise ExtractionError("llm.invalid_answer")
    return response.parsed_output


def _openai_service(s: Settings) -> str:
    """Short name of the OpenAI-like service for messages and costs: its host."""
    from urllib.parse import urlparse

    return urlparse(s.base_url()).netloc or s.base_url()


def _openai_base(s: Settings):
    import openai

    if not s.base_url():
        raise ExtractionError("llm.missing_url")
    # Local servers (Ollama, LM Studio) ignore the key but the SDK requires one.
    return openai.AsyncOpenAI(base_url=s.base_url(), api_key=s.api_key() or "none")


def _openai_client(s: Settings):
    if not s.model_for_provider():
        raise ExtractionError("llm.missing_model")
    if s.llm in ("openai", "openrouter") and not s.api_key():  # a compatible local server needs none
        raise ExtractionError("llm.missing_key", provider={"openai": "OpenAI", "openrouter": "OpenRouter"}[s.llm])
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

    root = s.base_url().rstrip("/").removesuffix("/v1")
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


# OpenRouter's aliases ("~google/gemini-flash-latest"): always the latest model of a
# family, the main ones of each provider. Offered first; this one recommended (the
# most precise on diagrams in our trials, fast and cheap).
ALIAS = "~"
RECOMMENDED = "~google/gemini-flash-latest"


async def list_models(s: Settings) -> dict:
    """Models of the OpenAI-compatible service, keeping those accepting images
    when the service tells (OpenRouter, Mistral, LM Studio, Ollama) — and
    structured output when it tells that too (OpenRouter).

    Returns {"models": [...], "vision_only": bool, "aliases": [...], "names": {id: name},
    "recommended": id or None}; vision_only is False when the service doesn't say
    (OpenAI): the admin test then tells for sure. `aliases`: the short list (OpenRouter)."""
    import openai

    client = _openai_base(s)
    try:
        models = [m async for m in client.models.list()]
    except openai.OpenAIError as e:
        raise _openai_error(e, s) from e
    # OpenRouter's ":batch" variants: half price, answered up to hours later (the
    # providers' batch APIs). A lesson is waited for: never offered.
    models = [m for m in models if not m.id.endswith(":batch")]

    known = [_usable(m) for m in models]
    if any(k is not None for k in known):
        usable = [m for m, k in zip(models, known, strict=True) if k]
        aliases = sorted((m.id for m in usable if m.id.startswith(ALIAS)), key=lambda i: (i != RECOMMENDED, i))
        return {
            "models": sorted(m.id for m in usable),
            "vision_only": True,
            "aliases": aliases,
            "names": {m.id: name for m in usable if (name := (m.model_extra or {}).get("name"))},
            "recommended": RECOMMENDED if RECOMMENDED in aliases else None,
        }
    local = await _local_vision_models(s) if s.llm == "compatible" else None
    if local is not None:
        return {"models": sorted(local), "vision_only": True, "aliases": [], "names": {}, "recommended": None}
    return {
        "models": sorted(m.id for m in models),
        "vision_only": False,
        "aliases": [],
        "names": {},
        "recommended": None,
    }


async def _openai[T: BaseModel](
    s: Settings, images: list[Image], text: str, schema: type[T], system: str = SYSTEM_PROMPT, light: bool = False
) -> T:
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
        service = _openai_service(s)
        response = await client.chat.completions.create(
            model=s.model_for_provider(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema.__name__.lower(), "schema": schema.model_json_schema()},
            },
            # OpenRouter tells the exact cost of the call when asked
            # OpenRouter: the exact cost of the call; and, for a light task, as little
            # thinking as the model allows (a model that doesn't think ignores it)
            extra_body=(
                {"usage": {"include": True}, **({"reasoning": {"effort": "minimal"}} if light else {})}
                if s.llm == "openrouter"
                else None
            ),
        )
        usage = response.usage
        if usage:
            cost = (usage.model_extra or {}).get("cost")
            model = response.model or s.model_for_provider()
            await record(s, service, model, usage.prompt_tokens, usage.completion_tokens, cost)
        return schema.model_validate(json.loads(response.choices[0].message.content or ""))
    except openai.OpenAIError as e:
        raise _openai_error(e, s) from e
    except ValueError as e:  # invalid JSON, or JSON not matching the schema
        raise ExtractionError("llm.invalid_answer") from e


def _lets_choose(prompt: str) -> bool:
    return "choisis" in prompt or "choose" in prompt


def _fake_vocabulary(images: list[Image], prompt: str) -> Deck:
    """Demo mode, by default: Spanish vocabulary."""
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


def _fake(images: list[Image], prompt: str, deck: str) -> Deck:
    if _lets_choose(prompt):  # "Automatic": the demo always chooses vocabulary
        return _fake_vocabulary(images, prompt)
    if images and any(w in prompt.lower() for w in ("diagram", "schéma", "schema")):
        return _fake_diagram()
    if any(w in prompt.lower() for w in ("picture", "image", "dessin")):
        return _fake_pictures()
    if any(w in prompt.lower() for w in ("cloze", "trous", "gaps")):
        return _fake_cloze()
    if any(w in prompt.lower() for w in ("figure", "géométrie", "geometry", "triangle")):
        return _fake_figures()
    return _fake_vocabulary(images, prompt)


def _fake_pictures() -> Deck:
    """Demo mode, picture prompt: three words to draw, one that can't be drawn."""
    words = [("an apple", "an apple"), ("a dog", "a dog"), ("an umbrella", "an umbrella"), ("", "tomorrow")]
    return Deck(
        deck="Anglais::Mots courants",
        cards=[
            Card(front="Comment dit-on en anglais ?" if subject else "demain", back=back, picture_prompt=subject)
            for subject, back in words
        ],
    )


def _fake_figure(description: str) -> str:
    """Demo mode: a right triangle, its first label taken from the description."""
    label = (description.split("«")[1].split("»")[0].strip() if "«" in description else "hypoténuse")[:30]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><rect width="400" height="300" fill="white"/>'
        '<polygon points="80,240 320,240 80,60" fill="none" stroke="black" stroke-width="2"/>'
        '<rect x="80" y="225" width="15" height="15" fill="none" stroke="black"/>'
        f'<text x="215" y="140" font-family="sans-serif" font-size="16" transform="rotate(37 215 140)">{label}</text>'
        "</svg>"
    )


def _fake_figures() -> Deck:
    """Demo mode, figure prompt: geometry cards with figures to draw."""
    return Deck(
        deck="Maths::Le triangle rectangle",
        cards=[
            Card(
                front="Comment s'appelle le côté opposé à l'angle droit ?",
                back="l'hypoténuse",
                figure="Un triangle rectangle ABC, rectangle en C, l'angle droit marqué, sans autre étiquette.",
            ),
            Card(
                front="Quel théorème relie les côtés de ce triangle ?",
                back="le théorème de Pythagore",
                figure="Un triangle rectangle, l'hypoténuse étiquetée « c », les autres côtés « a » et « b ».",
            ),
            Card(
                front="Qu'est-ce qu'une tangente à un cercle ?",
                back="Une droite qui touche le cercle en un seul point, perpendiculaire au rayon en ce point.",
                figure="Un cercle de centre O, une droite étiquetée « tangente » qui le touche en T, "
                "le rayon [OT] et l'angle droit en T.",
                picture_on_back=True,
            ),
        ],
    )


def _fake_cloze() -> Deck:
    """Demo mode, cloze prompt: sentences with gaps."""
    return Deck(
        deck="Histoire::La Révolution française",
        cards=[
            Card(
                front="La Révolution française commence en {{c1::1789}} avec la prise de {{c2::la Bastille}}.", back=""
            ),
            Card(front="Le roi {{c1::Louis XVI}} est guillotiné en {{c2::1793}}.", back="place de la Révolution"),
            Card(
                front="La {{c1::Déclaration des droits de l'homme et du citoyen}} est adoptée en {{c1::août}} 1789.",
                back="",
            ),
        ],
    )


def _fake_diagram() -> Deck:
    """Demo mode, diagram prompt: three labels hidden on the first photo."""
    labels = [
        ("la bouche", [0.1, 0.1, 0.35, 0.2]),
        ("le cœur", [0.55, 0.4, 0.85, 0.5]),
        ("l'estomac", [0.2, 0.7, 0.5, 0.8]),
    ]
    return Deck(
        deck="Sciences::Le corps humain",
        cards=[
            Card(front=f"Qu'est-ce que ({n}) ?", back=text, mask=Mask(page=1, n=n, box=box))
            for n, (text, box) in enumerate(labels, 1)
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
