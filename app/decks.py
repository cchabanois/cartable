"""Decks that already exist: the open Anki profile's, and those of the lessons it
sees. Given to the AI so a lesson goes into the right deck ("Maths", not a new
"Mathématiques"), and offered when the deck name is edited."""

from . import ankiconnect, lessons

LIMIT = 150  # enough to recognise the subjects, light in the request
SKIPPED = {"Default"}  # Anki's own deck


def _with_parents(name: str) -> list[str]:
    parts = [p.strip() for p in name.split("::") if p.strip()]
    return ["::".join(parts[: n + 1]) for n in range(len(parts))]


async def known(profile: str | None) -> list[str]:
    """Sorted names; the top levels first kept when there are too many. Anki closed:
    the lessons' decks alone."""
    names = {n for n in await ankiconnect.deck_names() or [] if n not in SKIPPED}
    for lesson in lessons.list_all():
        if lesson.shared or not lesson.owner or not profile or lesson.owner == profile:
            names.update(_with_parents(lesson.deck))
    kept = sorted(names, key=lambda n: (n.count("::"), n.casefold()))[:LIMIT]
    return sorted(kept, key=str.casefold)
