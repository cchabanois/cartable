"""Pictures on cards ("front: the picture of the word"), drawn by an image model."""

import html
import io
import json
import sqlite3
import zipfile

import pytest
from PIL import Image

from app import llm, pictures
from app.models import Card
from app.settings import Settings


def png(color="red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1024, 1024), color).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def drawn(monkeypatch):
    """A fake image model: records each call like a real one (0.03 $ each)."""
    subjects = []

    async def draw(s, subject):
        subjects.append(subject)
        if "umbrella" in subject:
            raise pictures.PictureError("picture.failed", detail="refused")
        await llm.record(s, "openrouter.ai", "google/gemini-3.1-flash-lite-image", 20, 1300, cost=0.03)
        return png()

    monkeypatch.setattr(pictures, "draw", draw)
    return subjects


def lesson_with_pictures(client):
    return client.post("/api/extract", data={"prompt": "10 mots courants, recto : image du mot"}).json()


def test_cards_ask_for_pictures_and_get_ids(client):
    lesson = lesson_with_pictures(client)
    assert [c["picture_prompt"] for c in lesson["cards"]] == ["an apple", "a dog", "an umbrella", ""]
    assert all(c["id"] and not c["picture"] for c in lesson["cards"])
    assert len({c["id"] for c in lesson["cards"]}) == 4


def test_pictures_drawn_and_kept(client, drawn):
    lesson = lesson_with_pictures(client)
    res = client.post(f"/api/lessons/{lesson['id']}/pictures").json()
    cards = res["lesson"]["cards"]
    assert res["failures"] == 1  # the umbrella: its card stays without a picture
    assert res["error"] == {"code": "picture.failed", "params": {"detail": "refused"}}  # why, for the page
    assert sorted(drawn) == ["a dog", "an apple", "an umbrella"]  # nothing for "tomorrow"
    assert [bool(c["picture"]) for c in cards] == [True, True, False, False]
    assert [c["id"] for c in cards] == [c["id"] for c in lesson["cards"]]  # ids kept
    picture = client.get(f"/api/lessons/{lesson['id']}/pictures/{cards[0]['picture']}")
    assert picture.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(picture.content)).size == (pictures.SIDE, pictures.SIDE)
    calls = client.get(f"/api/lessons/{lesson['id']}").json()["ai_calls"]
    assert [c["kind"] for c in calls] == ["extract", "picture", "picture"]

    # Drawn again: only what is missing (the umbrella, failing again)
    drawn.clear()
    client.post(f"/api/lessons/{lesson['id']}/pictures")
    assert drawn == ["an umbrella"]


def test_picture_names_checked(client, drawn):
    lesson = lesson_with_pictures(client)
    for name in ("../lesson.json", "page-1.jpg", "picture-x-12345678.jpg"):
        assert client.get(f"/api/lessons/{lesson['id']}/pictures/{name}").status_code == 404


def test_picture_cards_in_anki(client, drawn, tmp_path):
    lesson = lesson_with_pictures(client)
    cards = client.post(f"/api/lessons/{lesson['id']}/pictures").json()["lesson"]["cards"]

    def export(cards):
        body = {"deck": "D", "cards": cards, "lesson_id": lesson["id"]}
        with zipfile.ZipFile(io.BytesIO(client.post("/api/export", json=body).content)) as z:
            media = sorted(json.loads(z.read("media")).values())
            (tmp_path / "c.anki2").write_bytes(z.read("collection.anki2"))
        con = sqlite3.connect(tmp_path / "c.anki2")
        notes = con.execute("select guid, mid, flds from notes order by id").fetchall()
        models = json.loads(con.execute("select models from col").fetchone()[0])
        con.close()
        return notes, models, media

    notes, models, media = export(cards)
    names = {int(i): m["name"] for i, m in models.items()}
    assert sorted(names[mid] for _, mid, _ in notes) == [
        "Notosaurus image (audio)",
        "Notosaurus image (audio)",
        "Notosaurus recto/verso (audio)",  # the umbrella without its picture: a text card
        "Notosaurus recto/verso (audio)",  # "tomorrow"
    ]
    picture_fields = [
        html.unescape(f).split("\x1f") for _, mid, f in notes if names[mid].startswith("Notosaurus image")
    ]
    assert picture_fields[0][0] == "Comment dit-on en anglais ?"
    assert picture_fields[0][-2] == f'<img src="{cards[0]["picture"]}">' and picture_fields[0][-1] == cards[0]["id"]
    assert media == sorted([cards[0]["picture"], cards[1]["picture"]])

    # Same front on every picture card, then a new picture: the same notes (GUID from the id)
    cards[0]["picture"], cards[1]["front"] = cards[1]["picture"], ""
    notes2, _, _ = export(cards)
    assert [g for g, _, _ in notes2] == [g for g, _, _ in notes]


