"""Diagram labels hidden on the photo: boxes from the AI, card images, Anki notes."""

import html
import io
import json
import sqlite3
import zipfile

import pytest
from PIL import Image

from app import diagrams, llm
from app.models import Card, Mask


def photo(width=1000, height=500) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, "JPEG")
    return out.getvalue()


@pytest.mark.parametrize(
    ("model", "fmt"),
    [
        ("gemini-3.8-flash", "gemini"),
        ("google/gemini-3.8-flash", "gemini"),
        ("qwen/qwen3.8-max-0902", "normalized"),
        ("gpt-6.1-sol", "pixels"),
        ("claude-opus-5", "pixels"),
    ],
)
def test_box_format_follows_the_model(model, fmt):
    assert diagrams.box_format(model) == fmt


@pytest.mark.parametrize(
    ("fmt", "box"),
    [
        ("gemini", [100, 200, 300, 400]),  # y, x, y, x on 0-1000
        ("normalized", [200, 100, 400, 300]),  # x, y, x, y on 0-1000
        ("pixels", [200, 50, 400, 150]),  # on a 1000x500 photo
    ],
)
def test_boxes_become_fractions_of_the_photo(fmt, box):
    cards = [Card(front="What is (1)?", back="mouth", mask=Mask(page=1, n=1, box=box))]
    diagrams.normalize(cards, [(1000, 500)], fmt)
    x0, y0, x1, y1 = cards[0].mask.box
    pad_x, pad_y = diagrams.PADDING, diagrams.PADDING * 2  # a margin of the larger side, on each axis
    assert (x0, y0, x1, y1) == pytest.approx((0.2 - pad_x, 0.1 - pad_y, 0.4 + pad_x, 0.3 + pad_y), abs=1e-3)


def test_masks_that_cant_be_placed_are_dropped():
    cards = [
        Card(front="a", back="b", mask=Mask(page=2, n=1, box=[1, 2, 3, 4])),  # no photo 2
        Card(front="c", back="d", mask=Mask(page=1, n=2, box=[5, 5, 5, 9])),  # empty box
        Card(front="e", back="f", mask=Mask(page=1, n=3, box=[1, 2, 3])),  # not a box
        Card(front="g", back="h"),
    ]
    diagrams.normalize(cards, [(100, 100)], "pixels")
    assert [c.mask for c in cards] == [None, None, None, None]
    # An unreadable photo has no size: its masks can't be placed either
    cards = [Card(front="a", back="b", mask=Mask(page=1, n=1, box=[1, 2, 3, 4]))]
    diagrams.normalize(cards, [None], "pixels")
    assert cards[0].mask is None


def test_card_images_hide_every_label(tmp_path):
    page = tmp_path / "page-1.jpg"
    page.write_bytes(photo())
    cards = [
        Card(front="What is (1)?", back="mouth", mask=Mask(page=1, n=1, box=[0.1, 0.1, 0.3, 0.2])),
        Card(front="What is (2)?", back="heart", mask=Mask(page=1, n=2, box=[0.6, 0.6, 0.8, 0.7])),
    ]
    question, answer = diagrams.card_images(tmp_path / "images", page, cards[0], cards)

    def color(path, x, y):
        image = Image.open(path)
        return image.getpixel((int(x * image.width), int(y * image.height)))

    red = lambda c: c[0] > 200 and c[1] < 160  # noqa: E731  (the asked label, highlighted)
    yellow = lambda c: c[0] > 200 and c[1] > 180 and c[2] < 180  # noqa: E731  (the other labels)
    assert red(color(question, 0.12, 0.12)) and yellow(color(question, 0.62, 0.62))
    assert color(answer, 0.12, 0.12) == pytest.approx((255, 255, 255), abs=12)  # shown again
    assert yellow(color(answer, 0.62, 0.62))

    # Same masks: the same files, not drawn again; a moved mask: new files, old ones pruned
    assert diagrams.card_images(tmp_path / "images", page, cards[0], cards) == (question, answer)
    cards[1].mask.box = [0.5, 0.5, 0.7, 0.6]
    moved = diagrams.card_images(tmp_path / "images", page, cards[0], cards)
    assert moved != (question, answer)
    diagrams.prune(tmp_path / "images", set(moved))
    assert sorted(p.name for p in (tmp_path / "images").iterdir()) == sorted(p.name for p in moved)


def test_revision_keeps_the_masks():
    mask = Mask(page=1, n=2, box=[0.1, 0.1, 0.2, 0.2])
    before = [Card(front="What is (2)?", back="hart", mask=mask), Card(front="le chat", back="el gato")]
    revised = [
        Card(front="What is (2)?", back="heart"),
        Card(front="le chat", back="el gato"),
        Card(front="new", back="x"),
    ]
    llm._keep_masks(revised, before)
    assert [c.mask for c in revised] == [mask, None, None]


