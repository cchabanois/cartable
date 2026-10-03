"""Data formats: files carry the format they are written in; older ones are brought up
to date when read; a newer one (from a newer Notosaurus) is never read nor damaged."""

import json

from app import lessons, prompts, settings, usage


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_files_carry_their_format(client, tmp_path):
    lesson = client.post("/api/extract", data={"prompt": "FR → ES"}).json()
    client.post("/api/prompts", json={"name": "Mine", "text": "x"})
    settings.save({"tts_rate": "+0%"})
    data = tmp_path / "data"
    assert read(data / "lessons" / lesson["id"] / "lesson.json")["format"] == lessons.FORMAT
    assert read(data / "prompts.json")["format"] == prompts.FORMAT
    assert read(data / "settings.json")["format"] == settings.FORMAT
    assert read(data / "ai-calls.json")["format"] == usage.FORMAT


def test_files_before_formats_still_read(client, tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "ai-calls.json").write_text(
        json.dumps([{"at": "2026-09-01T10:00:00", "kind": "extract", "provider": "x", "model": "m", "cost": 0.01}])
    )
    (data / "prompts.json").write_text(json.dumps([{"id": 1, "name": "Old", "text": "kept"}]))
    assert usage.totals().total == 0.01
    assert [p.name for p in prompts.list_all() if not p.builtin] == ["Old"]


def test_a_newer_lesson_is_left_alone(client, tmp_path):
    lesson = client.post("/api/extract", data={"prompt": "FR → ES"}).json()
    path = tmp_path / "data" / "lessons" / lesson["id"] / "lesson.json"
    newer = {**read(path), "format": lessons.FORMAT + 1, "something": "new"}
    path.write_text(json.dumps(newer))
    assert client.get("/api/lessons").json() == []  # not listed, the others still are
    r = client.put(f"/api/lessons/{lesson['id']}", json={"deck": "x", "cards": []})
    assert (r.status_code, r.json()["detail"]) == (409, {"code": "data.too_new", "params": {"what": "lesson"}})
    assert read(path) == newer  # untouched


def test_newer_settings_refused(client, tmp_path):
    path = tmp_path / "data" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": settings.FORMAT + 1, "llm": "gemini"}))
    r = client.get("/api/config")
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "data.too_new")
