import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # before the app imports, some of which read variables at import time

import io

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import anki, ankiconnect, decks, diagrams, i18n, lessons, llm, pictures, prompts, settings, tts, usage
from .anki import build_apkg, notes
from .errors import AppError
from .llm import Image, check, extract_cards, list_models, revise_cards
from .models import (
    AdminPassword,
    AiCall,
    Deck,
    ExportRequest,
    Lesson,
    LessonAccess,
    LessonIn,
    LessonSummary,
    PictureRequest,
    Prompt,
    PromptIn,
    RevisionRequest,
    SettingsUpdate,
)
from .version import VERSION

log = logging.getLogger("cartable")

STATIC_DIR = Path(__file__).parent.parent / "static"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGES = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.warning("Cartable %s, LLM provider: %s", VERSION, settings.current().llm)
    yield


app = FastAPI(title="Cartable", version=VERSION, lifespan=lifespan)


@app.exception_handler(AppError)
async def app_error(request, e: AppError) -> JSONResponse:
    # The page translates {"code", "params"} (static/i18n/<lang>.json, "errors").
    return JSONResponse(status_code=e.status, content={"detail": e.detail()})


def page_lang(x_cartable_lang: str | None = Header(None)) -> str:
    """Language of the page making the request (sent by the page on every call)."""
    return i18n.resolve(x_cartable_lang) or i18n.DEFAULT


@app.get("/api/lang")
def lang() -> dict:
    """In the Anki add-on: Anki's language (English if we don't have it).
    Otherwise None: the page uses the browser's language. A language picked on
    the page itself (kept in the browser) wins over both."""
    return {
        "lang": i18n.anki_language(),
        "available": i18n.available(),
        "names": {code: i18n.get(code, "meta.name", code) for code in i18n.available()},
    }


# --- Prompts ---------------------------------------------------------------


@app.get("/api/prompts")
def list_prompts(lang: str = Depends(page_lang)) -> list[Prompt]:
    return prompts.list_all(lang)  # Cartable's prompts in this language, then the user's


@app.post("/api/prompts", status_code=201)
def add_prompt(c: PromptIn) -> Prompt:
    return prompts.add(c)


@app.put("/api/prompts/{id}")
def update_prompt(id: str, c: PromptIn) -> Prompt:
    if updated := prompts.update(id, c):
        return updated
    raise AppError("prompt.not_found", 404)


@app.delete("/api/prompts/{id}", status_code=204)
def delete_prompt(id: str) -> None:
    if not prompts.delete(id):
        raise AppError("prompt.not_found", 404)


@app.post("/api/prompts/{id}/duplicate", status_code=201)
def duplicate_prompt(id: str, lang: str = Depends(page_lang)) -> Prompt:
    """A copy of any prompt (Cartable's included), to adapt."""
    if copy := prompts.duplicate(id, lang):
        return copy
    raise AppError("prompt.not_found", 404)


# --- Extraction & export ---------------------------------------------------


@dataclass
class Generated:
    content: LessonIn
    photos: list[bytes]  # upright
    found: llm.Extracted
    calls: list[AiCall]
    profile: str


async def _generate(
    images: list[UploadFile], prompt: str, deck: str, voice: str, prompt_id: str | None, typing: bool, dictation: bool
) -> Generated:
    """Read the photos (or, without photos, work from the prompt alone): the lesson's
    new content, not saved yet."""
    if not images and not prompt.strip():
        raise AppError("extract.no_input")
    if len(images) > MAX_IMAGES:
        raise AppError("extract.too_many", max=MAX_IMAGES)
    for img in images:
        if img.content_type not in IMAGE_TYPES:
            raise AppError("extract.bad_format", format=img.content_type)

    data = [Image(await img.read(), img.content_type) for img in images]
    profile = await ankiconnect.active_profile() or ""  # the lesson belongs to this Anki profile
    with llm.recording("extract") as calls:  # model, tokens and cost, kept with the lesson
        try:
            found = await extract_cards(data, prompt, deck, profile, await decks.known(profile))
        except Exception:
            usage.add(calls, lesson_id=None)  # answered but unusable: paid for, no lesson saved
            raise
    if prompt_id is not None:
        prompts.mark_used(prompt_id)
    # Photos taken sideways are saved upright (masks and diagram frames turn with them)
    photos = diagrams.straighten([i.data for i in data], found.deck.cards, found.turns, found.frames)
    if voice.strip().lower() == "auto":  # the voice of the language the backs are in
        voice = await tts.voice_for(found.back_language)
    content = LessonIn(**found.deck.model_dump(), voice=voice, typing=typing, dictation=dictation)
    return Generated(content, photos, found, calls, profile)


