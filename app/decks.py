"""Decks that already exist: the open Anki profile's, and those of the lessons it
sees. Given to the AI so a lesson goes into the right deck ("Maths", not a new
"Mathématiques"), and offered when the deck name is edited."""

from . import ankiconnect, lessons

LIMIT = 150  # enough to recognise the subjects, light in the request
SKIPPED = {"Default"}  # Anki's own deck


def _with_parents(name: str) -> list[str]:
    parts = [p.strip() for p in name.split("::") if p.strip()]
    return ["::".join(parts[: n + 1]) for n in range(len(parts))]


async def _all(profile: str | None, but: str | None = None) -> set[str]:
    """Every deck name: Anki's (Anki closed: none) and the lessons' this profile sees,
    their parents included; `but`: a lesson left out (the one being generated again)."""
    names = {n for n in await ankiconnect.deck_names() or [] if n not in SKIPPED}
    own = lessons.get(but).deck.strip() if but and lessons.get(but) else None
    if own:  # its deck (and subdecks) in Anki are its own, not another's
        names = {
            n for n in names if n.casefold() != own.casefold() and not n.casefold().startswith(own.casefold() + "::")
        }
    for lesson in lessons.list_all():
        if lesson.id != but and (lesson.shared or not lesson.owner or not profile or lesson.owner == profile):
            names.update(_with_parents(lesson.deck))
    return names


async def known(profile: str | None) -> list[str]:
    """Sorted names; the top levels first kept when there are too many. Anki closed:
    the lessons' decks alone."""
    kept = sorted(await _all(profile), key=lambda n: (n.count("::"), n.casefold()))[:LIMIT]
    return sorted(kept, key=str.casefold)


async def new_name(deck: str, profile: str | None, but: str | None = None) -> str:
    """The lesson's own deck, new: a deck that already exists (in Anki, or another
    lesson's) gets " (2)", " (3)"… so two lessons never write into the same deck. Its
    parents may exist ("Maths::Fractions" in "Maths"). `but`: the lesson itself, when
    generated again. Anki doesn't tell decks apart by case: neither does this."""
    deck = deck.strip()
    taken = {name.casefold() for name in await _all(profile, but)}
    if not deck or deck.casefold() not in taken:
        return deck
    n = 2
    while f"{deck} ({n})".casefold() in taken:
        n += 1
    return f"{deck} ({n})"
