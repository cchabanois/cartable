"""A small AnkiConnect-compatible endpoint inside Anki, for the Cartable server.

It implements only the actions Cartable's ankiconnect.py uses, on 127.0.0.1 with
a random key, so the same server code works with the real AnkiConnect
(standalone mode) or with this bridge (add-on mode). Collection access happens
on Anki's main thread, as Anki requires.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from aqt import mw
from aqt.qt import QTimer

VERSION = 6  # AnkiConnect API version we mimic
MAIN_THREAD_TIMEOUT = 60


class BridgeError(Exception):
    pass


def _on_main(fn: Callable[[], Any]) -> Any:
    """Run `fn` on Anki's main thread and wait for its result."""
    done = threading.Event()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except Exception as e:  # reported to the caller as an AnkiConnect error
            box["error"] = e
        finally:
            done.set()

    mw.taskman.run_on_main(run)
    if not done.wait(MAIN_THREAD_TIMEOUT):
        raise BridgeError("Anki is not responding (a dialog may be open)")
    if "error" in box:
        raise box["error"]
    return box.get("result")


_refresh_pending = False


def _schedule_refresh() -> None:
    """Refresh Anki's main window once after a burst of changes (main thread)."""
    global _refresh_pending
    if not _refresh_pending:
        _refresh_pending = True

        def refresh() -> None:
            global _refresh_pending
            _refresh_pending = False
            if mw.col:
                mw.reset()

        QTimer.singleShot(800, refresh)


def _col():
    if not mw.col:
        raise BridgeError("no profile open")
    return mw.col


# --- Actions (main thread) ----------------------------------------------------


def _model_names(p: dict) -> list[str]:
    return [m.name for m in _col().models.all_names_and_ids()]


def _create_model(p: dict) -> int:
    mm = _col().models
    model = mm.new(p["modelName"])
    if p.get("isCloze"):
        model["type"] = 1  # MODEL_CLOZE: one card per gap number
    for name in p["inOrderFields"]:
        mm.add_field(model, mm.new_field(name))
    for t in p["cardTemplates"]:
        template = mm.new_template(t["Name"])
        template["qfmt"], template["afmt"] = t["Front"], t["Back"]
        mm.add_template(model, template)
    model["css"] = p.get("css", model["css"])
    return mm.add(model).id


def _deck_names(p: dict) -> list[str]:
    return [d.name for d in _col().decks.all_names_and_ids()]


def _create_deck(p: dict) -> int:
    deck_id = _col().decks.id(p["deck"], create=True)
    _schedule_refresh()
    return deck_id


def _store_media_file(p: dict) -> str:
    return _col().media.write_data(p["filename"], base64.b64decode(p["data"]))


def _find_notes(p: dict) -> list[int]:
    return list(_col().find_notes(p["query"]))


def _notes_info(p: dict) -> list[dict]:
    result = []
    for note_id in p["notes"]:
        note = _col().get_note(note_id)
        fields = {name: {"value": value, "order": i} for i, (name, value) in enumerate(note.items())}
        result.append({"noteId": note.id, "modelName": note.note_type()["name"], "fields": fields, "tags": note.tags})
    return result


def _update_note_fields(p: dict) -> None:
    note = _col().get_note(p["note"]["id"])
    for name, value in p["note"]["fields"].items():
        if name in note:
            note[name] = value
    _col().update_note(note)
    _schedule_refresh()


def _update_note_model(p: dict) -> None:
    """Move a note to another note type (its options changed), then set its fields and
    tags. Fields and cards go to those of the same name, so a card keeps its review
    history; a card whose template is gone is deleted, a new template makes a card."""
    col = _col()
    spec = p["note"]
    note = col.get_note(spec["id"])
    old, new = note.note_type(), col.models.by_name(spec["modelName"])
    if new is None:
        raise BridgeError(f"unknown note type: {spec['modelName']}")
    if old["id"] != new["id"]:
        request = col.models.change_notetype_info(old_notetype_id=old["id"], new_notetype_id=new["id"]).input
        old_fields = [f["name"] for f in old["flds"]]
        old_templates = [t["name"] for t in old["tmpls"]]
        request.new_fields[:] = [old_fields.index(f["name"]) if f["name"] in old_fields else -1 for f in new["flds"]]
        request.new_templates[:] = [
            old_templates.index(t["name"]) if t["name"] in old_templates else -1 for t in new["tmpls"]
        ]
        request.note_ids[:] = [note.id]
        col.models.change_notetype_of_notes(request)
        note = col.get_note(note.id)
    for name, value in spec.get("fields", {}).items():
        if name in note:
            note[name] = value
    if "tags" in spec:
        note.tags = list(spec["tags"])
    col.update_note(note)
    _schedule_refresh()


