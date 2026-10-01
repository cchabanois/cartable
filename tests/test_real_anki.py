"""Against Anki's real engine (the `anki` package from PyPI, no Anki window).

- The add-on's bridge (anki_addon/bridge.py) on a real collection, driven by the
  Cartable server itself: what runs for add-on users.
- Importing the exported .apkg, twice.

Skipped when `anki` isn't installed: `pip install -r requirements-anki.txt`.
"""

import importlib.util
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

anki_collection = pytest.importorskip("anki.collection")
from anki.import_export_pb2 import ImportAnkiPackageUpdateCondition  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import tts  # noqa: E402
from app.main import app  # noqa: E402

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8cfc0f00f00050201fe5b1ad9e70000000049454e44ae426082"
)
VOICE = "es-ES-ElviraNeural"
# Anki's default when importing: update a note if the imported one is newer
IF_NEWER = ImportAnkiPackageUpdateCondition.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_IF_NEWER


async def fake_synthesize(text, voice, path):
    path.write_bytes(b"ID3 fake mp3 " + text.encode())


class FakeMainWindow:
    """What bridge.py uses from aqt.mw, around a real collection. run_on_main runs
    tasks one at a time on a single thread, like Anki's main thread."""

    def __init__(self, col, profile: str, profiles: list[str]) -> None:
        self.col = col
        self.main = ThreadPoolExecutor(max_workers=1, thread_name_prefix="anki-main")
        self.taskman = types.SimpleNamespace(run_on_main=self.main.submit)
        self.pm = types.SimpleNamespace(name=profile, profiles=lambda: profiles, sync_auth=lambda: None)
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def on_sync_button_clicked(self) -> None:
        raise AssertionError("no sync without an AnkiWeb login")


def load_bridge(mw):
    """Import anki_addon/bridge.py with aqt replaced by the fake main window."""
    aqt = types.ModuleType("aqt")
    aqt.mw = mw
    qt = types.ModuleType("aqt.qt")
    qt.QTimer = types.SimpleNamespace(singleShot=lambda ms, fn: threading.Timer(ms / 1000, fn).start())
    sys.modules.update({"aqt": aqt, "aqt.qt": qt})
    spec = importlib.util.spec_from_file_location("cartable_bridge", Path("anki_addon/bridge.py"))
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


@pytest.fixture
def col(tmp_path):
    (tmp_path / "anki").mkdir()
    collection = anki_collection.Collection(str(tmp_path / "anki" / "collection.anki2"))
    yield collection
    collection.close()


