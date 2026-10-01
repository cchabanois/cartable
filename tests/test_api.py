import base64
import json
import sqlite3
import sys
import zipfile

import httpx
import pytest
from conftest import ADMIN, synthesized
from fastapi.testclient import TestClient

from app import ankiconnect, i18n, lessons, prompts, settings, storage, tts
from app.main import app


def test_cartable_prompts(client):
    """Cartable's prompts: in the page's language, read-only, before the user's."""
    fr = client.get("/api/prompts", headers={"X-Cartable-Lang": "fr-FR"}).json()
    assert [p["id"] for p in fr] == [f"cartable:{k}" for k in prompts.BUILTIN]
    assert all(p["builtin"] for p in fr) and fr[3]["name"] == "Schéma : une carte par légende"
    en = client.get("/api/prompts", headers={"X-Cartable-Lang": "en"}).json()
    assert en[0]["name"] == "Vocabulary of a language"  # the same prompts, in English
    de = client.get("/api/prompts", headers={"X-Cartable-Lang": "de"}).json()
    assert de[0]["name"] == "Vocabulary of a language"  # no German file: English

    for method in ("PUT", "DELETE"):
        r = client.request(method, "/api/prompts/cartable:questions", json={"name": "x", "text": "y"})
        assert (r.status_code, r.json()["detail"]["code"]) == (403, "prompt.builtin")


def test_duplicate_a_prompt_to_adapt_it(client):
    copy = client.post("/api/prompts/cartable:vocabulary/duplicate", headers={"X-Cartable-Lang": "fr"}).json()
    assert (copy["name"], copy["builtin"]) == ("Vocabulaire d'une langue (copie)", False)
    changed = client.put(f"/api/prompts/{copy['id']}", json={**copy, "text": "FR → ES", "voice": "es-ES-ElviraNeural"})
    assert changed.json()["voice"] == "es-ES-ElviraNeural"
    again = client.post(f"/api/prompts/{copy['id']}/duplicate", headers={"X-Cartable-Lang": "fr"}).json()
    assert (again["name"], again["text"]) == ("Vocabulaire d'une langue (copie) (copie)", "FR → ES")
    assert client.post("/api/prompts/cartable:nope/duplicate").status_code == 404
    names = [p["name"] for p in client.get("/api/prompts", headers={"X-Cartable-Lang": "fr"}).json()]
    assert names[-2:] == ["Vocabulaire d'une langue (copie)", "Vocabulaire d'une langue (copie) (copie)"]


def test_cartable_prompt_used(client):
    data = {"prompt": "words: le chat", "prompt_id": "cartable:wordlist"}
    client.post("/api/extract", data=data)
    wordlist = next(p for p in client.get("/api/prompts").json() if p["id"] == "cartable:wordlist")
    assert wordlist["used_at"] is not None


def test_old_prompts_file_converted(client, tmp_path):
    """Before, the file was a list seeded with the default prompts."""
    old = [
        {"id": 1, "name": "Vocabulaire FR → ES", "text": "Crée des cartes…", "voice": "es-ES-ElviraNeural"},
        {"id": 3, "name": "Questions / réponses", "text": next(iter(sorted(prompts.REPLACED)))},
        {"id": 6, "name": "Formules de maths", "text": "Les formules de 5e"},
    ]
    path = tmp_path / "data" / "prompts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old), encoding="utf-8")
    user = [p for p in client.get("/api/prompts").json() if not p["builtin"]]
    assert [(p["id"], p["name"]) for p in user] == [(1, "Vocabulaire FR → ES"), (6, "Formules de maths")]
    client.post("/api/prompts", json={"name": "Anglais", "text": "FR → EN"})  # written in the new format
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [p["id"] for p in saved["user"]] == [1, 6, 7]


def test_prompt_crud(client):
    created = client.post("/api/prompts", json={"name": "Anglais", "text": "FR → EN"}).json()
    assert created["id"]

    updated = client.put(
        f"/api/prompts/{created['id']}",
        json={"name": "Anglais", "text": "FR → EN, vocabulaire", "voice": "en-GB-SoniaNeural"},
    ).json()
    assert updated["voice"] == "en-GB-SoniaNeural"

    assert client.delete(f"/api/prompts/{created['id']}").status_code == 204
    assert client.delete(f"/api/prompts/{created['id']}").status_code == 404
    assert client.put("/api/prompts/999", json={"name": "x", "text": "y"}).status_code == 404


def test_extract_fake(client):
    files = [("images", ("p1.jpg", b"\xff\xd8fake", "image/jpeg"))] * 2
    res = client.post("/api/extract", files=files, data={"prompt": "FR → ES", "voice": "es-ES-ElviraNeural"})
    assert res.status_code == 201
    lesson = res.json()
    assert lesson["deck"].startswith("Espagnol")
    assert "2 photo(s)" in lesson["cards"][-1]["front"]
    assert lesson["photo_count"] == 2
    assert lesson["voice"] == "es-ES-ElviraNeural"
    assert lesson["prompt"] == "FR → ES"


def test_extract_rejects_non_image(client):
    files = [("images", ("a.pdf", b"%PDF", "application/pdf"))]
    res = client.post("/api/extract", files=files, data={"prompt": "x"})
    assert res.status_code == 400


def _notes(apkg_bytes, tmp_path):
    path = tmp_path / "out.apkg"
    path.write_bytes(apkg_bytes)
    with zipfile.ZipFile(path) as z:
        z.extract("collection.anki2", tmp_path)
        media = json.loads(z.read("media"))
    conn = sqlite3.connect(tmp_path / "collection.anki2")
    notes = conn.execute("SELECT guid, flds FROM notes").fetchall()
    models = json.loads(conn.execute("SELECT models FROM col").fetchone()[0])
    decks = json.loads(conn.execute("SELECT decks FROM col").fetchone()[0])
    conn.close()
    return notes, models, decks, media


EXPORT = {
    "deck": "Espagnol::Leçon 5",
    "cards": [
        {"front": "la mère", "back": "la madre", "info": "nom féminin", "subdeck": "Vocabulaire"},
        {"front": "<b>", "back": "¿Cómo te llamas?", "subdeck": "Phrases"},
        {"front": "", "back": "ignorée : recto vide"},
    ],
    "voice": "es_ES",
    "reverse": True,
}