@app.post("/api/extract", status_code=201)
async def extract(
    images: list[UploadFile] = File([]),
    prompt: str = Form(...),
    deck: str = Form(""),
    voice: str = Form(""),
    prompt_id: str | None = Form(None),
    typing: bool = Form(False),
    dictation: bool = Form(False),
) -> Lesson:
    """A new lesson (photos + cards), saved so it can be reopened."""
    g = await _generate(images, prompt, deck, voice, prompt_id, typing, dictation)
    created = lessons.create(g.content, prompt, g.photos, g.profile, g.found.frames, g.calls, g.found.choice)
    usage.add(g.calls, created.id, created.deck)
    return created


@app.post("/api/lessons/{id}/regenerate")
async def regenerate(
    id: str,
    images: list[UploadFile] = File([]),
    prompt: str = Form(...),
    deck: str = Form(""),
    voice: str = Form(""),
    prompt_id: str | None = Form(None),
    typing: bool = Form(False),
    dictation: bool = Form(False),
) -> Lesson:
    """Generate the lesson again (other prompt, other photos) in its place, instead of
    a second lesson. Only its owner's profile may."""
    await _editable(id)
    g = await _generate(images, prompt, deck, voice, prompt_id, typing, dictation)
    lesson = lessons.regenerated(id, g.content, prompt, g.photos, g.found.frames, g.calls, g.found.choice)
    if lesson is None:  # deleted meanwhile
        raise AppError("lesson.not_found", 404)
    usage.add(g.calls, id, lesson.deck)
    return lesson


def _filename(deck: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', " - ", deck).strip(" -") or "cartable"
    return f"{name}.apkg"


async def _card_audio(req: ExportRequest, background: BackgroundTasks) -> tuple[dict[str, Path], int]:
    """mp3 of the card backs for an edge-tts voice: {back: mp3} and the number of failures."""
    if not req.voice or tts.is_anki_locale(req.voice):
        return {}, 0
    # Saved lesson: mp3s go to its audio/ folder; otherwise a throwaway folder.
    directory = lessons.audio_dir(req.lesson_id) if req.lesson_id else None
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix="cartable-audio-"))
        background.add_task(shutil.rmtree, directory, ignore_errors=True)
    # Not read aloud: a back with a formula (the voice would read the MathJax code),
    # a text with gaps (its back is only an extra)
    backs = [
        c.back.strip()
        for c in req.cards
        if c.front.strip() and c.back.strip() and not tts.has_math(c.back) and not anki.is_cloze(c.front)
    ]
    audio, failures = await tts.tts_many(backs, req.voice, directory)
    tts.prune(directory, set(audio.values()))
    return audio, failures


def _pictures(req: ExportRequest, lesson: Lesson | None) -> dict[int, Path]:
    """The picture of each picture card (index in req.cards → file), from the saved lesson."""
    folder = lessons.folder(lesson.id) if lesson else None
    if folder is None:
        return {}
    pictures.prune(folder / "images", req.cards)  # pictures of cards gone (removed, regenerated)
    found = {i: folder / "images" / card.picture for i, card in enumerate(req.cards) if card.picture}
    return {i: path for i, path in found.items() if pictures.NAME.match(path.name) and path.is_file()}


