"""Diagram labels hidden on the photo: boxes from the AI, card images, Anki notes."""

import html
import io
import json
import sqlite3
import zipfile

import pytest
from PIL import Image

from app import diagrams, llm
from app.models import Card, Frame, Mask


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


def test_one_light_image_per_diagram(tmp_path):
    page = tmp_path / "page-1.jpg"
    Image.new("RGB", (3000, 2000), "white").save(page, "JPEG")
    image = diagrams.page_image(tmp_path / "images", page)
    assert max(Image.open(image).size) == diagrams.CARD_SIDE
    assert diagrams.page_image(tmp_path / "images", page) == image  # same photo: same file, not redone

    # Another photo content: a new file; the old one is pruned once unused
    Image.new("RGB", (3000, 2000), "black").save(page, "JPEG")
    other = diagrams.page_image(tmp_path / "images", page)
    assert other != image
    diagrams.prune(tmp_path / "images", {other})
    assert [p.name for p in (tmp_path / "images").iterdir()] == [other.name]


def test_masks_as_html():
    masks = [Mask(page=1, n=2, box=[0.6, 0.6, 0.8, 0.7]), Mask(page=1, n=1, box=[0.1, 0.1, 0.3, 0.25])]
    question = diagrams.masks_html(masks, target=2, reveal=False)
    assert question == (
        '<div class="cartable-mask" style="left:10.0%;top:10.0%;width:20.0%;height:15.0%">(1)</div>'
        '<div class="cartable-mask target" style="left:60.0%;top:60.0%;width:20.0%;height:10.0%">(2)</div>'
    )
    answer = diagrams.masks_html(masks, target=2, reveal=True)
    assert '<div class="cartable-mask revealed" style="left:60.0%;top:60.0%;width:20.0%;height:10.0%"></div>' in answer
    assert ">(1)</div>" in answer  # the other labels stay hidden


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
    (model,) = models.values()
    assert model["name"] == "Cartable légendes (audio)"
    names = [f["name"] for f in model["flds"]]
    fields = dict(zip(names, (html.unescape(f) for f in notes[0][1].split("\x1f")), strict=True))
    assert (fields["Front"], fields["Back"]) == ("Qu'est-ce que (1) ?", "la bouche")
    assert fields["Id"] == f"{lesson['id']}:1:1"  # lesson, photo, label number
    assert fields["Masks"].count("cartable-mask") == 3 and 'class="cartable-mask target"' in fields["Masks"]
    assert len(media) == 1 and fields["Image"] == f'<img src="{media[0]}">'  # one image for the whole diagram

    # Mask moved and answer corrected: same notes (GUID from the Id), same image, new masks
    cards[0]["mask"]["box"] = [0.12, 0.1, 0.37, 0.2]
    cards[0]["back"] = "la langue"
    notes2, _, media2 = export(cards)
    assert [g for g, _ in notes2] == [g for g, _ in notes]
    assert media2 == media
    assert notes2[0][1] != notes[0][1]


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
    from app.models import Deck

    async def sideways(images, prompt, deck="", profile=None):
        mask = Mask(page=1, n=1, box=[0.1, 0.2, 0.3, 0.4])
        deck = Deck(deck="D", cards=[Card(front="What is (1)?", back="x", mask=mask)])
        return llm.Extracted(deck, [90], [Frame(page=1, box=[0.05, 0.1, 0.5, 0.6])])

    monkeypatch.setattr(main, "extract_cards", sideways)
    files = [("images", ("p.jpg", photo(1000, 500), "image/jpeg"))]
    lesson = client.post("/api/extract", files=files, data={"prompt": "p"}).json()
    assert lesson["cards"][0]["mask"]["box"] == pytest.approx([0.6, 0.1, 0.8, 0.3])
    assert lesson["frames"] == [{"page": 1, "box": pytest.approx([0.4, 0.05, 0.9, 0.5])}]  # turned too
    saved = Image.open(io.BytesIO(client.get(f"/api/lessons/{lesson['id']}/photos/1").content))
    assert saved.size == (500, 1000)