def test_export_apkg(client, tmp_path):
    res = client.post("/api/export", json=EXPORT)
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]

    notes, models, decks, _ = _notes(res.content, tmp_path)
    assert len(notes) == 2
    assert any("&lt;b&gt;" in flds for _, flds in notes)  # text is HTML-escaped
    (model,) = models.values()
    assert [t["name"] for t in model["tmpls"]] == ["Recto → Verso", "Verso → Recto"]
    assert "{{tts es_ES:Back}}" in model["tmpls"][0]["afmt"]
    deck_names = {d["name"] for d in decks.values()}
    assert {"Espagnol::Leçon 5::Vocabulaire", "Espagnol::Leçon 5::Phrases"} <= deck_names


def test_export_guid_stable(client, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, *_ = _notes(client.post("/api/export", json=EXPORT).content, tmp_path / "a")
    # Corrected back, same front and deck → same GUID, so Anki updates the note.
    corrected = {**EXPORT, "cards": [{**EXPORT["cards"][0], "back": "la mamá"}]}
    b, *_ = _notes(client.post("/api/export", json=corrected).content, tmp_path / "b")
    assert b[0][0] in {guid for guid, _ in a}


def test_export_empty(client):
    res = client.post("/api/export", json={"deck": "X", "cards": [{"front": "", "back": ""}]})
    assert res.status_code == 400


AUDIO_EXPORT = {
    "deck": "Espagnol::Leçon 5",
    "cards": [
        {"front": "la mère", "back": "la madre"},
        {"front": "maman", "back": "la madre"},  # same back → a single mp3
        {"front": "le père", "back": "ÉCHEC"},
    ],
    "voice": "es-ES-ElviraNeural",
}


def test_export_audio_edge_tts(client, tmp_path):
    res = client.post("/api/export", json=AUDIO_EXPORT)
    assert res.status_code == 200
    assert res.headers["X-Cartable-Audio-Failures"] == "1"
    assert synthesized == ["la madre"]

    notes, models, _, media = _notes(res.content, tmp_path)
    (model,) = models.values()
    assert [f["name"] for f in model["flds"]] == ["Front", "Back", "Info", "Audio"]
    assert "{{Audio}}" in model["tmpls"][0]["afmt"]
    sounds = [flds.split("\x1f")[3] for _, flds in notes]
    assert sounds.count(f"[sound:{tts.filename('la madre', 'es-ES-ElviraNeural')}]") == 2
    assert "" in sounds  # the failed card goes out without sound
    assert list(media.values()) == [tts.filename("la madre", "es-ES-ElviraNeural")]


def test_audio_in_the_lesson(client):
    lesson = _extract(client)
    export = {**AUDIO_EXPORT, "lesson_id": lesson["id"]}
    client.post("/api/export", json=export)
    client.post("/api/export", json=export)
    assert synthesized == ["la madre"]  # kept in the lesson folder, not synthesized again

    audio = lessons.audio_dir(lesson["id"])
    assert [p.name for p in audio.iterdir()] == [tts.filename("la madre", "es-ES-ElviraNeural")]
    assert tts.filename("la madre", "es-ES-ElviraNeural").startswith("la-madre-")

    # Back corrected: the new mp3 replaces the old one
    corrected = {**export, "cards": [{"front": "la mère", "back": "la mamá"}]}
    client.post("/api/export", json=corrected)
    assert [p.name for p in audio.iterdir()] == [tts.filename("la mamá", "es-ES-ElviraNeural")]


def test_preview_then_export_reuses_the_sound(client):
    lesson = _extract(client)
    params = {"text": "la madre", "voice": "es-ES-ElviraNeural", "lesson": lesson["id"]}
    assert client.get("/api/tts", params=params).status_code == 200
    client.post("/api/export", json={**AUDIO_EXPORT, "lesson_id": lesson["id"]})
    assert synthesized == ["la madre"]


def test_voice_preview(client):
    res = client.get("/api/tts", params={"text": "hola", "voice": "es-ES-ElviraNeural"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/mpeg"
    assert client.get("/api/tts", params={"text": "hola", "voice": "es_ES"}).status_code == 400


def _extract(client, n_photos=2):
    files = [("images", (f"p{i}.jpg", f"photo {i}".encode(), "image/jpeg")) for i in range(1, n_photos + 1)]
    return client.post("/api/extract", files=files, data={"prompt": "FR → ES"}).json()


def test_lesson_saved(client):
    lesson = _extract(client)
    (summary,) = client.get("/api/lessons").json()
    assert summary["id"] == lesson["id"]
    assert summary["card_count"] == len(lesson["cards"])
    assert summary["exported_at"] is None

    photo = client.get(f"/api/lessons/{lesson['id']}/photos/2")
    assert photo.status_code == 200
    assert photo.content == b"photo 2"
    assert client.get(f"/api/lessons/{lesson['id']}/photos/3").status_code == 404


def test_lesson_updated(client):
    lesson = _extract(client)
    edit = {"deck": "Espagnol::Leçon 6", "cards": [{"front": "le chat", "back": "el gato"}], "reverse": True}
    res = client.put(f"/api/lessons/{lesson['id']}", json=edit)
    assert res.status_code == 200

    reloaded = client.get(f"/api/lessons/{lesson['id']}").json()
    assert reloaded["deck"] == "Espagnol::Leçon 6"
    assert [c["back"] for c in reloaded["cards"]] == ["el gato"]
    assert reloaded["reverse"] is True
    assert reloaded["prompt"] == "FR → ES"  # unchanged
    assert client.put("/api/lessons/999", json=edit).status_code == 404


def test_export_marks_the_lesson(client):
    lesson = _extract(client)
    export = {"deck": "Corrigé", "cards": [{"front": "la mère", "back": "la madre"}], "lesson_id": lesson["id"]}
    assert client.post("/api/export", json=export).status_code == 200

    reloaded = client.get(f"/api/lessons/{lesson['id']}").json()
    assert reloaded["deck"] == "Corrigé"
    assert reloaded["exported_at"] is not None


def test_lesson_deleted(client):
    lesson = _extract(client)
    photos = lessons.folder(lesson["id"])
    assert photos.is_dir()

    assert client.delete(f"/api/lessons/{lesson['id']}").status_code == 204
    assert not photos.exists()
    assert client.get(f"/api/lessons/{lesson['id']}").status_code == 404
    assert client.get("/api/lessons").json() == []


def test_failed_extraction_creates_no_lesson(client, monkeypatch):
    monkeypatch.setenv("CARTABLE_LLM", "inconnu")
    files = [("images", ("p.jpg", b"x", "image/jpeg"))]
    assert client.post("/api/extract", files=files, data={"prompt": "x"}).status_code == 502
    assert client.get("/api/lessons").json() == []


def test_prompt_used(client):
    prompt = client.get("/api/prompts").json()[1]
    assert prompt["used_at"] is None

    files = [("images", ("p.jpg", b"x", "image/jpeg"))]
    data = {"prompt": prompt["text"], "prompt_id": str(prompt["id"])}
    assert client.post("/api/extract", files=files, data=data).status_code == 201

    used = {c["id"]: c["used_at"] for c in client.get("/api/prompts").json()}
    assert used[prompt["id"]] is not None
    assert sum(v is not None for v in used.values()) == 1


def test_one_folder_per_lesson(client, tmp_path):
    lesson = _extract(client)
    folder = tmp_path / "data" / "lessons" / lesson["id"]
    assert lesson["id"].endswith("-espagnol-lecon-5-la-famille")
    assert sorted(p.name for p in folder.iterdir()) == ["lesson.json", "page-1.jpg", "page-2.jpg"]

    saved = json.loads((folder / "lesson.json").read_text(encoding="utf-8"))
    assert saved["deck"] == lesson["deck"]
    assert saved["prompt"] == "FR → ES"
    assert "id" not in saved  # the folder name is the id

    # Same deck the same day: a second folder, not an overwrite
    assert _extract(client)["id"] == lesson["id"] + "-2"


def test_invalid_lesson_id(client):
    for bad in ["..", "../data", "A-majuscule", "-tiret"]:
        assert client.get(f"/api/lessons/{bad}").status_code == 404
    assert lessons.folder("../prompts.json") is None


def test_prompts_in_a_file(client, tmp_path):
    client.post("/api/prompts", json={"name": "Anglais", "text": "FR → EN"})
    saved = json.loads((tmp_path / "data" / "prompts.json").read_text(encoding="utf-8"))
    assert [(p["id"], p["name"]) for p in saved["user"]] == [(1, "Anglais")]  # Cartable's aren't copied


def test_slugify():
    assert storage.slugify("Espagnol::Leçon 5 - La famille") == "espagnol-lecon-5-la-famille"
    assert storage.slugify("¿Cómo te llamas?") == "como-te-llamas"
    assert storage.slugify("日本語") == ""


# --- Admin -------------------------------------------------------------------


def test_admin_password_protected(admin):
    assert admin.get("/api/admin").json()["password_set"] is True
    assert admin.get("/api/admin/settings").status_code == 401
    assert admin.get("/api/admin/settings", headers={"X-Admin-Password": "faux"}).status_code == 401
    assert admin.get("/api/admin/settings", headers=ADMIN).status_code == 200
    # Once set, changing it requires the current password
    assert admin.post("/api/admin/password", json={"new": "pirate"}).status_code == 401
    assert admin.post("/api/admin/password", json={"current": "secret", "new": "nouveau"}).status_code == 204
    assert admin.get("/api/admin/settings", headers={"X-Admin-Password": "nouveau"}).status_code == 200


def test_admin_keys_never_sent_back(admin, monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFROMENV0001")
    res = admin.put(
        "/api/admin/settings",
        headers=ADMIN,
        json={"llm": "anthropic", "model": "claude-sonnet-5", "anthropic_api_key": " sk-ant-SECRET9876 "},
    )
    assert res.status_code == 200
    view = res.json()
    assert view["anthropic_api_key"] == "•••• 9876"
    assert view["gemini_api_key"] == "•••• 0001"  # default from the environment
    assert "SECRET" not in res.text and "FROMENV" not in res.text

    s = settings.current()
    assert (s.llm, s.model, s.anthropic_api_key) == ("anthropic", "claude-sonnet-5", "sk-ant-SECRET9876")
    if sys.platform != "win32":  # Windows: no Unix permissions (the user profile protects the file)
        assert (tmp_path / "data" / "settings.json").stat().st_mode & 0o777 == 0o600

    # Omitted key: unchanged; empty string: cleared
    admin.put("/api/admin/settings", headers=ADMIN, json={"model": ""})
    assert settings.current().anthropic_api_key == "sk-ant-SECRET9876"
    admin.put("/api/admin/settings", headers=ADMIN, json={"anthropic_api_key": ""})
    assert settings.current().anthropic_api_key == ""


def test_admin_invalid_values(admin):
    assert admin.put("/api/admin/settings", headers=ADMIN, json={"llm": "skynet"}).status_code == 422
    assert admin.put("/api/admin/settings", headers=ADMIN, json={"tts_rate": "vite"}).status_code == 422
    assert admin.post("/api/admin/password", json={"current": "secret", "new": "abc"}).status_code == 422


def test_admin_connection_test(admin):
    res = admin.post("/api/admin/test", headers=ADMIN)
    assert res.status_code == 200
    assert (res.json()["vision"], res.json()["json"]) == (True, True)


def test_extraction_follows_settings(admin, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    admin.put("/api/admin/settings", headers=ADMIN, json={"llm": "gemini"})
    files = [("images", ("p.jpg", b"x", "image/jpeg"))]
    res = admin.post("/api/extract", files=files, data={"prompt": "x"})
    assert res.status_code == 502
    assert res.json()["detail"] == {"code": "llm.missing_key", "params": {"provider": "Gemini"}}


# --- AI correction -------------------------------------------------------------


def test_revision_by_instruction(client):
    lesson = _extract(client)
    n = len(lesson["cards"])
    body = {
        "deck": lesson["deck"],
        "cards": lesson["cards"],
        "voice": "es-ES-ElviraNeural",
        "instruction": "supprime la dernière carte",
    }
    res = client.post(f"/api/lessons/{lesson['id']}/revise", json=body)
    assert res.status_code == 200
    assert res.json()["summary"]
    assert len(res.json()["lesson"]["cards"]) == n - 1

    # Saved on the server, with the voice sent by the page
    saved = client.get(f"/api/lessons/{lesson['id']}").json()
    assert len(saved["cards"]) == n - 1
    assert saved["voice"] == "es-ES-ElviraNeural"

    # Works on the cards sent (possibly unsaved edits), not the stored ones
    body = {"deck": "D", "cards": [{"front": "le chat", "back": "el gato"}], "instruction": "ajoute les couleurs"}
    cards = client.post(f"/api/lessons/{lesson['id']}/revise", json=body).json()["lesson"]["cards"]
    assert [c["front"] for c in cards] == ["le chat", "(demo addition)"]


def test_revision_errors(client, monkeypatch):
    body = {"deck": "D", "cards": [], "instruction": "x"}
    assert client.post("/api/lessons/unknown/revise", json=body).status_code == 404
    lesson = _extract(client)
    assert client.post(f"/api/lessons/{lesson['id']}/revise", json={**body, "instruction": ""}).status_code == 422
    monkeypatch.setenv("CARTABLE_LLM", "inconnu")
    assert client.post(f"/api/lessons/{lesson['id']}/revise", json=body).status_code == 502


# --- Direct send to Anki (AnkiConnect) -------------------------------------------


class FakeAnki:
    """In-memory AnkiConnect, enough for Cartable's calls."""

    def __init__(self, fail_sync=False, profile="Léa"):
        self.models, self.decks, self.media, self.notes = {}, {}, {}, {}
        self.profile = profile
        self.calls, self.keys, self.fail_sync = [], [], fail_sync

    def handle(self, request):
        body = json.loads(request.content)
        action, p = body["action"], body["params"]
        self.calls.append(action)
        self.keys.append(body.get("key"))
        result, error = None, None
        if action == "version":
            result = 6
        elif action == "getActiveProfile":
            result = self.profile
        elif action == "getProfiles":
            result = ["Léa", "Paul"]
        elif action == "modelNames":
            result = list(self.models)
        elif action == "createModel":
            self.models[p["modelName"]] = p
        elif action == "createDeck":
            result = self.decks.setdefault(p["deck"], 1000 + len(self.decks))
        elif action == "storeMediaFile":
            self.media[p["filename"]] = base64.b64decode(p["data"])
        elif action == "findNotes":
            result = [
                i
                for i, n in self.notes.items()
                if p["query"] == f'"note:{n["modelName"]}" did:{self.decks[n["deckName"]]}'
            ]
        elif action == "notesInfo":
            result = [
                {"noteId": i, "fields": {k: {"value": v} for k, v in self.notes[i]["fields"].items()}}
                for i in p["notes"]
            ]
        elif action == "updateNoteFields":
            self.notes[p["note"]["id"]]["fields"] = p["note"]["fields"]
        elif action == "addNote":
            result = len(self.notes) + 1
            self.notes[result] = p["note"]
        elif action == "sync":
            error = "AnkiWeb: login required" if self.fail_sync else None
        return httpx.Response(200, json={"result": result, "error": error})


@pytest.fixture
def anki(client, monkeypatch):
    fake = FakeAnki()
    monkeypatch.setattr(ankiconnect, "_transport", httpx.MockTransport(fake.handle))
    return fake


SEND = {
    "deck": "Espagnol::Leçon 5",
    "cards": [
        {"front": "la mère", "back": "la madre", "subdeck": "Vocabulaire", "tags": ["famille proche"]},
        {"front": "<b>", "back": "el padre"},
    ],
    "voice": "es-ES-ElviraNeural",
}


def test_anki_unavailable(client, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ankiconnect, "_transport", httpx.MockTransport(refuse))
    status = client.get("/api/anki/status").json()
    assert status == {"available": False, "error": {"code": "anki.unreachable", "params": {}}}
    assert client.post("/api/anki/send", json=SEND).status_code == 502


def test_direct_send_to_anki(anki, client):
    # AnkiConnect doesn't say whether the profile is logged in to AnkiWeb: unknown
    assert client.get("/api/anki/status").json() == {"available": True, "version": 6, "profile": "Léa", "sync": None}
    lesson = _extract(client)
    res = client.post("/api/anki/send", json={**SEND, "lesson_id": lesson["id"]})
    assert res.status_code == 200
    assert res.json() == {
        "added": 2,
        "updated": 0,
        "synced": True,
        "sync_error": None,
        "sync_skipped": False,
        "audio_failures": 0,
    }

    (model,) = anki.models.values()
    assert model["inOrderFields"] == ["Front", "Back", "Info", "Audio"]
    assert set(anki.decks) == {"Espagnol::Leçon 5::Vocabulaire", "Espagnol::Leçon 5"}
    mother = anki.notes[1]
    assert mother["fields"]["Audio"] == f"[sound:{tts.filename('la madre', 'es-ES-ElviraNeural')}]"
    assert mother["tags"] == ["famille_proche"]
    assert anki.notes[2]["fields"]["Front"] == "&lt;b&gt;"
    assert set(anki.media) == {
        tts.filename("la madre", "es-ES-ElviraNeural"),
        tts.filename("el padre", "es-ES-ElviraNeural"),
    }
    assert anki.calls[-1] == "sync"
    assert client.get(f"/api/lessons/{lesson['id']}").json()["exported_at"] is not None

    # Sent again after a correction: notes are updated, not duplicated
    corrected = {**SEND, "cards": [{**SEND["cards"][0], "back": "la mamá"}, SEND["cards"][1]]}
    res = client.post("/api/anki/send", json=corrected).json()
    assert (res["added"], res["updated"]) == (0, 2)
    assert len(anki.notes) == 2
    assert anki.notes[1]["fields"]["Back"] == "la mamá"
    assert "createModel" not in anki.calls[anki.calls.index("sync") :]  # note type reused


def test_send_without_sync_or_with_key(anki, client):
    admin_headers = {"X-Admin-Password": "secret"}
    client.post("/api/admin/password", json={"new": "secret"})
    client.put("/api/admin/settings", headers=admin_headers, json={"ankiconnect_key": "k3y", "anki_sync": False})
    res = client.post("/api/anki/send", json=SEND).json()
    assert res["synced"] is False and "sync" not in anki.calls
    assert set(anki.keys) == {"k3y"}


def test_sync_failure_not_blocking(client, monkeypatch):
    fake = FakeAnki(fail_sync=True)
    monkeypatch.setattr(ankiconnect, "_transport", httpx.MockTransport(fake.handle))
    res = client.post("/api/anki/send", json=SEND).json()
    assert res["added"] == 2 and res["synced"] is False
    assert res["sync_error"] == {
        "code": "anki.error",
        "params": {"action": "sync", "detail": "AnkiWeb: login required"},
    }


def test_anki_not_logged_in_to_ankiweb(client, monkeypatch):
    fake = FakeAnki()
    fake.sync_error = "sync: auth not configured"
    original = fake.handle

    def handle(request):
        if json.loads(request.content)["action"] == "sync":
            return httpx.Response(200, json={"result": None, "error": fake.sync_error})
        return original(request)

    monkeypatch.setattr(ankiconnect, "_transport", httpx.MockTransport(handle))
    res = client.post("/api/anki/send", json=SEND).json()
    assert res["added"] == 2
    assert res["sync_error"]["code"] == "anki.sync_not_logged_in"


def test_addon_mode(client, monkeypatch):
    """In the Anki add-on, the bridge address and key win over saved AnkiConnect settings."""
    client.post("/api/admin/password", json={"new": "secret"})
    client.put(
        "/api/admin/settings",
        headers={"X-Admin-Password": "secret"},
        json={"ankiconnect_url": "http://autre-pc:8765", "ankiconnect_key": "ancienne"},
    )
    assert settings.current().ankiconnect_url == "http://autre-pc:8765"

    monkeypatch.setenv("CARTABLE_EMBEDDED", "1")
    monkeypatch.setenv("CARTABLE_ANKICONNECT_URL", "http://127.0.0.1:40123")
    monkeypatch.setenv("CARTABLE_ANKICONNECT_KEY", "bridge-key")
    s = settings.current()
    assert (s.ankiconnect_url, s.ankiconnect_key) == ("http://127.0.0.1:40123", "bridge-key")
    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        assert local.get("/api/admin/settings").json()["embedded"] is True


def test_addon_settings_only_on_the_computer(client, monkeypatch):
    """Add-on: settings only from the computer itself, without a password; never from a phone."""
    monkeypatch.setenv("CARTABLE_EMBEDDED", "1")
    # From a phone on the Wi-Fi (TestClient's default address is not local)
    assert client.get("/api/admin").json() == {"password_set": False, "password_needed": False, "allowed": False}
    assert client.get("/api/admin/settings").status_code == 403
    assert client.get("/api/admin/settings").json()["detail"]["code"] == "admin.local_only"
    assert client.post("/api/admin/password", json={"new": "pirate"}).status_code == 403

    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        assert local.get("/api/admin").json()["allowed"] is True
        assert local.get("/api/admin/settings").status_code == 200  # no password needed
        assert local.put("/api/admin/settings", json={"tts_rate": "+0%"}).status_code == 200
        # Relayed by a proxy (e.g. tailscale serve): comes from 127.0.0.1 but isn't local
        relayed = local.get("/api/admin/settings", headers={"X-Forwarded-For": "100.64.0.7"})
        assert relayed.status_code == 403
    assert settings.current().tts_rate == "+0%"


def test_standalone_password_from_the_phone(admin):
    """Standalone: settings from any device of the network, with the password."""
    assert admin.get("/api/admin").json() == {"password_set": True, "password_needed": True, "allowed": True}
    assert admin.get("/api/admin/settings", headers=ADMIN).status_code == 200


def test_qr_code(client):
    res = client.get("/api/qr", params={"text": "http://192.168.1.20:8000/"})
    assert res.status_code == 200 and res.headers["content-type"] == "image/png"
    assert res.content.startswith(b"\x89PNG")


def test_lesson_owned_by_anki_profile(anki, client):
    lesson = _extract(client)
    assert (lesson["owner"], lesson["shared"]) == ("Léa", False)
    assert client.get("/api/lessons").json()[0]["owner"] == "Léa"


def test_lesson_without_anki_has_no_owner(client):
    assert _extract(client)["owner"] == ""


# --- Languages ---------------------------------------------------------------------


def _keys(d, prefix=""):
    keys = set()
    for k, v in d.items():
        if isinstance(v, dict):
            keys |= _keys(v, f"{prefix}{k}.")
        elif k != "defaultPrompts":
            keys.add(prefix + k)
    return keys


def test_all_languages_have_the_same_keys():
    reference = _keys(i18n.messages("en"))
    for lang in i18n.available():
        keys = _keys(i18n.messages(lang))
        assert keys == reference, f"{lang}: missing {sorted(reference - keys)}, extra {sorted(keys - reference)}"
        assert set(i18n.get(lang, "builtinPrompts")) == set(prompts.BUILTIN)


def test_all_error_codes_translated():
    """Every AppError code raised in the code has a message in English."""
    import re
    from pathlib import Path

    codes = set()
    for path in Path("app").glob("*.py"):
        codes |= set(
            re.findall(
                r'(?:AppError|ExtractionError|AnkiConnectError|PictureError)\(\s*"([a-z_]+\.[a-z_]+)"',
                path.read_text(encoding="utf-8"),
            )
        )
    codes |= set(ankiconnect.KNOWN_ERRORS.values())
    assert codes, "no error code found"
    missing = [c for c in sorted(codes) if i18n.get("en", f"errors.{c}") is None]
    assert missing == []


def test_language_choice(monkeypatch):
    assert i18n.resolve("fr-FR") == "fr"
    assert i18n.resolve("de-DE", "en-US") == "en"
    assert i18n.resolve("de") is None
    monkeypatch.setenv("CARTABLE_LANG", "fr_FR")  # Anki's language, set by the add-on
    assert i18n.anki_language() == "fr"
    monkeypatch.setenv("CARTABLE_LANG", "ja_JP")
    assert i18n.anki_language() == "en"  # no Japanese file
    monkeypatch.delenv("CARTABLE_LANG")
    assert i18n.anki_language() is None  # standalone: the browser decides


def test_lang_route(client, monkeypatch):
    assert client.get("/api/lang").json() == {
        "lang": None,
        "available": ["en", "fr"],
        "names": {"en": "English", "fr": "Français"},
    }
    monkeypatch.setenv("CARTABLE_LANG", "fr_FR")
    assert client.get("/api/lang").json()["lang"] == "fr"


def test_revision_summary_in_page_language(client):
    lesson = _extract(client)
    body = {"deck": "D", "cards": [{"front": "a", "back": "b"}], "instruction": "remove the last card"}
    res = client.post(f"/api/lessons/{lesson['id']}/revise", json=body, headers={"X-Cartable-Lang": "fr"})
    assert res.json()["summary"] == "Dernière carte supprimée (démo)."


# --- OpenAI-compatible services ------------------------------------------------------


class FakeModel:
    def __init__(self, id, modalities=None, parameters=None):
        self.id = id
        self.model_extra = {"architecture": {"input_modalities": modalities}} if modalities else {}
        if parameters is not None:
            self.model_extra["supported_parameters"] = parameters


def fake_openai(models, seen):
    class Models:
        def list(self):
            async def gen():
                for m in models:
                    yield m

            return gen()

    class Client:
        def __init__(self, base_url, api_key):
            seen.update(base_url=base_url, api_key=api_key)
            self.models = Models()

    return Client


def test_service_models(admin, monkeypatch):
    import openai

    seen = {}
    admin.put(
        "/api/admin/settings",
        headers=ADMIN,
        json={"llm": "openai", "openai_base_url": "https://openrouter.ai/api/v1", "openai_api_key": "sk-or-1"},
    )

    # OpenRouter-like: models describe their inputs → only those accepting images
    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        fake_openai(
            [
                FakeModel("openai/gpt-6-luna", ["text", "image"], ["tools", "structured_outputs"]),
                FakeModel("some/text-only", ["text"], ["structured_outputs"]),
                FakeModel("anthropic/claude-sonnet-5", ["image", "text"], ["structured_outputs"]),
                FakeModel("vision/no-json", ["image", "text"], ["tools", "response_format"]),
            ],
            seen,
        ),
    )
    res = admin.post("/api/admin/models", headers=ADMIN)
    assert res.json() == {"models": ["anthropic/claude-sonnet-5", "openai/gpt-6-luna"], "vision_only": True}
    assert seen == {"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-1"}

    # OpenAI-like: no description → every model
    monkeypatch.setattr(openai, "AsyncOpenAI", fake_openai([FakeModel("b"), FakeModel("a")], seen))
    assert admin.post("/api/admin/models", headers=ADMIN).json() == {"models": ["a", "b"], "vision_only": False}
    assert admin.post("/api/admin/models").status_code == 401


def test_openai_compatible_without_model(admin):
    admin.put("/api/admin/settings", headers=ADMIN, json={"llm": "openai", "model": ""})
    files = [("images", ("p.jpg", b"x", "image/jpeg"))]
    res = admin.post("/api/extract", files=files, data={"prompt": "x"})
    assert res.status_code == 502
    assert res.json()["detail"]["code"] == "llm.missing_model"


def test_openai_compatible_service_error(admin, monkeypatch):
    """The provider's own error code goes in the message, not in the HTTP status (regression)."""
    import httpx as _httpx
    import openai

    request = _httpx.Request("GET", "https://api.example.test/v1/models")

    class Failing:
        def __init__(self, base_url, api_key):
            self.models = self

        def list(self):
            async def gen():
                raise openai.InternalServerError("boom", response=_httpx.Response(500, request=request), body=None)
                yield

            return gen()

    admin.put(
        "/api/admin/settings", headers=ADMIN, json={"llm": "openai", "openai_base_url": "https://api.example.test/v1"}
    )
    monkeypatch.setattr(openai, "AsyncOpenAI", Failing)
    res = admin.post("/api/admin/models", headers=ADMIN)
    assert res.status_code == 502
    assert res.json()["detail"] == {
        "code": "llm.api_error",
        "params": {"provider": "api.example.test", "status": 500, "detail": "boom"},
    }

    admin.put("/api/admin/settings", headers=ADMIN, json={"openai_base_url": ""})
    assert admin.post("/api/admin/models", headers=ADMIN).json()["detail"]["code"] == "llm.missing_url"


def test_one_key_per_openai_compatible_service(admin, tmp_path):
    def put(body):
        return admin.put("/api/admin/settings", headers=ADMIN, json=body).json()

    put({"llm": "openai", "openai_base_url": "https://api.openai.com/v1", "openai_api_key": "sk-openai-1111"})
    view = put({"openai_base_url": "https://openrouter.ai/api/v1", "openai_api_key": "sk-or-2222"})

    # Each service keeps its own key; the page gets them masked, by service
    assert view["openai_keys"] == {
        "https://api.openai.com/v1": "•••• 1111",
        "https://openrouter.ai/api/v1": "•••• 2222",
    }
    assert view["openai_api_key"] == "•••• 2222"  # the current service's
    assert "sk-" not in json.dumps(view)
    assert settings.current().openai_key() == "sk-or-2222"

    put({"openai_base_url": "https://api.openai.com/v1/"})  # switching back: its key comes back
    assert settings.current().openai_key() == "sk-openai-1111"

    put({"openai_base_url": "https://api.mistral.ai/v1"})  # a service without a key
    assert settings.current().openai_key() == ""

    put({"openai_base_url": "https://openrouter.ai/api/v1", "openai_api_key": ""})  # delete OpenRouter's only
    assert set(settings.current().openai_keys) == {"https://api.openai.com/v1"}


def test_legacy_openai_key_moved_to_its_service(client, tmp_path):
    """A key saved before keys per service belongs to the service saved with it only."""
    path = tmp_path / "data" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"llm": "openai", "openai_base_url": "https://api.openai.com/v1", "openai_api_key": "sk-old-9999"})
    )
    assert settings.current().openai_key() == "sk-old-9999"
    settings.save({"openai_base_url": "https://openrouter.ai/api/v1"})
    assert settings.current().openai_key() == ""  # not reused for OpenRouter
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "openai_api_key" not in saved  # migrated on save
    assert saved["openai_keys"] == {"https://api.openai.com/v1": "sk-old-9999"}


class FakeMistralModel:
    def __init__(self, id, vision):
        self.id = id
        self.model_extra = {"capabilities": {"completion_chat": True, "vision": vision}}


def test_mistral_models(admin, monkeypatch):
    import openai

    admin.put(
        "/api/admin/settings", headers=ADMIN, json={"llm": "openai", "openai_base_url": "https://api.mistral.ai/v1"}
    )
    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        fake_openai([FakeMistralModel("mistral-medium-3-5", True), FakeMistralModel("codestral", False)], {}),
    )
    assert admin.post("/api/admin/models", headers=ADMIN).json() == {
        "models": ["mistral-medium-3-5"],
        "vision_only": True,
    }