def test_only_the_owner_draws(client, drawn, monkeypatch):
    from app import ankiconnect

    profile = {"name": "Léa"}

    async def active_profile():
        return profile["name"]

    monkeypatch.setattr(ankiconnect, "active_profile", active_profile)
    lesson = lesson_with_pictures(client)  # Léa's
    client.put(f"/api/lessons/{lesson['id']}", json={"deck": "D", "cards": lesson["cards"], "shared": True})
    profile["name"] = "Paul"
    assert client.post(f"/api/lessons/{lesson['id']}/pictures").status_code == 403


def test_correction_keeps_ids_and_pictures():
    before = [
        Card(front="Q", back="an apple", picture_prompt="an apple", picture="picture-a-12345678.jpg", id="a"),
        Card(front="Q", back="a dog", picture_prompt="a dog", picture="picture-b-12345678.jpg", id="b"),
    ]
    revised = [
        Card(front="Q", back="a dog", picture_prompt="a dog"),
        Card(front="Q", back="the apple", picture_prompt="an apple"),  # answer fixed: same card
        Card(front="Q", back="a cat", picture_prompt="a cat"),  # added
    ]
    llm._keep_ids(revised, before)
    assert [(c.id, c.picture) for c in revised] == [
        ("b", "picture-b-12345678.jpg"),
        ("a", "picture-a-12345678.jpg"),
        ("", ""),
    ]


def test_pictures_drawn_by_the_cards_service():
    def drawing(**kw):
        s = Settings(**kw)
        return pictures.service(s), pictures.model(s)

    assert drawing() == ("", "")  # no key
    # The cards' own service, when it draws
    assert drawing(llm="gemini", gemini_api_key="k") == ("gemini", "gemini-3.1-flash-lite-image")
    assert drawing(llm="openai", openai_api_key="k", openrouter_api_key="r") == ("openai", "gpt-image-1-mini")
    assert drawing(llm="openrouter", openrouter_api_key="k", gemini_api_key="free") == (
        "openrouter",
        "google/gemini-3.1-flash-lite-image",
    )
    # Claude or a local model can't draw: the first service with a key
    assert drawing(llm="anthropic", gemini_api_key="k") == ("gemini", "gemini-3.1-flash-lite-image")
    assert drawing(llm="compatible", gemini_api_key="g", openrouter_api_key="r")[0] == "openrouter"
    # Chosen on purpose: that service, with its model or the one set
    chosen = {"llm": "openai", "openai_api_key": "k", "openrouter_api_key": "r", "picture_service": "openrouter"}
    assert drawing(**chosen) == ("openrouter", "google/gemini-3.1-flash-lite-image")
    assert drawing(**chosen, picture_model="openai/gpt-5-image-mini") == ("openrouter", "openai/gpt-5-image-mini")
    assert drawing(llm="gemini", gemini_api_key="k", picture_service="openai") == ("", "")  # its key is missing
    assert drawing(llm="gemini", gemini_api_key="k", picture_service="none") == ("", "")  # no pictures