@pytest.mark.parametrize(
    ("first", "last", "turn"),
    [
        ([100, 400, 150, 450], [100, 600, 150, 650], 0),  # left to right: upright
        ([100, 600, 150, 650], [100, 400, 150, 450], 180),  # right to left: upside down
        ([100, 400, 150, 450], [700, 400, 750, 450], 270),  # down the photo: its top is on the right
        ([700, 400, 750, 450], [100, 400, 150, 450], 90),  # up the photo: its top is on the left
    ],
)
def test_turn_from_the_reading_direction(first, last, turn):
    from app.models import TextLine

    line = TextLine(page=1, first_word=first, last_word=last)  # gemini boxes: y, x, y, x on 0-1000
    assert diagrams.turns([line], [(1000, 800)], "gemini") == [turn]


def test_turn_unknown_without_a_usable_line():
    from app.models import TextLine

    lines = [
        TextLine(page=2, first_word=[1, 2, 3, 4], last_word=[5, 6, 7, 8]),
        TextLine(page=1, first_word=[1], last_word=[]),
    ]
    assert diagrams.turns(lines, [(100, 100)], "pixels") == [0]


def test_turn_a_photo_by_hand(client):
    files = [("images", (f"p{i}.jpg", photo(1000, 500), "image/jpeg")) for i in (1, 2)]
    lesson = client.post("/api/extract", files=files, data={"prompt": "Le schéma"}).json()  # masks on photo 1
    url = f"/api/lessons/{lesson['id']}/photos"

    turned = client.post(f"{url}/1/rotate").json()
    assert [c["mask"]["box"] for c in turned["cards"]] == [
        pytest.approx(diagrams.rotate_box(c["mask"]["box"], 90)) for c in lesson["cards"]
    ]
    assert Image.open(io.BytesIO(client.get(f"{url}/1").content)).size == (500, 1000)
    assert Image.open(io.BytesIO(client.get(f"{url}/2").content)).size == (1000, 500)  # the other photo: as it was

    # Photo 2 has no mask: only the photo turns
    assert client.post(f"{url}/2/rotate").json()["cards"] == turned["cards"]
    assert Image.open(io.BytesIO(client.get(f"{url}/2").content)).size == (500, 1000)
    assert client.post(f"{url}/3/rotate").status_code == 404


def test_frames_become_fractions():
    found = [Frame(page=1, box=[100, 200, 600, 800]), Frame(page=2, box=[1, 2, 3, 4]), Frame(page=1, box=[5])]
    m = diagrams.CROP_MARGIN  # a frame from the AI gets a margin: it is often drawn tight on the drawing
    (frame,) = diagrams.frames(found, [(1000, 500)], "gemini")
    assert (frame.page, frame.box) == (1, pytest.approx([0.2 - m, 0.1 - m, 0.8 + m, 0.6 + m]))


def test_crop_holds_the_frame_and_every_mask():
    masks = [Mask(page=1, n=1, box=[0.05, 0.3, 0.15, 0.35]), Mask(page=1, n=2, box=[0.5, 0.5, 0.7, 0.9])]
    m = diagrams.CROP_MARGIN
    assert diagrams.crop([0.1, 0.2, 0.6, 0.8], masks) == pytest.approx([0.05 - m, 0.2, 0.7 + m, 0.9 + m])
    assert diagrams.crop([0.0, 0.1, 0.9, 1.0], masks) == pytest.approx([0.0, 0.1, 0.9, 1.0])  # already holds them
    assert diagrams.crop(None, masks) is None  # no frame from the AI: the whole photo
    # A crop is stable: used as the frame, it gives itself again (a frame set by hand doesn't grow)
    once = diagrams.crop([0.1, 0.2, 0.6, 0.8], masks)
    assert diagrams.crop(once, masks) == pytest.approx(once)


def test_frame_set_by_hand(client):
    files = [("images", ("p.jpg", photo(1000, 500), "image/jpeg"))]
    lesson = client.post("/api/extract", files=files, data={"prompt": "Le schéma"}).json()
    assert lesson["frames"] == []  # the fake provider gives none: the whole photo
    frame = {"page": 1, "box": [0.05, 0.05, 0.9, 0.9]}
    saved = client.put(f"/api/lessons/{lesson['id']}", json={"deck": "D", "cards": lesson["cards"], "frames": [frame]})
    assert saved.json()["frames"] == [frame]
    # An update without frames (an older page) leaves them alone
    older = client.put(f"/api/lessons/{lesson['id']}", json={"deck": "D", "cards": lesson["cards"]})
    assert older.json()["frames"] == [frame]

    def exported_size(**extra):
        body = {"deck": "D", "cards": lesson["cards"], "lesson_id": lesson["id"], **extra}
        with zipfile.ZipFile(io.BytesIO(client.post("/api/export", json=body).content)) as z:
            (index,) = json.loads(z.read("media")).keys()
            return Image.open(io.BytesIO(z.read(index))).size

    assert exported_size() == (850, 425)  # the saved frame
    assert exported_size(frames=[]) == (1000, 500)  # the page's current frames win: none, the whole photo