def local_server(monkeypatch, routes):
    """httpx client answering like a local LM Studio / Ollama (routes: (method, path) → JSON)."""
    real = httpx.AsyncClient

    def handler(request):
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404)
        answer = routes[key]
        return httpx.Response(200, json=answer(json.loads(request.content)) if callable(answer) else answer)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_lm_studio_models(admin, monkeypatch):
    import openai

    admin.put(
        "/api/admin/settings", headers=ADMIN, json={"llm": "openai", "openai_base_url": "http://localhost:1234/v1"}
    )
    monkeypatch.setattr(openai, "AsyncOpenAI", fake_openai([FakeModel("google/gemma-4"), FakeModel("qwen/qwen3")], {}))
    local_server(
        monkeypatch,
        {
            ("GET", "/api/v1/models"): {
                "models": [
                    {"type": "llm", "key": "google/gemma-4", "capabilities": {"vision": True}},
                    {"type": "llm", "key": "qwen/qwen3", "capabilities": {"vision": False}},
                    {"type": "embedding", "key": "nomic-embed"},
                ]
            }
        },
    )
    assert admin.post("/api/admin/models", headers=ADMIN).json() == {"models": ["google/gemma-4"], "vision_only": True}


def test_ollama_models(admin, monkeypatch):
    import openai

    admin.put(
        "/api/admin/settings", headers=ADMIN, json={"llm": "openai", "openai_base_url": "http://localhost:11434/v1"}
    )
    monkeypatch.setattr(openai, "AsyncOpenAI", fake_openai([FakeModel("qwen2.5vl:7b"), FakeModel("llama3:8b")], {}))
    capabilities = {"qwen2.5vl:7b": ["completion", "vision"], "llama3:8b": ["completion"]}
    local_server(
        monkeypatch,
        {
            ("GET", "/api/tags"): {"models": [{"name": "qwen2.5vl:7b"}, {"name": "llama3:8b"}]},
            ("POST", "/api/show"): lambda body: {"capabilities": capabilities[body["model"]]},
        },
    )
    assert admin.post("/api/admin/models", headers=ADMIN).json() == {"models": ["qwen2.5vl:7b"], "vision_only": True}


