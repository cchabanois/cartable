"""Send notes straight into Anki desktop through the AnkiConnect add-on.

https://foosoft.net/projects/anki-connect/ — Anki must be running with the add-on
installed. Notes already sent are updated (same note type, deck and front) instead
of duplicated; notes deleted in Cartable are left untouched in Anki.
"""

import base64
from dataclasses import dataclass

import httpx

from . import settings
from .anki import TAG_PREFIX, Note, family
from .errors import AppError

TIMEOUT = 30.0
_transport: httpx.AsyncBaseTransport | None = None  # tests plug a fake AnkiConnect here


class AnkiConnectError(AppError):
    status = 502


# Messages from AnkiConnect / the Cartable bridge that have their own error code.
KNOWN_ERRORS = {
    "auth not configured": "anki.sync_not_logged_in",
    "no profile open": "anki.no_profile",
}


@dataclass
class SendResult:
    added: int
    updated: int
    synced: bool
    sync_error: dict | None = None  # {"code", "params"}, translated by the page
    sync_skipped: bool = False  # the profile isn't logged in to AnkiWeb: not tried
    converted: int = 0  # of the updated notes, those moved to another note type (options changed)
    conversion_unsupported: bool = False  # AnkiConnect too old to change a note's type: added instead


async def _invoke(client: httpx.AsyncClient, action: str, **params):
    s = settings.current()
    body = {"action": action, "version": 6, "params": params}
    if s.ankiconnect_key:
        body["key"] = s.ankiconnect_key
    try:
        response = await client.post(s.ankiconnect_url, json=body)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as e:
        raise AnkiConnectError("anki.unreachable") from e
    except ValueError as e:
        raise AnkiConnectError("anki.bad_response") from e
    if error := data.get("error"):
        code = next((c for text, c in KNOWN_ERRORS.items() if text in str(error)), "anki.error")
        raise AnkiConnectError(code, action=action, detail=str(error))
    return data.get("result")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)


async def active_profile() -> str | None:
    """Anki profile open right now, or None (Anki closed, profile screen, old AnkiConnect)."""
    try:
        async with httpx.AsyncClient(timeout=2.0, transport=_transport) as client:
            return await _invoke(client, "getActiveProfile") or None
    except AnkiConnectError:
        return None


async def sync_configured() -> bool | None:
    """Whether the open profile is logged in to AnkiWeb. The add-on's bridge knows;
    AnkiConnect has no such action: None (unknown)."""
    try:
        async with httpx.AsyncClient(timeout=2.0, transport=_transport) as client:
            result = await _invoke(client, "isSyncConfigured")
    except AnkiConnectError:
        return None
    return result if isinstance(result, bool) else None


async def profiles() -> list[str] | None:
    """Every Anki profile, or None when Anki can't be reached."""
    try:
        async with httpx.AsyncClient(timeout=2.0, transport=_transport) as client:
            return list(await _invoke(client, "getProfiles") or [])
    except AnkiConnectError:
        return None


async def deck_names() -> list[str] | None:
    """The open profile's decks, or None when Anki can't be reached."""
    try:
        async with httpx.AsyncClient(timeout=2.0, transport=_transport) as client:
            return list(await _invoke(client, "deckNames") or [])
    except AnkiConnectError:
        return None


async def version() -> int:
    async with _client() as client:
        return await _invoke(client, "version")


async def send(notes: list[Note]) -> SendResult:
    async with _client() as client:
        note_types = list(dict.fromkeys(n.nt for n in notes))
        known = await _invoke(client, "modelNames")
        for nt in note_types:
            if nt.name not in known:
                await _invoke(
                    client,
                    "createModel",
                    modelName=nt.name,
                    inOrderFields=list(nt.fields),
                    css=nt.css,
                    isCloze=nt.cloze,
                    cardTemplates=[{"Name": t["name"], "Front": t["qfmt"], "Back": t["afmt"]} for t in nt.templates],
                )
        for path in dict.fromkeys(p for n in notes for p in n.media):
            await _invoke(
                client, "storeMediaFile", filename=path.name, data=base64.b64encode(path.read_bytes()).decode()
            )

        added = updated = converted = 0
        unsupported = False
        retag: dict[str, list[int]] = {}  # notes sent before they had the lesson's tag
        others: dict[str, dict] = {}  # per deck: its notes of the other Cartable note types
        for deck, nt in dict.fromkeys((n.deck, n.nt) for n in notes):
            # createDeck returns the id of the deck, existing or new. Searching by id
            # and comparing keys here avoids escaping names in Anki's search syntax.
            deck_id = await _invoke(client, "createDeck", deck=deck)
            ids = await _invoke(client, "findNotes", query=f'"note:{nt.name}" did:{deck_id}')
            infos = await _invoke(client, "notesInfo", notes=ids) if ids else []
            # By key; two cards with the same front ("le vol": vuelo, robo) have a note each
            existing: dict[str, list[int]] = {}
            for info in infos:
                if nt.key in info["fields"]:
                    existing.setdefault(info["fields"][nt.key]["value"], []).append(info["noteId"])
            for note in (n for n in notes if (n.deck, n.nt) == (deck, nt)):
                if existing.get(note.key):
                    note_id = existing[note.key].pop(0)
                    await _invoke(client, "updateNoteFields", note={"id": note_id, "fields": note.fields})
                    for tag in (t for t in note.tags if t.startswith(TAG_PREFIX)):
                        retag.setdefault(tag, []).append(note_id)
                    updated += 1
                    continue
                if deck not in others:
                    others[deck] = await _other_cartable_notes(client, deck)
                # Sent before with other options (voice, reverse, typing, dictation): the same
                # note moves to the new note type, keeping its review history
                candidates = [
                    i for i in others[deck].get((nt.family, nt.key, note.key), []) if i["modelName"] != nt.name
                ]
                old = candidates[0] if candidates else None
                if old and not unsupported:
                    try:
                        tags = sorted({*old["tags"], *note.tags})
                        changed = {"id": old["noteId"], "modelName": nt.name, "fields": note.fields, "tags": tags}
                        await _invoke(client, "updateNoteModel", note=changed)
                        others[deck][(nt.family, nt.key, note.key)].remove(old)
                        updated += 1
                        converted += 1
                        continue
                    except AnkiConnectError as e:
                        if "unsupported action" not in str(e.params.get("detail", "")):
                            raise
                        unsupported = True  # an old AnkiConnect: added next to it, as before
                await _invoke(
                    client,
                    "addNote",
                    note={
                        "deckName": deck,
                        "modelName": nt.name,
                        "fields": note.fields,
                        "tags": note.tags,
                        "options": {"allowDuplicate": True},  # same front in another deck is fine
                    },
                )
                added += 1

        for tag, ids in retag.items():
            await _invoke(client, "addTags", notes=ids, tags=tag)

        result = SendResult(added, updated, synced=False, converted=converted, conversion_unsupported=unsupported)
        await _sync(client, result)
        return result