def test_cropped_image_and_masks(tmp_path):
    page = tmp_path / "page-1.jpg"
    Image.new("RGB", (1000, 500), "white").save(page, "JPEG")
    box = [0.2, 0.2, 0.6, 1.0]
    assert Image.open(diagrams.page_image(tmp_path / "images", page, box)).size == (400, 400)
    assert diagrams.page_image(tmp_path / "images", page, box) != diagrams.page_image(tmp_path / "images", page)
    html = diagrams.masks_html([Mask(page=1, n=1, box=[0.3, 0.4, 0.4, 0.6])], target=1, reveal=False, box=box)
    assert 'style="left:25.0%;top:25.0%;width:25.0%;height:25.0%"' in html  # relative to the crop


def test_turning_a_photo_by_hand_turns_its_frame(client, monkeypatch):
    from app import main
    from app.models import Deck

    async def framed(images, prompt, deck="", profile=None):
        mask = Mask(page=1, n=1, box=[0.1, 0.2, 0.3, 0.4])
        deck = Deck(deck="D", cards=[Card(front="What is (1)?", back="x", mask=mask)])
        return llm.Extracted(deck, [0], [Frame(page=1, box=[0.05, 0.1, 0.5, 0.6])])

    monkeypatch.setattr(main, "extract_cards", framed)
    files = [("images", ("p.jpg", photo(1000, 500), "image/jpeg"))]
    lesson = client.post("/api/extract", files=files, data={"prompt": "p"}).json()
    turned = client.post(f"/api/lessons/{lesson['id']}/photos/1/rotate").json()
    assert turned["frames"][0]["box"] == pytest.approx(diagrams.rotate_box([0.05, 0.1, 0.5, 0.6], 90))

    # The exported image is cropped to the frame and its masks
    body = {"deck": "D", "cards": turned["cards"], "lesson_id": lesson["id"]}
    with zipfile.ZipFile(io.BytesIO(client.post("/api/export", json=body).content)) as z:
        (name,) = json.loads(z.read("media")).values()
        (index,) = json.loads(z.read("media")).keys()
        size = Image.open(io.BytesIO(z.read(index))).size
    assert name.startswith("diagram-page-1-") and size[0] < 500 and size[1] < 1000


def test_correction_adds_a_masked_label():
    kept = Mask(page=1, n=1, box=[0.1, 0.1, 0.2, 0.2])
    before = [Card(front="What is (1)?", back="mouth", mask=kept)]
    revised = [
        Card(front="What is (1)?", back="mouth", mask=Mask(page=1, n=1, box=[1, 1, 2, 2])),  # the model's copy
        Card(front="What is (2)?", back="title", mask=Mask(page=1, n=1, box=[100, 50, 300, 100])),  # added, n taken
        Card(front="le chat", back="el gato"),
    ]
    llm._keep_masks(revised, before, [(1000, 500)], "pixels")
    assert revised[0].mask == kept  # an existing mask never moves
    added = revised[1].mask
    assert (added.page, added.n) == (1, 2)  # the next free number
    m = diagrams.PADDING
    assert added.box == pytest.approx([0.1 - m, 0.1 - 2 * m, 0.3 + m, 0.2 + 2 * m])
    assert revised[2].mask is None


def test_correction_tells_the_model_how_to_place_a_label():
    from app.models import Deck

    empty = Deck(deck="D", cards=[])
    text = llm._revision_text("p", empty, "add the title", "en", 1, [(800, 600)], "pixels", {1: [1, 2]})
    assert "photo 1: [1, 2]" in text and "photo 1: 800x600" in text and diagrams.FORMATS["pixels"] in text
    assert "mask" not in llm._revision_text("p", empty, "x", "en", 0)  # no photo, no diagram