def test_test_image_and_json(admin, monkeypatch):
    """The admin test tells whether the model reads the image and answers in JSON."""
    from app import llm

    admin.put("/api/admin/settings", headers=ADMIN, json={"llm": "gemini", "gemini_api_key": "k"})

    def test():
        return admin.post("/api/admin/test", headers=ADMIN)

    async def answer(color):
        return llm._CheckAnswer(color=color)

    monkeypatch.setattr(llm, "_generate", lambda s, images, text, schema: answer("Red"))
    assert (test().json()["vision"], test().json()["json"]) == (True, True)

    monkeypatch.setattr(llm, "_generate", lambda s, images, text, schema: answer("I see no image"))
    assert (test().json()["vision"], test().json()["json"]) == (False, True)

    async def not_json(*_):
        raise llm.ExtractionError("llm.invalid_answer")

    monkeypatch.setattr(llm, "_generate", not_json)
    assert test().json()["json"] is False

    async def bad_key(*_):
        raise llm.ExtractionError("llm.invalid_key", provider="Gemini")

    monkeypatch.setattr(llm, "_generate", bad_key)
    assert test().status_code == 502 and test().json()["detail"]["code"] == "llm.invalid_key"


def test_model_without_vision_refuses_image(admin, monkeypatch):
    """Ollama refuses an image for a text-only model with a 400: the test says so plainly."""
    from app import llm

    admin.put("/api/admin/settings", headers=ADMIN, json={"llm": "gemini", "gemini_api_key": "k"})

    async def refuse(*_):
        raise llm.ExtractionError(
            "llm.api_error",
            provider="localhost:11434",
            status=400,
            detail="Multimodal data provided, but model does not support multimodal requests.",
        )

    monkeypatch.setattr(llm, "_generate", refuse)
    r = admin.post("/api/admin/test", headers=ADMIN).json()
    assert (r["vision"], r["json"]) == (False, None)
    assert "multimodal" in r["refused"]

    nested = {"message": '{"error":{"code":400,"message":"no vision here","type":"x"}}'}
    assert llm._error_message(nested) == "no vision here"


