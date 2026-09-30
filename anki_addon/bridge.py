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
    for name in p["inOrderFields"]:
        mm.add_field(model, mm.new_field(name))
    for t in p["cardTemplates"]:
        template = mm.new_template(t["Name"])
        template["qfmt"], template["afmt"] = t["Front"], t["Back"]
        mm.add_template(model, template)
    model["css"] = p.get("css", model["css"])
    return mm.add(model).id


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
        result.append({"noteId": note.id, "fields": fields, "tags": note.tags})
    return result


def _update_note_fields(p: dict) -> None:
    note = _col().get_note(p["note"]["id"])
    for name, value in p["note"]["fields"].items():
        if name in note:
            note[name] = value
    _col().update_note(note)
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


def _sync(p: dict) -> None:
    # Never open the login dialog in the middle of a send: report it instead
    # (Cartable translates this into "Anki is not logged in to AnkiWeb").
    if not mw.pm.sync_auth():
        raise BridgeError("sync: auth not configured")
    mw.on_sync_button_clicked()


def _active_profile(p: dict) -> str | None:
    """Name of the open profile (same action as AnkiConnect), None on the profile screen."""
    return mw.pm.name if mw.col else None


ACTIONS: dict[str, Callable[[dict], Any]] = {
    "getActiveProfile": _active_profile,
    "modelNames": _model_names,
    "createModel": _create_model,
    "createDeck": _create_deck,
    "storeMediaFile": _store_media_file,
    "findNotes": _find_notes,
    "notesInfo": _notes_info,
    "updateNoteFields": _update_note_fields,
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
