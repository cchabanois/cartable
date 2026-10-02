"""The pages in a real browser (Playwright, headless Chromium): what the API tests
can't see — the page saving as it goes, generating again and undoing, deleting a
lesson, the cloze preview, the settings' order.

Skipped unless Playwright is installed: `pip install -r requirements-page.txt`, then
`python -m playwright install chromium` (or CARTABLE_TEST_CHROMIUM=/usr/bin/chromium
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

from app import tts  # noqa: E402
from app.main import app  # noqa: E402

FRONT_PROMPT = "Une carte par mot de la famille"  # demo mode: Spanish family words
CLOZE_PROMPT = "Texte à trous sur la Révolution"  # demo mode: sentences with gaps
SAVED = re.compile(r"\bsaved\b")  # the save pill once the server has the lesson


async def fake_synthesize(text, voice, path):
    path.write_bytes(b"ID3 fake mp3")


@pytest.fixture(scope="session")
def server():
    """Cartable on a free port, in this process: the tests' settings apply to it."""
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
        executable = os.environ.get("CARTABLE_TEST_CHROMIUM")
        browser = p.chromium.launch(executable_path=executable) if executable else p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser, server, tmp_path, monkeypatch):
    """A phone-sized page in French, on fresh data and the demo AI (no key, no cost)."""
    monkeypatch.setenv("CARTABLE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CARTABLE_LLM", "fake")
    monkeypatch.setenv("CARTABLE_ANKICONNECT_URL", "http://127.0.0.1:1")  # Anki closed
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
    page.get_by_text("OpenRouter", exact=True).click()
    sync_api.expect(page.get_by_text("Clé API OpenRouter")).to_be_visible()
    sync_api.expect(address).to_be_hidden()
    page.get_by_text("Autre service compatible OpenAI").click()
    sync_api.expect(address).to_be_visible()  # only this one has an address