# --- Lesson owner / shared -------------------------------------------------------


def test_only_the_owner_changes_a_lesson(anki, client):
    lesson = _extract(client)  # created in Léa's profile: hers, private
    assert (lesson["owner"], lesson["shared"]) == ("Léa", False)
    url = f"/api/lessons/{lesson['id']}"
    cards = lesson["cards"]
    read_only = {"code": "lesson.read_only", "params": {"owner": "Léa"}}

    # Léa shares it; the owner never changes, even if a page sends one
    shared = client.put(url, json={"deck": lesson["deck"], "cards": cards, "shared": True, "owner": "Paul"}).json()
    assert (shared["owner"], shared["shared"]) == ("Léa", True)

    # From Paul's profile: read-only
    anki.profile = "Paul"
    for method, path, body in [
        ("PUT", url, {"deck": "Changed", "cards": []}),
        ("PUT", url, {"deck": lesson["deck"], "cards": cards, "shared": False}),
        ("POST", f"{url}/revise", {"deck": "D", "cards": cards, "instruction": "remove the last card"}),
        ("DELETE", url, None),
    ]:
        r = client.request(method, path, json=body)
        assert (r.status_code, r.json()["detail"]) == (403, read_only), (method, path)

    # ...but Paul can still read it, send it to his own Anki and export it, without changing it
    assert client.get(url).status_code == 200
    changed = {"deck": "Paul's copy", "cards": cards[:1], "lesson_id": lesson["id"]}
    assert client.post("/api/anki/send", json={**SEND, **changed}).status_code == 200
    assert client.post("/api/export", json=changed).status_code == 200
    kept = client.get(url).json()
    assert (kept["deck"], len(kept["cards"]), kept["shared"], kept["exported_at"]) == (
        lesson["deck"],
        len(cards),
        True,
        None,
    )

    # Back in Léa's profile: hers to change; updates that don't mention sharing leave it alone
    anki.profile = "Léa"
    assert client.put(url, json={"deck": "D", "cards": []}).json()["shared"] is True
    assert client.delete(url).status_code == 204