def _diagram_images(req: ExportRequest, lesson: Lesson | None) -> dict[int, tuple[Path, list[float] | None]]:
    """The diagram image of each diagram card (index in req.cards → path and crop),
    from the saved lesson's photos. Without a saved lesson, there is no photo to show."""
    folder = lessons.folder(lesson.id) if lesson else None
    if folder is None:
        return {}
    frames = {f.page: f.box for f in (req.frames if req.frames is not None else lesson.frames)}
    images, used = {}, set()
    for i, card in enumerate(req.cards):
        photo = lessons.photo_path(lesson.id, card.mask.page) if card.mask else None
        if photo and photo.is_file():
            page = card.mask.page
            box = diagrams.crop(frames.get(page), [c.mask for c in req.cards if c.mask and c.mask.page == page])
            images[i] = (diagrams.page_image(folder / "images", photo, box), box)
            used.add(images[i][0])
    diagrams.prune(folder / "images", used)
    return images


async def _visible(owner: str, shared: bool) -> bool:
    """A profile sees its own lessons, shared ones and lessons without owner; another
    profile's private lessons don't exist for it. Anki closed (no profile known): all."""
    if shared or not owner:
        return True
    profile = await ankiconnect.active_profile()
    return profile is None or profile == owner


async def _lesson(id: str | None) -> Lesson | None:
    """The lesson, if the open Anki profile may see it."""
    lesson = lessons.get(id) if id else None
    if lesson is None:
        if id:
            raise AppError("lesson.not_found", 404)
        return None
    if not await _visible(lesson.owner, lesson.shared):
        raise AppError("lesson.not_found", 404)
    return lesson


async def _is_owner(lesson: Lesson) -> bool:
    """Only the profile that created a lesson changes it; lessons without owner are everyone's."""
    return not lesson.owner or await ankiconnect.active_profile() == lesson.owner


async def _editable(id: str) -> Lesson:
    """The lesson, if the open Anki profile may change it: for the others, it's read-only
    (they can still read it, send it to their own Anki and export it)."""
    lesson = await _lesson(id)
    if not await _is_owner(lesson):
        raise AppError("lesson.read_only", 403, owner=lesson.owner)
    return lesson


@app.post("/api/export")
async def export(req: ExportRequest, background: BackgroundTasks) -> FileResponse:
    lesson = await _lesson(req.lesson_id)
    audio, failures = await _card_audio(req, background)
    path = build_apkg(req, audio, _diagram_images(req, lesson), _pictures(req, lesson))
    background.add_task(os.remove, path)
    if lesson and await _is_owner(lesson):  # someone else's lesson: exported, not changed
        lessons.update(lesson.id, req, exported=True)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=_filename(req.deck),
        headers={"X-Cartable-Audio-Failures": str(failures)},
    )


# --- Direct send to Anki (AnkiConnect) ----------------------------------------


@app.get("/api/decks")
async def deck_names() -> list[str]:
    """The decks that already exist (Anki's and the lessons'), offered for the deck name."""
    return await decks.known(await ankiconnect.active_profile())


@app.get("/api/anki/status")
async def anki_status() -> dict:
    try:
        return {
            "available": True,
            "version": await ankiconnect.version(),
            "profile": await ankiconnect.active_profile(),
            "sync": await ankiconnect.sync_configured(),  # logged in to AnkiWeb (None: unknown)
        }
    except ankiconnect.AnkiConnectError as e:
        return {"available": False, "error": e.detail()}


@app.post("/api/anki/send")
async def anki_send(req: ExportRequest, background: BackgroundTasks) -> dict:
    lesson = await _lesson(req.lesson_id)  # another profile's private lesson doesn't exist for this one
    save = lesson and await _is_owner(lesson)  # someone else's lesson: sent, not changed
    audio, failures = await _card_audio(req, background)
    result = await ankiconnect.send(notes(req, audio, _diagram_images(req, lesson), _pictures(req, lesson)))
    if save:
        lessons.update(lesson.id, req, exported=True)
    return {**result.__dict__, "audio_failures": failures}


# --- Saved lessons ---------------------------------------------------------


@app.get("/api/config")
def config() -> dict:
    """Settings the main page needs (no secrets, no password)."""
    s = settings.current()
    # Claude places diagram masks less precisely (too tight on handwriting): say so in the review.
    loose_boxes = s.llm == "anthropic" or "claude" in s.model_for_provider().lower()
    return {"version": VERSION, "diagram_warning": loose_boxes}


@app.get("/api/lessons")
async def list_lessons() -> list[LessonSummary]:
    return [s for s in lessons.list_all() if await _visible(s.owner, s.shared)]