@pytest.fixture
def cartable(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTABLE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CARTABLE_LLM", "fake")
    monkeypatch.setattr(tts, "_synthesize", fake_synthesize)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def bridged(col, cartable, monkeypatch):
    """Cartable → the add-on's bridge → the real collection, with "Léa" open."""
    mw = FakeMainWindow(col, "Léa", ["Léa", "Paul"])
    bridge = load_bridge(mw).Bridge()
    bridge.start()
    monkeypatch.setenv("CARTABLE_ANKICONNECT_URL", bridge.url)
    monkeypatch.setenv("CARTABLE_ANKICONNECT_KEY", bridge.key)
    yield cartable, mw
    bridge.stop()
    mw.main.shutdown()
    for name in ("aqt", "aqt.qt"):
        sys.modules.pop(name, None)


def extract(client) -> dict:
    res = client.post(
        "/api/extract",
        files=[("images", ("page-1.png", PNG, "image/png"))],
        data={"prompt": "FR → ES", "voice": VOICE},
    )
    assert res.status_code == 201, res.text
    return res.json()


def fields(col, note_id) -> dict:
    return dict(col.get_note(note_id).items())


def test_bridge_on_a_real_collection(bridged, col):
    client, mw = bridged
    # The add-on knows the profile isn't logged in to AnkiWeb
    assert client.get("/api/anki/status").json() == {"available": True, "version": 6, "profile": "Léa", "sync": False}

    lesson = extract(client)
    assert lesson["owner"] == "Léa"  # the bridge reported the open profile
    body = {"deck": lesson["deck"], "cards": lesson["cards"], "voice": VOICE, "lesson_id": lesson["id"]}
    res = client.post("/api/anki/send", json=body).json()
    cards = [c for c in lesson["cards"] if c["front"].strip() and c["back"].strip()]
    assert (res["added"], res["updated"], res["audio_failures"]) == (len(cards), 0, 0)
    # Not logged in to AnkiWeb: no sync tried (no warning at each send, no login dialog)
    assert (res["synced"], res["sync_error"], res["sync_skipped"]) == (False, None, True)

    # What landed in the collection
    (note_type,) = [m for m in col.models.all_names_and_ids() if m.name.startswith("Cartable")]
    model = col.models.get(note_type.id)
    assert [f["name"] for f in model["flds"]] == ["Front", "Back", "Info", "Audio"]
    note_ids = col.find_notes(f'"note:{note_type.name}"')
    assert len(note_ids) == len(cards)
    decks = {col.decks.name(col.get_note(n).cards()[0].did) for n in note_ids}
    assert decks == {f"{lesson['deck']}::{c['subdeck']}" if c["subdeck"] else lesson["deck"] for c in cards}
    mother = next(n for n in note_ids if fields(col, n)["Front"] == "la mère")
    audio = fields(col, mother)["Audio"]
    assert audio == f"[sound:{tts.filename('la madre', VOICE)}]"
    assert (Path(col.media.dir()) / tts.filename("la madre", VOICE)).read_bytes() == b"ID3 fake mp3 la madre"

    # Sent again, corrected: the note is updated, not duplicated
    corrected = [{**c, "back": "la mamá"} if c["front"] == "la mère" else c for c in lesson["cards"]]
    res = client.post("/api/anki/send", json={**body, "cards": corrected}).json()
    assert (res["added"], res["updated"]) == (0, len(cards))
    assert len(col.find_notes(f'"note:{note_type.name}"')) == len(cards)
    assert fields(col, mother)["Back"] == "la mamá"

    # Anki's profiles, for the owner picker in the settings
    assert client.post("/api/admin/password", json={"new": "secret"}).status_code == 204
    listing = client.get("/api/admin/lessons", headers={"X-Admin-Password": "secret"}).json()
    assert listing["profiles"] == ["Léa", "Paul"]

    # Profile screen (no collection open): refused with a clear error
    mw.col = None
    assert client.get("/api/anki/status").json()["profile"] is None
    res = client.post("/api/anki/send", json=body)
    assert res.json()["detail"]["code"] == "anki.no_profile"


def test_apkg_import_then_reimport_updates(cartable, col, tmp_path):
    lesson = extract(cartable)
    body = {"deck": lesson["deck"], "cards": lesson["cards"], "voice": VOICE, "lesson_id": lesson["id"]}

    def import_apkg(cards):
        res = cartable.post("/api/export", json={**body, "cards": cards})
        assert res.status_code == 200
        path = tmp_path / "lesson.apkg"
        path.write_bytes(res.content)
        request = anki_collection.ImportAnkiPackageRequest(
            package_path=str(path),
            options=anki_collection.ImportAnkiPackageOptions(
                with_scheduling=False,
                merge_notetypes=True,
                update_notes=IF_NEWER,
                update_notetypes=IF_NEWER,
            ),
        )
        return col.import_anki_package(request).log

    cards = [c for c in lesson["cards"] if c["front"].strip() and c["back"].strip()]
    assert len(import_apkg(lesson["cards"]).new) == len(cards)
    assert col.note_count() == len(cards)
    mother = col.find_notes('"Front:la mère"')
    assert len(mother) == 1
    assert (Path(col.media.dir()) / tts.filename("la madre", VOICE)).is_file()  # sounds imported

    # The corrected lesson, imported again: same notes (stable GUIDs), new back
    corrected = [{**c, "back": "la mamá"} if c["front"] == "la mère" else c for c in lesson["cards"]]
    time.sleep(1.1)  # notes are dated to the second: a later export is newer
    log = import_apkg(corrected)
    assert (len(log.new), len(log.updated)) == (0, len(cards))
    assert col.note_count() == len(cards)
    assert fields(col, mother[0])["Back"] == "la mamá"


def test_diagram_lesson_on_a_real_collection(bridged, col):
    import io

    from PIL import Image

    client, _ = bridged
    out = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(out, "JPEG")
    res = client.post(
        "/api/extract", files=[("images", ("p.jpg", out.getvalue(), "image/jpeg"))], data={"prompt": "Le schéma"}
    )
    lesson = res.json()
    body = {"deck": lesson["deck"], "cards": lesson["cards"], "lesson_id": lesson["id"]}
    assert client.post("/api/anki/send", json=body).json()["added"] == 3

    (note_type,) = [m for m in col.models.all_names_and_ids() if m.name.startswith("Cartable légendes")]
    note_ids = col.find_notes(f'"note:{note_type.name}"')
    first = fields(col, min(note_ids))
    image = first["Image"].split('"')[1]
    assert first["Id"] == f"{lesson['id']}:1:1"
    assert (Path(col.media.dir()) / image).stat().st_size > 1000  # the diagram, shared by its 3 cards
    assert {fields(col, n)["Image"] for n in note_ids} == {first["Image"]}

    # A mask moved in the review: the same notes, updated with new images
    lesson["cards"][0]["mask"]["box"] = [0.15, 0.1, 0.4, 0.2]
    res = client.post("/api/anki/send", json={**body, "cards": lesson["cards"]}).json()
    assert (res["added"], res["updated"]) == (0, 3)
    assert col.find_notes(f'"note:{note_type.name}"') == note_ids
    moved = fields(col, min(note_ids))
    assert moved["Image"] == first["Image"]  # one image per diagram
    assert moved["Masks"] != first["Masks"]


def test_picture_lesson_on_a_real_collection(bridged, col, monkeypatch):
    import io

    from PIL import Image

    from app import pictures

    async def draw(s, subject):
        out = io.BytesIO()
        Image.new("RGB", (600, 600), "orange").save(out, "PNG")
        return out.getvalue()

    monkeypatch.setattr(pictures, "draw", draw)
    client, _ = bridged
    lesson = client.post("/api/extract", data={"prompt": "recto : image du mot"}).json()
    cards = client.post(f"/api/lessons/{lesson['id']}/pictures").json()["lesson"]["cards"]
    body = {"deck": "Anglais", "cards": cards, "lesson_id": lesson["id"]}
    assert client.post("/api/anki/send", json=body).json()["added"] == 4

    (note_type,) = [m for m in col.models.all_names_and_ids() if m.name.startswith("Cartable image")]
    note_ids = sorted(col.find_notes(f'"note:{note_type.name}"'))
    assert len(note_ids) == 3  # the apple, the dog, the umbrella; "tomorrow" is a text card
    first = fields(col, note_ids[0])
    assert first["Id"] == cards[0]["id"] and first["Front"] == "Comment dit-on en anglais ?"
    assert (Path(col.media.dir()) / cards[0]["picture"]).is_file()

    # Same front on every picture card: sent again, updated through the card ids, not duplicated
    res = client.post("/api/anki/send", json=body).json()
    assert (res["added"], res["updated"]) == (0, 4)
    assert sorted(col.find_notes(f'"note:{note_type.name}"')) == note_ids


def test_cloze_lesson_on_a_real_collection(bridged, col, tmp_path):
    client, _ = bridged
    lesson = client.post("/api/extract", data={"prompt": "Texte à trous"}).json()
    body = {"deck": lesson["deck"], "cards": lesson["cards"], "lesson_id": lesson["id"]}
    assert client.post("/api/anki/send", json=body).json()["added"] == 3

    (note_type,) = [m for m in col.models.all_names_and_ids() if m.name == "Cartable texte à trous"]
    assert col.models.get(note_type.id)["type"] == 1  # a real cloze note type
    note_ids = sorted(col.find_notes(f'"note:{note_type.name}"'))
    # Anki makes one card per gap number: c1 and c2, c1 and c2, c1 (twice)
    assert [len(col.get_note(n).cards()) for n in note_ids] == [2, 2, 1]
    question = col.get_note(note_ids[0]).cards()[0].question()
    assert ">[...]</span> avec la prise de" in question and ">la Bastille</span>" in question  # c1 hidden

    # Imported as a package too: the same note type, the same cards
    res = client.post("/api/export", json=body)
    path = tmp_path / "cloze.apkg"
    path.write_bytes(res.content)
    other = anki_collection.Collection(str(tmp_path / "anki" / "other.anki2"))
    try:
        other.import_anki_package(
            anki_collection.ImportAnkiPackageRequest(
                package_path=str(path),
                options=anki_collection.ImportAnkiPackageOptions(
                    with_scheduling=False, merge_notetypes=True, update_notes=IF_NEWER, update_notetypes=IF_NEWER
                ),
            )
        )
        assert other.card_count() == 5
    finally:
        other.close()