def test_lesson_without_owner_is_everyones(client):
    lesson = _extract(client)  # Anki closed: no owner
    assert lesson["owner"] == ""
    url = f"/api/lessons/{lesson['id']}"
    assert client.put(url, json={"deck": "D", "cards": []}).status_code == 200
    assert client.delete(url).status_code == 204


def test_other_profiles_private_lessons_hidden(anki, client):
    lea = _extract(client)  # private to Léa
    anki.profile = "Paul"
    paul = _extract(client)
    shared = _extract(client)
    client.put(f"/api/lessons/{shared['id']}", json={"deck": "S", "cards": [], "shared": True})

    # Paul doesn't get Léa's private lesson, in any route
    ids = {lesson["id"] for lesson in client.get("/api/lessons").json()}
    assert ids == {paul["id"], shared["id"]}
    for method, url in [
        ("GET", f"/api/lessons/{lea['id']}"),
        ("GET", f"/api/lessons/{lea['id']}/photos/1"),
        ("DELETE", f"/api/lessons/{lea['id']}"),
    ]:
        assert client.request(method, url).status_code == 404
    assert client.put(f"/api/lessons/{lea['id']}", json={"deck": "x", "cards": []}).status_code == 404
    assert client.post("/api/anki/send", json={**SEND, "lesson_id": lea["id"]}).status_code == 404

    anki.profile = "Léa"  # back in Léa's profile: it's hers again
    assert client.get(f"/api/lessons/{lea['id']}").status_code == 200
    assert {lesson["id"] for lesson in client.get("/api/lessons").json()} == {lea["id"], shared["id"]}

    # Anki closed: no profile known, every lesson listed (read-only for those with an owner)
    anki.profile = None
    assert len(client.get("/api/lessons").json()) == 3