def test_picture_model_saved_before_the_service(client, tmp_path):
    """A model saved alone went through the service its name told: still the same."""
    import json

    from app import settings

    path = tmp_path / "data" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"llm": "openai", "picture_model": "google/gemini-3.1-flash-image"}))
    assert settings.current().picture_service == "openrouter"
    path.write_text(json.dumps({"llm": "anthropic", "picture_model": "gemini-3.1-flash-image"}))
    assert settings.current().picture_service == "gemini"


def test_cache_reused_by_the_next_lessons(client, drawn):
    first = lesson_with_pictures(client)
    client.post(f"/api/lessons/{first['id']}/pictures")
    assert sorted(drawn) == ["a dog", "an apple", "an umbrella"]
    drawn.clear()
    second = lesson_with_pictures(client)  # the same subjects
    cards = client.post(f"/api/lessons/{second['id']}/pictures").json()["lesson"]["cards"]
    assert drawn == ["an umbrella"]  # only the one that failed: the others come from the cache, free
    assert [bool(c["picture"]) for c in cards] == [True, True, False, False]
    calls = client.get(f"/api/lessons/{second['id']}").json()["ai_calls"]
    assert [c["kind"] for c in calls] == ["extract"]  # nothing paid for the pictures


def test_redraw_with_another_subject(client, drawn):
    lesson = lesson_with_pictures(client)
    cards = client.post(f"/api/lessons/{lesson['id']}/pictures").json()["lesson"]["cards"]
    apple = cards[0]
    url = f"/api/lessons/{lesson['id']}/cards/{apple['id']}/picture/draw"

    drawn.clear()
    res = client.post(url, json={}).json()  # again, the same subject: drawn anew, not from the cache
    assert drawn == ["an apple"] and res["card"]["picture"] and res["card"]["id"] == apple["id"]
    res = client.post(url, json={"subject": "a green apple"}).json()
    assert res["card"]["picture_prompt"] == "a green apple"
    saved = client.get(f"/api/lessons/{lesson['id']}").json()
    assert saved["cards"][0]["picture"] == res["card"]["picture"]
    assert [c["kind"] for c in saved["ai_calls"]].count("picture") == 4  # 2 at first + 2 redrawn

    # A card without a picture gets one ("tomorrow"), with a subject written by the user
    tomorrow = cards[3]
    res = client.post(f"/api/lessons/{lesson['id']}/cards/{tomorrow['id']}/picture/draw", json={"subject": " "})
    assert res.json()["detail"]["code"] == "picture.no_subject"
    url = f"/api/lessons/{lesson['id']}/cards/{tomorrow['id']}/picture/draw"
    assert client.post(url, json={"subject": "a calendar"}).json()["card"]["picture"]
    assert client.post(f"/api/lessons/{lesson['id']}/cards/nope/picture/draw", json={}).status_code == 404


def test_own_photo_then_no_picture(client, drawn, tmp_path):
    lesson = lesson_with_pictures(client)
    cards = client.post(f"/api/lessons/{lesson['id']}/pictures").json()["lesson"]["cards"]
    dog = cards[1]
    out = io.BytesIO()
    Image.new("RGB", (1600, 1200), "blue").save(out, "JPEG")
    url = f"/api/lessons/{lesson['id']}/cards/{dog['id']}/picture"
    res = client.post(url, files={"photo": ("p.jpg", out.getvalue(), "image/jpeg")}).json()
    assert res["card"]["picture"] != dog["picture"]
    folder = tmp_path / "data" / "lessons" / lesson["id"] / "images"
    assert not (folder / dog["picture"]).exists()  # the replaced picture is removed
    assert Image.open(folder / res["card"]["picture"]).size == (pictures.SIDE, 384)
    assert client.post(url, files={"photo": ("p.txt", b"x", "text/plain")}).status_code == 400

    res = client.delete(url).json()
    assert (res["card"]["picture"], res["card"]["picture_prompt"]) == ("", "")  # a text card now
    assert not any(folder.glob(f"picture-{dog['id']}-*"))