async def _other_cartable_notes(client: httpx.AsyncClient, deck: str) -> dict[tuple, list[dict]]:
    """The deck's Cartable notes (not its subdecks'), by (family, key field, key value)."""
    name = _search(deck)
    ids = await _invoke(client, "findNotes", query=f'"deck:{name}" -"deck:{name}::*" "note:Cartable*"')
    found = {}
    for info in await _invoke(client, "notesInfo", notes=ids) if ids else []:
        kind = family(info.get("modelName", ""))
        for key in ("Front", "Id"):
            if kind and key in info["fields"]:
                found.setdefault((kind, key, info["fields"][key]["value"]), []).append(info)
    return found


async def _sync(client: httpx.AsyncClient, result: SendResult) -> None:
    """Sync with AnkiWeb when the settings ask for it; a failure is told, not fatal."""
    if not settings.current().anki_sync:
        return
    if await sync_configured() is False:
        result.sync_skipped = True  # no AnkiWeb login on this profile: nothing to warn about at each change
        return
    try:
        await _invoke(client, "sync")
        result.synced = True
    except AnkiConnectError as e:  # the change is in Anki anyway
        result.sync_error = e.detail()


def _search(text: str) -> str:
    """A name inside a quoted Anki search: its quotes, backslashes and wildcards escaped."""
    return "".join("\\" + c if c in '\\"*_' else c for c in text)


async def find_lesson_notes(
    lesson_id: str,
    notes: list[Note],
    others: set[tuple[str, str]] = frozenset(),
    other_tags: set[str] = frozenset(),
) -> list[int]:
    """The lesson's notes in the open profile: those with its tag, and those sent before
    notes had it (same note type and deck, same key). Never a note another lesson uses
    too: tagged for it as well (`other_tags`: the tags of the lessons that still exist),
    or untagged with the deck and key of one of its cards (`others`)."""
    own = TAG_PREFIX + lesson_id
    async with _client() as client:
        tagged = await _invoke(client, "findNotes", query=f'"tag:{_search(own)}"')
        infos = await _invoke(client, "notesInfo", notes=tagged) if tagged else []
        found = {i["noteId"] for i in infos if not other_tags & set(i["tags"])}
        for deck, nt in dict.fromkeys((n.deck, n.nt) for n in notes):
            keys = {n.key for n in notes if (n.deck, n.nt) == (deck, nt)} - {k for d, k in others if d == deck}
            name = _search(deck)
            query = f'"note:{_search(nt.name)}" "deck:{name}" -"deck:{name}::*" -"tag:{_search(TAG_PREFIX)}*"'
            ids = await _invoke(client, "findNotes", query=query)  # this deck (not its subdecks), untagged
            infos = await _invoke(client, "notesInfo", notes=ids) if ids else []
            found.update(i["noteId"] for i in infos if i["fields"].get(nt.key, {}).get("value") in keys)
        return sorted(found)


def _with_parents(decks: list[str]) -> set[str]:
    return {"::".join(d.split("::")[: n + 1]) for d in decks for n in range(d.count("::") + 1)}


async def delete_notes(ids: list[int], decks: list[str]) -> SendResult:
    """Delete these notes (their review history with them), then `decks` and their parent
    decks left without any card (a deck still holding cards, even in a subdeck, stays;
    Anki's "Default" too), then sync."""
    async with _client() as client:
        if ids:
            await _invoke(client, "deleteNotes", notes=ids)
        parents_too = _with_parents(decks) - {"Default"}
        for deck in sorted(parents_too, key=lambda d: d.count("::"), reverse=True):  # subdecks first
            if not await _invoke(client, "findCards", query=f'"deck:{_search(deck)}"'):
                await _invoke(client, "deleteDecks", decks=[deck], cardsToo=True)
        result = SendResult(added=0, updated=0, synced=False)
        await _sync(client, result)
        return result