def test_version_from_pyproject(client):
    import tomllib
    from pathlib import Path

    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert client.get("/api/config").json()["version"] == version


def test_release_notes(tmp_path, monkeypatch):
    from tools import changelog_section

    changelog = tmp_path / "CHANGELOG.md"
    monkeypatch.setattr(changelog_section, "CHANGELOG", changelog)
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n- Next thing\n\n## [0.2.0] - 2026-10-01\n\n- Done\n\n"
        "## [0.1.0]\n\n- First\n\n[0.2.0]: https://example.com\n"
    )
    assert changelog_section.notes("0.2.0") == "- Done"  # release being finalized
    assert changelog_section.notes("0.1.0") == "- First"  # last section, before the links
    assert changelog_section.notes("0.3.0") == "- Next thing"  # not finalized yet: Unreleased
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n## [0.1.0]\n\n- First\n")
    assert changelog_section.notes("0.2.0") == "No changes listed yet."


def test_admin_manages_every_lesson(anki, admin):
    lea = _extract(admin)  # Léa's, private
    url = f"/api/admin/lessons/{lea['id']}"
    assert admin.get("/api/admin/lessons").status_code == 401  # settings password needed
    assert admin.put(url, json={"owner": "Paul"}).status_code == 401

    listing = admin.get("/api/admin/lessons", headers=ADMIN).json()
    assert listing["profiles"] == ["Léa", "Paul"]
    assert [(x["id"], x["owner"], x["shared"]) for x in listing["lessons"]] == [(lea["id"], "Léa", False)]

    # Given to Paul, shared, then to nobody; the content is untouched
    anki.profile = "Paul"
    assert admin.put(url, headers=ADMIN, json={"owner": "Paul"}).json()["owner"] == "Paul"
    assert admin.put(f"/api/lessons/{lea['id']}", json={"deck": "Paul's now", "cards": []}).status_code == 200
    assert admin.put(url, headers=ADMIN, json={"shared": True}).json()["shared"] is True
    nobody = admin.put(url, headers=ADMIN, json={"owner": ""}).json()
    assert (nobody["owner"], nobody["deck"]) == ("", "Paul's now")

    # A lesson whose owner left Anki can be deleted from the settings
    admin.put(url, headers=ADMIN, json={"owner": "Ghost"})
    assert admin.delete(url, headers=ADMIN).status_code == 204
    assert admin.get(f"/api/lessons/{lea['id']}").status_code == 404
    assert admin.delete(url, headers=ADMIN).status_code == 404
    assert admin.put(url, headers=ADMIN, json={"owner": "Paul"}).status_code == 404