@app.get("/api/lessons/{id}")
async def get_lesson(id: str) -> Lesson:
    return await _lesson(id)


@app.put("/api/lessons/{id}")
async def update_lesson(id: str, lesson: LessonIn) -> Lesson:
    current = await _editable(id)
    # Sharing only changes when asked, and only for a lesson that has an owner.
    share = lesson.shared if lesson.shared is not None and current.owner else None
    return lessons.update(id, lesson, share=share)


@app.delete("/api/lessons/{id}", status_code=204)
async def delete_lesson(id: str) -> None:
    await _editable(id)
    lessons.delete(id)


@app.post("/api/lessons/{id}/revise")
async def revise_lesson(id: str, req: RevisionRequest, lang: str = Depends(page_lang)) -> dict:
    """Apply a natural-language correction to the cards, using the lesson photos."""
    lesson = await _editable(id)
    photos = [Image(lessons.photo_path(id, n).read_bytes(), "image/jpeg") for n in range(1, lesson.photo_count + 1)]
    with llm.recording("revise") as calls:
        try:
            revision = await revise_cards(
                photos, lesson.prompt, Deck(deck=req.deck, cards=req.cards), req.instruction, lang, lesson.owner
            )
        finally:
            lessons.add_ai_calls(id, calls)  # an answer that couldn't be used is paid for too
            usage.add(calls, id, req.deck)
    updated = lessons.update(
        id,
        LessonIn(
            deck=revision.deck,
            cards=revision.cards,
            voice=req.voice,
            reverse=req.reverse,
            typing=req.typing,
            dictation=req.dictation,
        ),
    )
    return {"lesson": updated, "summary": revision.summary}


@app.post("/api/lessons/{id}/photos/{n}/rotate")
async def rotate_photo(id: str, n: int) -> Lesson:
    """Turn a photo a quarter turn clockwise (the AI got its direction wrong), with the
    diagram masks on it."""
    lesson = await _editable(id)
    path = lessons.photo_path(id, n)
    if path is None or not path.is_file():
        raise AppError("photo.not_found", 404)
    cards = [card.model_copy(deep=True) for card in lesson.cards]
    frames = [frame.model_copy(deep=True) for frame in lesson.frames]
    path.write_bytes(diagrams.turn(path.read_bytes(), diagrams.boxes_on(n, cards, frames), 90))
    content = LessonIn(**{**lesson.model_dump(include=set(LessonIn.model_fields)), "cards": cards, "frames": frames})
    return lessons.update(id, content)


@app.post("/api/lessons/{id}/pictures")
async def draw_pictures(id: str) -> dict:
    """Draw the pictures the cards ask for and don't have yet (after a generation or a
    correction). A picture that fails leaves its card without one: said, not fatal."""
    lesson = await _editable(id)
    cards = [card.model_copy(deep=True) for card in lesson.cards]
    folder = lessons.folder(id) / "images"
    with llm.recording("picture") as calls:
        try:
            failures, error = await pictures.draw_all(folder, cards)
        finally:
            lessons.add_ai_calls(id, calls)
            usage.add(calls, id, lesson.deck)
    content = LessonIn(**{**lesson.model_dump(include=set(LessonIn.model_fields)), "cards": cards})
    return {"lesson": lessons.update(id, content), "failures": failures, "error": error}


def _card(lesson: Lesson, card_id: str) -> int:
    index = next((i for i, c in enumerate(lesson.cards) if c.id == card_id), None)
    if index is None:
        raise AppError("card.not_found", 404)
    return index


def _save_card(lesson: Lesson, cards: list) -> dict:
    folder = lessons.folder(lesson.id) / "images"
    pictures.prune(folder, cards)
    content = LessonIn(**{**lesson.model_dump(include=set(LessonIn.model_fields)), "cards": cards})
    return lessons.update(lesson.id, content).model_dump()


