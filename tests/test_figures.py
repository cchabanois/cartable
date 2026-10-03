"""Figures on cards: SVG drawn by the AI, cleaned, saved and sent like the pictures."""

import json
import sqlite3
import zipfile

import pytest

from app import figures, lessons

TRAPPED = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" width="9999" onload="alert(1)">
<script>alert(1)</script><rect width="400" height="300" fill="white"/>
<a href="javascript:alert(1)"><text x="10" y="20">lien</text></a>
<image href="http://evil.test/x.png"/><foreignObject><div>x</div></foreignObject>
<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="5" refY="5" orient="auto">
<path d="M0,0 L10,5 L0,10 z"/></marker></defs>
<line x1="0" y1="0" x2="100" y2="0" stroke="black" marker-end="url(#arrow)" style="stroke-width:2;fill:url(http://evil.test)"/>
<text x="50" y="50" transform="rotate(-30 50 50)" font-family="sans-serif" onclick="x()">hypoténuse</text>
<rect fill="url(http://evil.test/a)" transform="translate(1,2) expression(alert(1))"/><animate attributeName="x"/>
</svg>"""


def test_clean_keeps_only_shapes_and_text():
    svg = figures.clean(TRAPPED)
    for gone in (
        "script",
        "onload",
        "javascript",
        "<image",
        "foreignObject",
        "evil.test",
        "expression",
        "animate",
        "onclick",
    ):
        assert gone not in svg, gone
    for kept in ('transform="rotate(-30 50 50)"', 'marker-end="url(#arrow)"', "hypoténuse", 'style="stroke-width:2"'):
        assert kept in svg, kept
    assert 'width="400" height="300"' in svg  # sized from its viewBox: an <img> shows it at its size


@pytest.mark.parametrize(
    "bad",
    [
        '<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>',  # no DTD, no entities
        "<html><body/></html>",
        "<svg",
        "<svg>" + "<g/>" * 60000 + "</svg>",  # too big
    ],
)
def test_clean_refuses(bad):
    with pytest.raises(figures.FigureError):
        figures.clean(bad)


def test_clean_gives_the_namespace():
    svg = figures.clean('<svg viewBox="0 0 200 100"><circle cx="50" cy="50" r="40"/></svg>')
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"') and "<circle" in svg


def test_figures_drawn_shown_and_exported(client, tmp_path):
    lesson = client.post("/api/extract", data={"prompt": "Le triangle rectangle (géométrie)"}).json()
    assert all(c["figure"] and not c["picture"] for c in lesson["cards"])
    res = client.post(f"/api/lessons/{lesson['id']}/pictures").json()
    assert res["failures"] == 0
    cards = res["lesson"]["cards"]
    assert all(c["picture"].endswith(".svg") for c in cards)

    # Served as an image that can't run anything, even opened on its own
    r = client.get(f"/api/lessons/{lesson['id']}/pictures/{cards[1]['picture']}")
    assert r.headers["content-type"] == "image/svg+xml"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "« c »" not in r.text and ">c<" in r.text  # the label from the description

    # In the package: the card with its figure
    body = {"deck": lesson["deck"], "cards": cards, "lesson_id": lesson["id"]}
    path = tmp_path / "out.apkg"
    path.write_bytes(client.post("/api/export", json=body).content)
    with zipfile.ZipFile(path) as z:
        z.extract("collection.anki2", tmp_path)
        media = json.loads(z.read("media"))
    assert {cards[0]["picture"], cards[1]["picture"]} <= set(media.values())
    conn = sqlite3.connect(tmp_path / "collection.anki2")
    fields = [f for (f,) in conn.execute("SELECT flds FROM notes")]
    assert any(f'<img src="{cards[0]["picture"]}">' in f for f in fields)


def test_redraw_a_figure_from_its_description(client):
    lesson = client.post("/api/extract", data={"prompt": "géométrie"}).json()
    card = lesson["cards"][0]
    url = f"/api/lessons/{lesson['id']}/cards/{card['id']}/picture/draw"
    res = client.post(url, json={"subject": "Un triangle, l'hypoténuse étiquetée « hypoténuse »"}).json()
    assert res["card"]["figure"].startswith("Un triangle") and res["card"]["picture"].endswith(".svg")
    svg = (lessons.folder(lesson["id"]) / "images" / res["card"]["picture"]).read_text()
    assert "hypoténuse" in svg
    # No picture any more: a text card again
    gone = client.delete(f"/api/lessons/{lesson['id']}/cards/{card['id']}/picture").json()["card"]
    assert (gone["figure"], gone["picture"]) == ("", "")


def test_a_figure_with_the_answer_goes_on_the_back(client, tmp_path):
    """ "What is a tangent?": the figure belongs to the answer, it shows with it only."""
    lesson = client.post("/api/extract", data={"prompt": "géométrie"}).json()
    cards = client.post(f"/api/lessons/{lesson['id']}/pictures").json()["lesson"]["cards"]
    tangent = next(c for c in cards if c["picture_on_back"])
    assert tangent["front"].startswith("Qu'est-ce qu'une tangente")
    body = {"deck": lesson["deck"], "cards": cards, "lesson_id": lesson["id"]}
    path = tmp_path / "out.apkg"
    path.write_bytes(client.post("/api/export", json=body).content)
    with zipfile.ZipFile(path) as z:
        z.extract("collection.anki2", tmp_path)
    conn = sqlite3.connect(tmp_path / "collection.anki2")
    models = json.loads(conn.execute("SELECT models FROM col").fetchone()[0]).values()
    on_back = next(m for m in models if m["name"].startswith("Cartable image au verso"))
    (template,) = on_back["tmpls"]
    assert "{{Picture}}" not in template["qfmt"] and "{{Picture}}" in template["afmt"]
    assert template["name"] == "Image"  # as on the front: a note moves between them keeping its card