def test_admin_lessons_without_anki(admin):
    _extract(admin)
    listing = admin.get("/api/admin/lessons", headers=ADMIN).json()
    assert listing["profiles"] is None  # Anki closed: the page says so
    assert len(listing["lessons"]) == 1


def test_page_files_revalidated(client):
    for path in ("/", "/admin.html", "/app.js", "/style.css", "/i18n/fr.json"):
        res = client.get(path)
        assert res.status_code == 200 and res.headers["cache-control"] == "no-cache", path
    etag = client.get("/style.css").headers["etag"]
    assert client.get("/style.css", headers={"If-None-Match": etag}).status_code == 304  # unchanged: nothing re-sent


def test_lesson_from_the_prompt_alone(client):
    res = client.post("/api/extract", data={"prompt": "Cards with: le chat, le chien"})
    assert res.status_code == 201, res.text
    lesson = res.json()
    assert (lesson["photo_count"], lesson["prompt"]) == (0, "Cards with: le chat, le chien")
    assert "0 photo(s)" in lesson["cards"][-1]["front"]  # the fake provider got no image
    assert client.get(f"/api/lessons/{lesson['id']}").status_code == 200
    body = {"deck": lesson["deck"], "cards": lesson["cards"], "instruction": "add the colours"}
    assert client.post(f"/api/lessons/{lesson['id']}/revise", json=body).status_code == 200  # no photos to send

    # Neither a photo nor a prompt: nothing to work from
    res = client.post("/api/extract", data={"prompt": "  "})
    assert (res.status_code, res.json()["detail"]["code"]) == (400, "extract.no_input")


def test_prompt_only_tells_the_ai_there_is_no_photo():
    from app import llm
    from app.models import Deck

    assert "no photo" in llm._user_text("Cards with: le chat", "", photos=0)
    assert "no photo" not in llm._user_text("Vocabulary", "", photos=2)
    assert "these instructions (no photo)" in llm._revision_text("p", Deck(deck="D", cards=[]), "x", "en", photos=0)


def test_no_sync_when_the_profile_is_not_logged_in(anki, client, monkeypatch):
    from app import ankiconnect

    async def not_logged_in():
        return False

    monkeypatch.setattr(ankiconnect, "sync_configured", not_logged_in)  # what the add-on says
    res = client.post("/api/anki/send", json=SEND).json()
    assert (res["synced"], res["sync_error"], res["sync_skipped"]) == (False, None, True)
    assert "sync" not in anki.calls


def test_voice_for_a_language(monkeypatch):
    import asyncio

    available = [
        {"voice": "es-MX-JorgeNeural", "locale": "es-MX", "gender": "Male"},
        {"voice": "de-AT-IngridNeural", "locale": "de-AT", "gender": "Female"},
        {"voice": "ja-JP-KeitaNeural", "locale": "ja-JP", "gender": "Male"},
        {"voice": "ja-JP-NanamiNeural", "locale": "ja-JP", "gender": "Female"},
    ]

    async def voices():
        return available

    monkeypatch.setattr(tts, "voices", voices)
    voice = lambda language: asyncio.run(tts.voice_for(language))  # noqa: E731
    assert voice("es-ES") == voice("es") == "es-ES-ElviraNeural"  # preferred
    assert voice("es-MX") == "es-MX-DaliaNeural"
    assert voice("de-AT") == "de-AT-IngridNeural"  # that variety, not the preferred German one
    assert voice("ja-JP") == voice("ja") == "ja-JP-NanamiNeural"  # a female voice first
    assert voice("xx") == voice("") == ""


def test_auto_voice_follows_the_language_of_the_backs(client):
    lesson = client.post("/api/extract", data={"prompt": "FR → ES", "voice": "auto"}).json()
    assert lesson["voice"] == "es-ES-ElviraNeural"  # the fake AI says the backs are es-ES
    voices = {p["id"]: p["voice"] for p in client.get("/api/prompts").json()}
    assert voices["cartable:sentences"] == voices["cartable:vocabulary"] == "auto"
    assert voices["cartable:questions"] == ""