@app.post("/api/lessons/{id}/cards/{card_id}/picture/draw")
async def redraw_picture(id: str, card_id: str, req: PictureRequest) -> dict:
    """Draw a card's picture (again): the AI's subject, or one the user wrote. Never
    from the cache: this is asked when the picture didn't suit."""
    lesson = await _editable(id)
    cards = [card.model_copy(deep=True) for card in lesson.cards]
    card = cards[_card(lesson, card_id)]
    subject = (req.subject if req.subject is not None else card.picture_prompt).strip()
    if not subject:
        raise AppError("picture.no_subject")
    s = settings.current()
    with llm.recording("picture") as calls:
        try:
            jpeg = await pictures.picture(s, subject, fresh=True)
        finally:
            lessons.add_ai_calls(id, calls)
            usage.add(calls, id, lesson.deck)
    card.picture_prompt = subject
    card.picture = pictures.save(lessons.folder(id) / "images", card, jpeg)
    return {"card": card, "lesson": _save_card(lessons.get(id), cards)}


@app.post("/api/lessons/{id}/cards/{card_id}/picture", status_code=201)
async def upload_picture(id: str, card_id: str, photo: UploadFile) -> dict:
    """The user's own photo as the card's picture."""
    lesson = await _editable(id)
    if photo.content_type not in IMAGE_TYPES:
        raise AppError("extract.bad_format", format=photo.content_type)
    cards = [card.model_copy(deep=True) for card in lesson.cards]
    card = cards[_card(lesson, card_id)]
    try:
        card.picture = pictures.save(lessons.folder(id) / "images", card, await photo.read())
    except (OSError, ValueError) as e:
        raise AppError("extract.bad_format", format=photo.content_type) from e
    return {"card": card, "lesson": _save_card(lesson, cards)}


@app.delete("/api/lessons/{id}/cards/{card_id}/picture")
async def remove_picture(id: str, card_id: str) -> dict:
    """No picture on the card any more: it becomes a text card."""
    lesson = await _editable(id)
    cards = [card.model_copy(deep=True) for card in lesson.cards]
    card = cards[_card(lesson, card_id)]
    card.picture = card.picture_prompt = ""
    return {"card": card, "lesson": _save_card(lesson, cards)}


