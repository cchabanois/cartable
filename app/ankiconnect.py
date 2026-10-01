"""Send notes straight into Anki desktop through the AnkiConnect add-on.

https://foosoft.net/projects/anki-connect/ — Anki must be running with the add-on
installed. Notes already sent are updated (same note type, deck and front) instead
of duplicated; notes deleted in Cartable are left untouched in Anki.
"""

import base64
from dataclasses import dataclass

import httpx

from . import settings
from .anki import Note
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

        added = updated = 0
        for deck, nt in dict.fromkeys((n.deck, n.nt) for n in notes):
            # createDeck returns the id of the deck, existing or new. Searching by id
            # and comparing keys here avoids escaping names in Anki's search syntax.
            deck_id = await _invoke(client, "createDeck", deck=deck)
            ids = await _invoke(client, "findNotes", query=f'"note:{nt.name}" did:{deck_id}')
            infos = await _invoke(client, "notesInfo", notes=ids) if ids else []
            existing = {info["fields"][nt.key]["value"]: info["noteId"] for info in infos if nt.key in info["fields"]}
            for note in (n for n in notes if (n.deck, n.nt) == (deck, nt)):
                if note.key in existing:
                    await _invoke(client, "updateNoteFields", note={"id": existing[note.key], "fields": note.fields})
                    updated += 1
                else:
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

        result = SendResult(added, updated, synced=False)
        if settings.current().anki_sync and await sync_configured() is False:
            result.sync_skipped = True  # no AnkiWeb login on this profile: nothing to warn about at each send
        elif settings.current().anki_sync:
            try:
                await _invoke(client, "sync")
                result.synced = True
            except AnkiConnectError as e:  # the notes are in Anki anyway
                result.sync_error = e.detail()
        return result
