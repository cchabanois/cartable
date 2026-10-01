"""Every AI call ever made, in data/ai-calls.json: what was spent, whatever became of
the lessons.

A lesson keeps its own calls, but deleting it must not make the money spent
disappear, and a generation that failed after the model answered (paid for, no
lesson) has nowhere else to go. Entries are only ever added.
"""

from pydantic import BaseModel

from . import lessons, storage
from .models import AiCall


class Entry(AiCall):
    lesson_id: str | None = None  # None: a generation that failed (no lesson)
    deck: str = ""  # the lesson's name at the time


def _path():
    return storage.data_dir() / "ai-calls.json"


def _load() -> list[Entry]:
    """The journal; the first time, filled with the calls the lessons already keep."""
    data = storage.read_json(_path())
    if data is not None:
        return [Entry(**e) for e in data]
    entries = []
    for summary in lessons.list_all():
        for call in summary.ai_calls:
            entries.append(Entry(**call.model_dump(), lesson_id=summary.id, deck=summary.deck))
    entries.sort(key=lambda e: e.at)
    _save(entries)
    return entries


def _save(entries: list[Entry]) -> None:
    storage.write_json(_path(), [e.model_dump() for e in entries])


def _key(e: Entry) -> tuple:
    return e.lesson_id, e.at, e.kind, e.model, e.input_tokens, e.output_tokens


def add(calls: list[AiCall], lesson_id: str | None, deck: str = "") -> None:
    if not calls:
        return
    with storage.lock:
        entries = _load()
        known = {_key(e) for e in entries}
        new = [Entry(**c.model_dump(), lesson_id=lesson_id, deck=deck) for c in calls]
        _save([*entries, *(e for e in new if _key(e) not in known)])  # filled from the lessons just before


class Totals(BaseModel):
    total: float  # US dollars, calls with a known cost
    deleted: float  # of it, for lessons deleted since
    failed: float  # of it, generations that failed
    estimated: bool  # some costs are estimates (not given by the service)
    unknown: int  # calls whose cost is unknown


def totals() -> Totals:
    with storage.lock:
        entries = _load()
    existing = {s.id for s in lessons.list_all()}
    known = [e for e in entries if e.cost is not None]
    return Totals(
        total=round(sum(e.cost for e in known), 6),
        deleted=round(sum(e.cost for e in known if e.lesson_id and e.lesson_id not in existing), 6),
        failed=round(sum(e.cost for e in known if e.lesson_id is None), 6),
        estimated=any(not e.exact and e.cost for e in known),
        unknown=sum(1 for e in entries if e.cost is None),
    )
