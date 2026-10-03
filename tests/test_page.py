"""The pages in a real browser (Playwright, headless Chromium): what the API tests
can't see — the page saving as it goes, generating again and undoing, deleting a
lesson, the cloze preview, the settings' order.

Skipped unless Playwright is installed: `pip install -r requirements-page.txt`, then
`python -m playwright install chromium` (or NOTOSAURUS_TEST_CHROMIUM=/usr/bin/chromium
to use the system's). The pages load Alpine.js and KaTeX from a CDN: network needed.
"""

import os
import re
import socket
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from app import storage, tts  # noqa: E402
from app.main import app  # noqa: E402

FRONT_PROMPT = "Une carte par mot de la famille"  # demo mode: Spanish family words
CLOZE_PROMPT = "Texte à trous sur la Révolution"  # demo mode: sentences with gaps
SAVED = re.compile(r"\bsaved\b")  # the save pill once the server has the lesson


async def fake_synthesize(text, voice, path):
    path.write_bytes(b"ID3 fake mp3")


@pytest.fixture(scope="session")
def server():
    """Notosaurus on a free port, in this process: the tests' settings apply to it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    runner = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    for _ in range(100):
        if runner.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    runner.should_exit = True
    thread.join(5)


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        executable = os.environ.get("NOTOSAURUS_TEST_CHROMIUM")
        browser = p.chromium.launch(executable_path=executable) if executable else p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser, server, tmp_path, monkeypatch):
    """A phone-sized page in French, on fresh data and the demo AI (no key, no cost)."""
    monkeypatch.setenv("NOTOSAURUS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NOTOSAURUS_LLM", "fake")
    monkeypatch.setenv("NOTOSAURUS_ANKICONNECT_URL", "http://127.0.0.1:1")  # Anki closed
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)  # the same on every computer: no key (.env)
    monkeypatch.setattr(tts, "_synthesize", fake_synthesize)
    context = browser.new_context(locale="fr-FR", viewport={"width": 390, "height": 844}, base_url=server)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    yield page
    context.close()
    assert not errors, errors


def lessons(page) -> list[dict]:
    return page.request.get("/api/lessons").json()


def lesson(page, id: str) -> dict:
    return page.request.get(f"/api/lessons/{id}").json()


def generate_free(page, prompt: str) -> None:
    """A lesson from a prompt written for this time (no photo)."""
    page.goto("/")
    page.get_by_role("radio", name="✏️ Libre").click()
    page.locator("textarea[x-ref=promptText]").fill(prompt)
    page.get_by_role("button", name="✨ Générer à partir de la consigne seule").click()
    page.locator(".flash").first.wait_for()


def test_generate_then_edit_saves_by_itself(page):
    generate_free(page, FRONT_PROMPT)
    assert page.locator(".flash").count() == 6
    (summary,) = lessons(page)
    assert lesson(page, summary["id"])["prompt"] == FRONT_PROMPT  # kept with the lesson…
    assert not [p for p in page.request.get("/api/prompts").json() if not p["builtin"]]  # …not saved as a prompt

    page.locator(".flash input.back").first.fill("la mamá")
    sync_api.expect(page.locator(".save-pill")).to_have_class(SAVED)
    assert lesson(page, summary["id"])["cards"][0]["back"] == "la mamá"


def test_generate_again_in_place_then_undo(page):
    generate_free(page, FRONT_PROMPT)
    (first,) = lessons(page)
    page.locator("textarea[x-ref=promptText]").fill(CLOZE_PROMPT)
    page.get_by_role("button", name="✨ Régénérer").click()
    sync_api.expect(page.locator(".math-preview .gap").first).to_be_visible()  # the gaps, numbered

    (again,) = lessons(page)  # the same lesson, its cards replaced
    assert again["id"] == first["id"]
    assert all("{{c" in c["front"] for c in lesson(page, first["id"])["cards"])
    assert page.locator(".math-preview .gap sup").first.inner_text() == "1"

    page.get_by_role("button", name="↩ Annuler").click()
    sync_api.expect(page.locator(".save-pill")).to_have_class(SAVED)
    undone = lesson(page, first["id"])
    assert undone["cards"][0]["front"] == "la mère" and undone["prompt"] == FRONT_PROMPT


def test_figures_drawn_after_the_cards(page):
    generate_free(page, "Le triangle rectangle (géométrie)")  # demo mode: two figures to draw
    figure = page.locator(".card-picture.figure img").first
    sync_api.expect(figure).to_be_visible()
    assert page.evaluate("img => img.naturalWidth", figure.element_handle()) == 400  # the SVG, at its size
    (summary,) = lessons(page)
    assert all(c["picture"].endswith(".svg") for c in lesson(page, summary["id"])["cards"])
    # The tangent's figure belongs to its answer: shown under it, as in Anki
    tangent = page.locator(".flash", has_text="tangente")
    sync_api.expect(tangent.locator(".card-picture.on-back img")).to_be_visible()
    sync_api.expect(tangent.locator(".card-picture:not(.on-back)")).to_be_hidden()


def test_delete_the_open_lesson(page):
    generate_free(page, FRONT_PROMPT)
    page.locator(".review-head").get_by_role("button", name="Supprimer la leçon").click()
    sheet = page.locator(".sheet.top")
    sync_api.expect(sheet).to_contain_text("Anki n'est pas ouvert")  # Anki closed: the lesson alone
    sheet.get_by_role("button", name="Supprimer la leçon").click()
    sync_api.expect(page.locator(".flash")).to_have_count(0)
    assert lessons(page) == []


def test_settings_show_what_the_service_needs(page):
    page.goto("/admin.html")
    page.locator("input[autocomplete=new-password]").first.fill("secret")
    page.locator("input[autocomplete=new-password]").nth(1).fill("secret")
    page.get_by_role("button", name="Créer et continuer").click()
    address = page.get_by_placeholder("http://localhost:11434/v1")
    page.locator(".options .option", has_text="OpenRouter").click()
    sync_api.expect(page.get_by_text("Clé API OpenRouter")).to_be_visible()
    sync_api.expect(address).to_be_hidden()
    # Its suggested models are buttons; the field suggests every model as one types
    listed = {
        "loadedModels": ["~google/gemini-flash-latest", "a/one", "b/two"],
        "modelAliases": ["~google/gemini-flash-latest"],
        "recommendedModel": "~google/gemini-flash-latest",
    }
    page.evaluate("listed => Object.assign(Alpine.$data(document.querySelector('[x-data]')), listed)", listed)
    sync_api.expect(page.get_by_role("button", name="⭐ gemini-flash")).to_be_visible()
    assert page.locator("#model-suggestions option").count() == 3
    sync_api.expect(page.get_by_text("Ou tape le nom d'un des 3 modèles")).to_be_visible()
    page.locator(".options .option", has_text="Autre service compatible OpenAI").click()
    sync_api.expect(address).to_be_visible()  # only this one has an address
    # It can't draw, and no other service has a key: said where the pictures are set
    sync_api.expect(page.locator(".picture-summary")).to_contain_text(
        "ne dessine pas, et aucun autre service n'a de clé"
    )
    # In the order things are set: the service, its access, then the model
    titles = [" ".join(t.split()) for t in page.locator("section.panel h2:visible").all_inner_texts()]
    assert titles[:4] == ["1 Service d'IA", "2 Accès", "3 Modèle", "4 Images des cartes"]
    # The settings save themselves a moment later: done before the next test's data
    page.wait_for_function("Alpine.$data(document.querySelector('[x-data]')).saveState === 'saved'")


def png(color: str) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (600, 800), color).save(out, "PNG")
    return out.getvalue()


def test_photos_and_pictures_seen_in_full(page):
    page.goto("/")
    page.locator("input[type=file][multiple]").set_input_files(
        [{"name": f"page-{n}.png", "mimeType": "image/png", "buffer": png(c)} for n, c in ((1, "red"), (2, "blue"))]
    )
    page.locator(".thumb img").first.click()
    viewer = page.locator(".viewer")
    sync_api.expect(viewer).to_be_visible()
    sync_api.expect(viewer.locator(".viewer-count")).to_have_text("1 / 2")
    viewer.get_by_role("button", name="Photo suivante").click()
    sync_api.expect(viewer.locator(".viewer-count")).to_have_text("2 / 2")
    viewer.locator(".viewer-frame img").click()  # its real size, to pan
    sync_api.expect(viewer.locator(".viewer-frame")).to_have_class(re.compile(r"\bzoomed\b"))
    page.keyboard.press("Escape")
    sync_api.expect(viewer).to_be_hidden()

    # A card's figure too, alone: no arrows
    generate_free(page, "Le triangle rectangle (géométrie)")
    page.locator(".card-picture img").first.click()
    sync_api.expect(viewer).to_be_visible()
    sync_api.expect(viewer.locator(".viewer-nav")).to_have_count(0)


def test_fun_facts_switch(page):
    page.goto("/")
    switch = page.get_by_text("💡 Ajouter des « Le savais-tu ? »")
    assert not page.locator(".switch.fun-facts input").is_checked()  # off by default
    switch.click()
    page.reload()  # kept on the device
    assert page.locator(".switch.fun-facts input").is_checked()
    generate_free(page, FRONT_PROMPT)
    fact = page.locator(".flash .fun-fact input").first
    sync_api.expect(fact).to_be_visible()
    assert fact.input_value().startswith("Le savais-tu")
    sync_api.expect(page.locator(".flash .fun-fact:visible")).to_have_count(1)  # the others have none


def test_multiple_choice_wrong_answers_edited(page):
    generate_free(page, "QCM sur la Révolution")
    first = page.locator(".flash").first
    sync_api.expect(first.locator(".choice input")).to_have_count(3)
    assert first.locator("input.back").get_attribute("placeholder") == "Bonne réponse"
    first.get_by_role("button", name="+ mauvaise réponse").click()
    first.locator(".choice input").nth(3).fill("1830")
    first.get_by_role("button", name="Retirer cette réponse").first.click()  # 1715 goes
    sync_api.expect(page.locator(".save-pill")).to_have_class(SAVED)
    (summary,) = lessons(page)
    assert lesson(page, summary["id"])["cards"][0]["choices"] == ["1799", "1804", "1830"]


@pytest.fixture
def clock(monkeypatch):
    """A second more at each save: the lessons made in a test are in order (saved to
    the second, they would all be at the same time)."""
    ticks = iter(range(3600))
    monkeypatch.setattr(storage, "now", lambda: "2026-10-03T10:{:02d}:{:02d}".format(*divmod(next(ticks), 60)))


def test_lessons_grouped_by_subject(page, clock):
    page.goto("/")
    for deck in ("Espagnol::a", "Anglais::Leçon 1", "anglais::Leçon 2::Phrases"):  # the most recent last
        made = page.request.post("/api/extract", multipart={"prompt": FRONT_PROMPT}).json()
        page.request.put(f"/api/lessons/{made['id']}", data={"deck": deck, "cards": made["cards"]})
    page.reload()
    page.get_by_role("button", name="Leçons").click()
    groups = page.locator(".lesson-group")
    sync_api.expect(groups).to_have_count(2)
    titles = [" ".join(h.split()).casefold() for h in groups.locator("h3").all_inner_texts()]
    assert titles == ["anglais 2", "espagnol 1"]  # "anglais", "Anglais": the same deck for Anki
    assert groups.first.locator(".lesson strong").all_inner_texts() == ["Leçon 2 › Phrases", "Leçon 1"]


def test_many_lessons_recent_folded_and_searched(page, clock):
    page.goto("/")
    decks = [f"Espagnol::Leçon {n}" for n in range(1, 7)] + ["Maths::Fractions", "Maths::Pythagore", "Anglais::Unit 1"]
    for deck in [*decks, "Anglais::Unit 2"]:  # 10 lessons, the most recent last
        made = page.request.post("/api/extract", multipart={"prompt": FRONT_PROMPT}).json()
        page.request.put(f"/api/lessons/{made['id']}", data={"deck": deck, "cards": made["cards"]})
    page.reload()
    page.get_by_role("button", name="Leçons").click()
    sheet = page.locator(".lesson-list")
    heads = sheet.locator(".group-head")
    sync_api.expect(heads).to_have_count(4)  # the recent ones, then the subjects
    assert [" ".join(h.split()).casefold() for h in heads.all_inner_texts()] == [
        "récentes",
        "▸ anglais 2",
        "▸ maths 2",
        "▸ espagnol 6",
    ]
    recent = sheet.locator(".lesson-group").first.locator(".lesson strong")
    assert recent.all_inner_texts() == ["Anglais › Unit 2", "Anglais › Unit 1", "Maths › Pythagore"]
    sync_api.expect(sheet.locator(".lesson:visible")).to_have_count(3)  # the subjects folded

    heads.nth(3).click()  # Espagnol, opened: still open next time
    sync_api.expect(sheet.locator(".lesson:visible")).to_have_count(9)
    page.reload()
    page.get_by_role("button", name="Leçons").click()
    sync_api.expect(sheet.locator(".lesson:visible")).to_have_count(9)

    page.get_by_placeholder("🔍 Chercher une leçon…").fill("lecon 4")  # no accent, no case
    sync_api.expect(sheet.locator(".lesson:visible strong")).to_have_text(["Espagnol › Leçon 4"])
    page.get_by_placeholder("🔍 Chercher une leçon…").fill("chimie")
    sync_api.expect(page.get_by_text("Aucune leçon ne correspond.")).to_be_visible()


def test_admin_lessons_by_owner_then_subject(page, clock):
    page.goto("/admin.html")
    page.locator("input[autocomplete=new-password]").first.fill("secret")
    page.locator("input[autocomplete=new-password]").nth(1).fill("secret")
    page.get_by_role("button", name="Créer et continuer").click()
    page.wait_for_function("Alpine.$data(document.querySelector('[x-data]')).saveState === 'saved'")
    admin = {"X-Admin-Password": "secret"}
    for deck, owner in (
        ("Anglais::Leçon 1", "Léa"),
        ("Maths::Fractions", "Paul"),
        ("Anglais::Leçon 2", "Léa"),
        ("SVT", ""),
    ):
        made = page.request.post("/api/extract", multipart={"prompt": FRONT_PROMPT}).json()
        page.request.put(f"/api/lessons/{made['id']}", data={"deck": deck, "cards": made["cards"]})
        page.request.put(f"/api/admin/lessons/{made['id']}", headers=admin, data={"owner": owner})
    page.reload()
    owners = page.locator(".owner-group > summary strong")
    sync_api.expect(owners).to_have_text(["👤 Léa", "👤 Paul", "Personne (commune à tous)"])  # nobody's last
    lea = page.locator(".owner-group").first
    sync_api.expect(lea.locator("> summary small")).to_contain_text("2 leçons")
    sync_api.expect(page.locator(".admin-lesson-head:visible")).to_have_count(0)  # folded: the owners alone
    lea.locator("> summary").click()
    sync_api.expect(page.locator(".admin-lesson-head:visible")).to_have_count(2)
    sync_api.expect(lea.locator(".subject-group h3")).to_have_text(["Anglais"])
    sync_api.expect(lea.locator(".admin-lesson-head strong")).to_have_text(["Leçon 2", "Leçon 1"])
    sync_api.expect(page.locator(".owner-group").last.locator(".admin-lesson-head strong")).to_have_text(["SVT"])
