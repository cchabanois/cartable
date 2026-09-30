import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # before the app imports, some of which read variables at import time

import io

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import ankiconnect, diagrams, i18n, lessons, prompts, settings, tts
from .anki import build_apkg, notes
from .errors import AppError
from .llm import Image, check, extract_cards, list_models, revise_cards
from .models import (
    AdminPassword,
    Deck,
    ExportRequest,
    Lesson,
    LessonAccess,
    LessonIn,
    LessonSummary,
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
    return prompts.list_all(lang)  # first start: default prompts in this language


@app.post("/api/prompts", status_code=201)
def add_prompt(c: PromptIn) -> Prompt:
    return prompts.add(c)


@app.put("/api/prompts/{id}")
def update_prompt(id: int, c: PromptIn) -> Prompt:
    if updated := prompts.update(id, c):
        return updated
    raise AppError("prompt.not_found", 404)


@app.delete("/api/prompts/{id}", status_code=204)
def delete_prompt(id: int) -> None:
    if not prompts.delete(id):
        raise AppError("prompt.not_found", 404)


# --- Extraction & export ---------------------------------------------------


@app.post("/api/extract", status_code=201)
async def extract(
    images: list[UploadFile] = File([]),
    prompt: str = Form(...),
    deck: str = Form(""),
    voice: str = Form(""),
    prompt_id: int | None = Form(None),
) -> Lesson:
    """Read the photos (or, without photos, work from the prompt alone), then save the
    lesson (photos + cards) so it can be reopened."""
    if not images and not prompt.strip():
        raise AppError("extract.no_input")
    if len(images) > MAX_IMAGES:
        raise AppError("extract.too_many", max=MAX_IMAGES)
    for img in images:
        if img.content_type not in IMAGE_TYPES:
            raise AppError("extract.bad_format", format=img.content_type)

    data = [Image(await img.read(), img.content_type) for img in images]
    extraction = await extract_cards(data, prompt, deck)
    if prompt_id is not None:
        prompts.mark_used(prompt_id)
    # Photos taken sideways are saved upright (the masks turn with them)
    photos = diagrams.straighten([i.data for i in data], extraction.cards, extraction.rotations)
    profile = await ankiconnect.active_profile() or ""  # the lesson belongs to this Anki profile
    lesson = LessonIn(**extraction.model_dump(exclude={"rotations"}), voice=voice)
    return lessons.create(lesson, prompt, photos, profile)


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
    backs = [c.back.strip() for c in req.cards if c.front.strip() and c.back.strip()]
    audio, failures = await tts.tts_many(backs, req.voice, directory)
    tts.prune(directory, set(audio.values()))
    return audio, failures


def _diagram_images(req: ExportRequest, lesson: Lesson | None) -> dict[int, Path]:
    """The diagram image of each diagram card (index in req.cards → path), from the
    saved lesson's photos. Without a saved lesson, there is no photo to show."""
    folder = lessons.folder(lesson.id) if lesson else None
    if folder is None:
        return {}
    images, used = {}, set()
    for i, card in enumerate(req.cards):
        photo = lessons.photo_path(lesson.id, card.mask.page) if card.mask else None
        if photo and photo.is_file():
            images[i] = diagrams.page_image(folder / "images", photo)
            used.add(images[i])
    diagrams.prune(folder / "images", used)
    return images


async def _lesson(id: str | None) -> Lesson | None:
    """The lesson, if the open Anki profile may see it: when "see other profiles'
    lessons" is off in the settings, another profile's private lessons don't exist."""
    lesson = lessons.get(id) if id else None
    if lesson is None:
        if id:
            raise AppError("lesson.not_found", 404)
        return None
    if not settings.current().all_profiles_view and not lesson.visible_to(await ankiconnect.active_profile()):
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
    path = build_apkg(req, audio, _diagram_images(req, lesson))
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


@app.get("/api/anki/status")
async def anki_status() -> dict:
    try:
        return {
            "available": True,
            "version": await ankiconnect.version(),
            "profile": await ankiconnect.active_profile(),
        }
    except ankiconnect.AnkiConnectError as e:
        return {"available": False, "error": e.detail()}


@app.post("/api/anki/send")
async def anki_send(req: ExportRequest, background: BackgroundTasks) -> dict:
    lesson = await _lesson(req.lesson_id)
    if lesson and lesson.owner and not lesson.shared and not req.force:
        active = await ankiconnect.active_profile()
        if active and active != lesson.owner:
            # Don't write one child's lesson into another child's collection by mistake.
            raise AppError("anki.profile_mismatch", 409, lesson_profile=lesson.owner, active_profile=active)
    save = lesson and await _is_owner(lesson)  # someone else's lesson: sent, not changed
    audio, failures = await _card_audio(req, background)
    result = await ankiconnect.send(notes(req, audio, _diagram_images(req, lesson)))
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
    return {"all_profiles_view": s.all_profiles_view, "version": VERSION, "diagram_warning": loose_boxes}


@app.get("/api/lessons")
async def list_lessons() -> list[LessonSummary]:
    summaries = lessons.list_all()
    if settings.current().all_profiles_view:
        return summaries
    profile = await ankiconnect.active_profile()
    return [s for s in summaries if s.shared or not s.owner or s.owner == profile]


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
    revision = await revise_cards(photos, lesson.prompt, Deck(deck=req.deck, cards=req.cards), req.instruction, lang)
    updated = lessons.update(
        id, LessonIn(deck=revision.deck, cards=revision.cards, voice=req.voice, reverse=req.reverse)
    )
    return {"lesson": updated, "summary": revision.summary}


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
    return {"lessons": lessons.list_all(), "profiles": await ankiconnect.profiles()}


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