@app.get("/api/lessons/{id}/pictures/{name}")
async def get_picture(id: str, name: str) -> FileResponse:
    await _lesson(id)
    folder = lessons.folder(id)
    path = folder / "images" / name if folder and pictures.NAME.match(name) else None
    if path is None or not path.is_file():
        raise AppError("photo.not_found", 404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/lessons/{id}/photos/{n}")
async def get_photo(id: str, n: int) -> FileResponse:
    await _lesson(id)
    path = lessons.photo_path(id, n)
    if path is None or not path.is_file():
        raise AppError("photo.not_found", 404)
    return FileResponse(path, media_type="image/jpeg")


# --- Voices ----------------------------------------------------------------


@app.get("/api/voices")
async def list_voices() -> list[dict]:
    try:
        return await tts.voices()
    except Exception as e:
        raise AppError("tts.voices_unavailable", 502, detail=str(e)) from e


@app.get("/api/tts")
async def preview(text: str, voice: str, lesson: str | None = None) -> FileResponse:
    """Listen to a text: stored in the lesson's audio/ folder when a lesson is given
    (so the export reuses it), in the preview cache otherwise."""
    if tts.is_anki_locale(voice):
        raise AppError("tts.no_preview_for_anki_locale")
    directory = lessons.audio_dir(lesson) if lesson else None
    try:
        path = await tts.tts(text[:200], voice, directory)
    except Exception as e:
        raise AppError("tts.failed", 502, detail=str(e)) from e
    return FileResponse(path, media_type="audio/mpeg")


# --- Admin -----------------------------------------------------------------


def is_local(request: Request) -> bool:
    """Request made on this computer itself (not from a phone, not relayed by a proxy)."""
    host = request.client.host if request.client else ""
    relayed = "x-forwarded-for" in request.headers or "forwarded" in request.headers
    return host in ("127.0.0.1", "::1") and not relayed


def require_admin(request: Request, x_admin_password: str | None = Header(None)) -> None:
    # In the Anki add-on, settings are opened on the computer (Tools → Cartable →
    # Settings): API keys never cross the Wi-Fi and phones can't change anything.
    if settings.embedded():
        if not is_local(request):
            raise AppError("admin.local_only", 403)
        return
    if not settings.check_password(x_admin_password):
        raise AppError("admin.password_required", 401)


def _settings_view() -> dict:
    """Current settings for the admin page, API keys masked."""
    current = settings.current()
    s = current.model_dump()
    for field in settings.SECRET_FIELDS:
        s[field] = settings.masked(s[field])
    # OpenAI-compatible: the key of each service, masked, so the page shows the
    # right one when switching service; the .env key applies to the others.
    s["openai_keys"] = {url: settings.masked(key) for url, key in current.openai_keys.items()}
    s["openai_api_key"] = settings.masked(current.openai_key())
    s["openai_default_key"] = settings.masked(current.openai_api_key)
    return {
        **s,
        "providers": settings.PROVIDERS,
        "default_models": settings.DEFAULT_MODELS,
        "embedded": settings.embedded(),
        # The image model used when none is set: from the saved keys
        "picture_default": pictures.model(current.model_copy(update={"picture_model": ""})),
    }


@app.get("/api/admin")
def admin_status(request: Request) -> dict:
    """What the admin page must do: ask for a password, or explain it only opens on the computer."""
    embedded = settings.embedded()
    return {
        "password_set": settings.password_is_set(),
        "password_needed": not embedded,
        "allowed": not embedded or is_local(request),
    }


@app.post("/api/admin/password", status_code=204)
def admin_password(body: AdminPassword, request: Request) -> None:
    """First visit: create the password. Afterwards: change it (current one required)."""
    if settings.embedded():  # no password in the add-on: the settings only open on the computer
        raise AppError("admin.local_only", 403)
    if settings.password_is_set() and not settings.check_password(body.current):
        raise AppError("admin.wrong_password", 401)
    settings.set_password(body.new)


@app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
def admin_settings() -> dict:
    return _settings_view()


@app.put("/api/admin/settings", dependencies=[Depends(require_admin)])
def admin_save_settings(changes: SettingsUpdate) -> dict:
    settings.save(changes.model_dump())
    return _settings_view()


@app.post("/api/admin/models", dependencies=[Depends(require_admin)])
async def admin_models() -> dict:
    """Models of the saved OpenAI-compatible service, to fill the model suggestions."""
    return await list_models(settings.current())


@app.post("/api/admin/test", dependencies=[Depends(require_admin)])
async def admin_test() -> dict:
    """Check the saved provider, model and key: can it read an image and answer in JSON?"""
    s = settings.current()
    start = time.monotonic()
    result = await check(s)
    return {"llm": s.llm, "model": s.model_for_provider(), "seconds": round(time.monotonic() - start, 1), **result}


@app.get("/api/admin/lessons", dependencies=[Depends(require_admin)])
async def admin_lessons() -> dict:
    """Every lesson, whoever owns it, and Anki's profiles (None: Anki not reachable)."""
    return {"lessons": lessons.list_all(), "profiles": await ankiconnect.profiles(), "costs": usage.totals()}


@app.put("/api/admin/lessons/{id}", dependencies=[Depends(require_admin)])
def admin_lesson_access(id: str, access: LessonAccess) -> LessonSummary:
    """Give a lesson to another profile (or to nobody), share it or not."""
    lesson = lessons.set_access(id, access.owner, access.shared)
    if lesson is None:
        raise AppError("lesson.not_found", 404)
    return LessonSummary(card_count=len(lesson.cards), **lesson.model_dump(exclude={"cards"}))


@app.delete("/api/admin/lessons/{id}", status_code=204, dependencies=[Depends(require_admin)])
def admin_delete_lesson(id: str) -> None:
    if not lessons.delete(id):
        raise AppError("lesson.not_found", 404)


@app.get("/api/qr")
def qr(text: str) -> Response:
    """QR code (PNG) of a text, e.g. the address to open on the phone."""
    import segno

    out = io.BytesIO()
    segno.make(text[:500], error="m").save(out, kind="png", scale=8, border=2)
    return Response(out.getvalue(), media_type="image/png")


class PageFiles(StaticFiles):
    """The page's files, revalidated on every load (ETag: a cheap 304 when unchanged).
    Without it, browsers keep old copies after an update: new HTML with old
    translations or styles."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", PageFiles(directory=STATIC_DIR, html=True), name="static")