def _add_note(p: dict) -> int:
    col = _col()
    spec = p["note"]
    model = col.models.by_name(spec["modelName"])
    if model is None:
        raise BridgeError(f"unknown note type: {spec['modelName']}")
    note = col.new_note(model)
    for name, value in spec["fields"].items():
        if name in note:
            note[name] = value
    note.tags = list(spec.get("tags", []))
    col.add_note(note, col.decks.id(spec["deckName"], create=True))
    _schedule_refresh()
    return note.id


def _find_cards(p: dict) -> list[int]:
    return list(_col().find_cards(p["query"]))


def _add_tags(p: dict) -> None:
    _col().tags.bulk_add(list(p["notes"]), p["tags"])


def _delete_notes(p: dict) -> None:
    _col().remove_notes(list(p["notes"]))
    _schedule_refresh()


def _delete_decks(p: dict) -> None:
    # Cartable only asks for decks it found empty; their cards would go too (cardsToo)
    col = _col()
    ids = [deck_id for name in p["decks"] if (deck_id := col.decks.id_for_name(name))]
    col.decks.remove(ids)
    _schedule_refresh()


def _sync(p: dict) -> None:
    # Never open the login dialog in the middle of a send: report it instead
    # (Cartable translates this into "Anki is not logged in to AnkiWeb").
    if not mw.pm.sync_auth():
        raise BridgeError("sync: auth not configured")
    mw.on_sync_button_clicked()


def _sync_configured(p: dict) -> bool:
    """Whether the open profile is logged in to AnkiWeb (not an AnkiConnect action):
    Cartable then doesn't try to sync a profile that can't."""
    return bool(mw.pm.sync_auth())


def _active_profile(p: dict) -> str | None:
    """Name of the open profile (same action as AnkiConnect), None on the profile screen."""
    return mw.pm.name if mw.col else None


def _profiles(p: dict) -> list[str]:
    """Every Anki profile (same action as AnkiConnect): owners to pick in Cartable's settings."""
    return list(mw.pm.profiles())


ACTIONS: dict[str, Callable[[dict], Any]] = {
    "getActiveProfile": _active_profile,
    "getProfiles": _profiles,
    "isSyncConfigured": _sync_configured,
    "modelNames": _model_names,
    "createModel": _create_model,
    "deckNames": _deck_names,
    "createDeck": _create_deck,
    "storeMediaFile": _store_media_file,
    "findNotes": _find_notes,
    "notesInfo": _notes_info,
    "updateNoteFields": _update_note_fields,
    "updateNoteModel": _update_note_model,
    "findCards": _find_cards,
    "addTags": _add_tags,
    "deleteNotes": _delete_notes,
    "deleteDecks": _delete_decks,
    "addNote": _add_note,
    "sync": _sync,
}


# --- HTTP server ----------------------------------------------------------------


class Bridge:
    def __init__(self) -> None:
        self.key = secrets.token_urlsafe(24)
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        key = self.key

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    if not secrets.compare_digest(str(body.get("key", "")), key):
                        raise BridgeError("invalid key")
                    action, params = body.get("action"), body.get("params", {})
                    if action == "version":
                        reply = {"result": VERSION, "error": None}
                    elif action in ACTIONS:
                        reply = {"result": _on_main(lambda: ACTIONS[action](params)), "error": None}
                    else:
                        raise BridgeError(f"unsupported action: {action}")
                except Exception as e:
                    reply = {"result": None, "error": str(e)}
                out = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args) -> None:
                pass

        # Port 0: the system picks a free port; only this machine can connect.
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, name="cartable-bridge", daemon=True).start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