def test_diagram_lesson_exported(client, tmp_path):
    res = client.post(
        "/api/extract", files=[("images", ("p.jpg", photo(), "image/jpeg"))], data={"prompt": "Le schéma sans les noms"}
    )
    lesson = res.json()
    cards = lesson["cards"]
    assert [c["mask"]["n"] for c in cards] == [1, 2, 3]  # the fake provider's three labels

    def export(cards):
        body = {"deck": lesson["deck"], "cards": cards, "lesson_id": lesson["id"]}
        content = client.post("/api/export", json=body).content
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            media = json.loads(z.read("media"))
            db = z.read("collection.anki2")
        path = tmp_path / "collection.anki2"
        path.write_bytes(db)
        con = sqlite3.connect(path)
        notes = con.execute("select guid, flds from notes order by id").fetchall()
        models = json.loads(con.execute("select models from col").fetchone()[0])
        con.close()
        return notes, models, sorted(media.values())

    notes, models, media = export(cards)
    assert {m["name"] for m in models.values()} == {"Cartable schéma (audio)"}
    fields = [html.unescape(f) for f in notes[0][1].split("\x1f")]
    assert fields[:2] == ["Qu'est-ce que (1) ?", "la bouche"]
    assert fields[-1] == f"{lesson['id']}:1:1"  # Id: lesson, photo, label number
    assert len(media) == 6 and all(m.startswith("diagram-1-") for m in media)  # front and back per label

    # Mask moved and answer corrected: same notes (GUID from the Id), new images
    cards[0]["mask"]["box"] = [0.12, 0.1, 0.37, 0.2]
    cards[0]["back"] = "la langue"
    notes2, _, media2 = export(cards)
    assert [g for g, _ in notes2] == [g for g, _ in notes]
    assert media2 != media


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_photos_turned_upright_with_their_masks(degrees):
    """A red spot under a mask stays under it once the photo is turned."""
    image = Image.new("RGB", (1000, 500), "white")
    box = [0.1, 0.2, 0.3, 0.4]  # x0, y0, x1, y1
    image.paste((255, 0, 0), (100, 100, 300, 200))
    out = io.BytesIO()
    image.save(out, "PNG")
    cards = [Card(front="What is (1)?", back="spot", mask=Mask(page=1, n=1, box=box)), Card(front="a", back="b")]

    (turned,) = diagrams.straighten([out.getvalue()], cards, [degrees])
    result = Image.open(io.BytesIO(turned))
    assert result.size == ((500, 1000) if degrees in (90, 270) else (1000, 500))
    x0, y0, x1, y1 = cards[0].mask.box
    center = (int((x0 + x1) / 2 * result.width), int((y0 + y1) / 2 * result.height))
    r, g, b = result.getpixel(center)
    assert r > 200 and g < 60 and b < 60
    # Turning back the other way gives the first box again
    assert diagrams.rotate_box(cards[0].mask.box, 360 - degrees if degrees != 180 else 180) == pytest.approx(box)


def test_rotation_ignored_when_unknown_or_unreadable():
    cards = [Card(front="a", back="b", mask=Mask(page=1, n=1, box=[0.1, 0.2, 0.3, 0.4]))]
    assert diagrams.straighten([photo()], cards, [45]) == [photo()]  # not a right angle
    assert diagrams.straighten([b"not an image"], cards, [90]) == [b"not an image"]
    assert diagrams.straighten([photo()], cards, []) == [photo()]  # no answer for that photo
    assert cards[0].mask.box == [0.1, 0.2, 0.3, 0.4]


def test_sideways_photo_saved_upright(client, monkeypatch):
    from app import main
    from app.models import Extraction

    async def sideways(images, prompt, deck=""):
        mask = Mask(page=1, n=1, box=[0.1, 0.2, 0.3, 0.4])
        return Extraction(deck="D", cards=[Card(front="What is (1)?", back="x", mask=mask)], rotations=[90])

    monkeypatch.setattr(main, "extract_cards", sideways)
    res = client.post(
        "/api/extract", files=[("images", ("p.jpg", photo(1000, 500), "image/jpeg"))], data={"prompt": "p"}
    )
    lesson = res.json()
    assert "rotations" not in lesson
    assert lesson["cards"][0]["mask"]["box"] == pytest.approx([0.6, 0.1, 0.8, 0.3])
    saved = Image.open(io.BytesIO(client.get(f"/api/lessons/{lesson['id']}/photos/1").content))
    assert saved.size == (500, 1000)
