"""Saved lessons: one folder per lesson under data/lessons/.

<yyyy-mm-dd-deck-slug>/
  lesson.json     deck, cards, prompt text, voice, dates
  page-1.jpg …    the photos, in order
  audio/          mp3 of the card backs, embedded in exported packages
"""

import hashlib
import logging
import re
import shutil
import uuid
from datetime import date
from pathlib import Path

from . import storage
from .models import AiCall, Card, Frame, Lesson, LessonIn, LessonSummary

log = logging.getLogger("notosaurus")

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # folder names we create; blocks "../"


def _root() -> Path:
    return storage.data_dir() / "lessons"


def folder(id: str) -> Path | None:
    """Folder of an existing lesson, or None (unknown or malformed id)."""
    if not ID_PATTERN.match(id):
        return None
    path = _root() / id
    return path if (path / "lesson.json").is_file() else None


def photo_path(id: str, n: int) -> Path | None:
    path = folder(id)
    return path / f"page-{n}.jpg" if path else None


def audio_dir(id: str) -> Path | None:
    path = folder(id)
    return path / "audio" if path else None


def _count_photos(path: Path) -> int:
    return len(list(path.glob("page-*.jpg")))


# lesson.json's format; storage.migrate brings older files up to it
FORMAT = 1


def _read(path: Path) -> Lesson:
    data = storage.migrate(storage.read_json(path / "lesson.json"), "lesson", FORMAT, {0: dict})
    lesson = Lesson(id=path.name, photo_count=_count_photos(path), **data)
    # Cards saved before cards had ids: the same id at every reading, until it's saved
    for n, card in enumerate(lesson.cards):
        if not card.id:
            card.id = hashlib.sha1(f"{path.name}|{n}|{card.front}".encode()).hexdigest()[:12]
    return lesson


def with_ids(cards: list[Card]) -> list[Card]:
    """New cards (from the AI, or added) get their stable id."""
    for card in cards:
        if not card.id:
            card.id = uuid.uuid4().hex[:12]
    return cards


def _write(path: Path, lesson: Lesson) -> None:
    with_ids(lesson.cards)
    storage.write_json(path / "lesson.json", {"format": FORMAT, **lesson.model_dump(exclude={"id", "photo_count"})})


def _new_folder(deck: str) -> Path:
    base = f"{date.today().isoformat()}-{storage.slugify(deck) or 'lesson'}"
    path, n = _root() / base, 2
    while path.exists():
        path, n = _root() / f"{base}-{n}", n + 1
    return path


def create(
    lesson: LessonIn,
    prompt: str,
    photos: list[bytes],
    owner: str = "",
    frames: list[Frame] = (),
    ai_calls: list[AiCall] = (),
    choice: str = "",
) -> Lesson:
    with storage.lock:
        path = _new_folder(lesson.deck)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.mkdir(parents=True)
        try:
            for n, data in enumerate(photos, start=1):
                (tmp / f"page-{n}.jpg").write_bytes(data)
            now = storage.now()
            _write(
                tmp,
                Lesson(
                    **{
                        **lesson.model_dump(exclude={"prompt", "choice"}),
                        "owner": owner,
                        "shared": False,
                        "frames": list(frames),
                    },
                    ai_calls=list(ai_calls),
                    choice=choice,
                    id=path.name,
                    prompt=prompt,
                    photo_count=len(photos),
                    created_at=now,
                    updated_at=now,
                ),
            )
            tmp.rename(path)  # the lesson appears complete, or not at all
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return _read(path)


def get(id: str) -> Lesson | None:
    path = folder(id)
    return _read(path) if path else None


def list_all() -> list[LessonSummary]:
    root = _root()
    if not root.is_dir():
        return []
    summaries = []
    for path in (p for p in root.iterdir() if (p / "lesson.json").is_file()):
        try:
            lesson = _read(path)
        except storage.DataTooNew as e:  # saved by a newer Notosaurus: left alone
            log.warning("Lesson %s not listed: %s", path.name, e.detail_text)
            continue
        summaries.append(LessonSummary(card_count=len(lesson.cards), **lesson.model_dump(exclude={"cards"})))
    return sorted(summaries, key=lambda s: (s.updated_at, s.id), reverse=True)


def update(id: str, changes: LessonIn, exported: bool = False, share: bool | None = None) -> Lesson | None:
    """Save the lesson's content. Sharing only changes through `share`, after the
    caller checked it's the owner's profile (content updates never touch it)."""
    with storage.lock:
        path = folder(id)
        if not path:
            return None
        now = storage.now()
        # Validated again (not model_copy): the cards become Card objects, not dicts
        lesson = Lesson(
            **{
                **_read(path).model_dump(),
                **changes.model_dump(include=set(LessonIn.model_fields) - {"shared"}, exclude_none=True),
                **({"shared": share} if share is not None else {}),
                "updated_at": now,
                **({"exported_at": now} if exported else {}),
            }
        )
        _write(path, lesson)
    return lesson


def regenerated(
    id: str, content: LessonIn, prompt: str, photos: list[bytes], frames: list[Frame], calls: list[AiCall], choice: str
) -> Lesson | None:
    """A new generation in place of the lesson's content: its cards, photos, frames and
    prompt replaced; its owner, sharing, dates and AI calls kept (the new ones added)."""
    with storage.lock:
        path = folder(id)
        if not path:
            return None
        old = _read(path)
        for photo in path.glob("page-*.jpg"):
            photo.unlink()
        for n, data in enumerate(photos, start=1):
            (path / f"page-{n}.jpg").write_bytes(data)
        # images/ is kept: "Undo" brings back the old cards with their pictures (pruned at export)
        lesson = Lesson(
            **{
                **old.model_dump(),
                **content.model_dump(include=set(LessonIn.model_fields) - {"shared"}, exclude_none=True),
                "id": id,
                "prompt": prompt,
                "choice": choice,
                "frames": list(frames),
                "photo_count": len(photos),
                "ai_calls": [*old.ai_calls, *calls],
                "updated_at": storage.now(),
            }
        )
        _write(path, lesson)
    return _read(path)


def add_ai_calls(id: str, calls: list[AiCall]) -> None:
    """Keep the AI calls of a correction with the lesson (not a content change)."""
    if not calls:
        return
    with storage.lock:
        path = folder(id)
        if path:
            lesson = _read(path)
            _write(path, lesson.model_copy(update={"ai_calls": [*lesson.ai_calls, *calls]}))


def set_access(id: str, owner: str | None = None, shared: bool | None = None) -> Lesson | None:
    """Change who a lesson belongs to (settings page). Not a content change: the
    lesson keeps its place in the list."""
    changes = {k: v for k, v in {"owner": owner, "shared": shared}.items() if v is not None}
    with storage.lock:
        path = folder(id)
        if not path:
            return None
        lesson = _read(path).model_copy(update=changes)
        _write(path, lesson)
    return lesson


def delete(id: str) -> bool:
    with storage.lock:
        path = folder(id)
        if not path:
            return False
        shutil.rmtree(path)
    return True
