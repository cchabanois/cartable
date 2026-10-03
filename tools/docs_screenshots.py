"""Screenshots of Notosaurus for the user documentation, in each language.

Two steps:

    python tools/docs_screenshots.py generate --lang fr --settings data/settings.json
        Makes the demo lessons of a language once, with a real AI (a few cents): it
        photographs docs/demo/inputs/*.<lang>.html, generates the cards, applies a
        correction, and stores the lessons in docs/demo/<lang>/lessons/. The API keys
        come from --settings and are never written to the repository.

    python tools/docs_screenshots.py shoot --lang fr
        Runs Notosaurus on these lessons (no AI call, a fake API key, a fake Anki with
        the profiles Léa and Paul) and takes the screenshots into
        docs/src/assets/screenshots/<lang>/. Run it again whenever the interface changes.

Needs Playwright (requirements-page.txt); NOTOSAURUS_TEST_CHROMIUM picks a system Chromium.
"""

import argparse
import contextlib
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "docs" / "demo"
SHOTS = ROOT / "docs" / "src" / "assets" / "screenshots"
PROFILES = ["Léa", "Paul"]
APP = "document.querySelector('[x-data]')._x_dataStack[0]"

# The lessons made by "generate": input page, Notosaurus prompt, deck name, correction
LESSONS = {
    "en": [
        (
            "vocab",
            "notosaurus:vocabulary",
            "Spanish::Unit 3 - At school",
            "Add the plural on the back, like: la regla / las reglas",
        ),
        ("plant", "notosaurus:diagram", "Science::Parts of a plant", None),
    ],
    "fr": [
        (
            "vocab",
            "notosaurus:vocabulary",
            "Espagnol::Unité 3 - À l'école",
            "Ajoute le pluriel au verso, comme : la regla / las reglas",
        ),
        ("plant", "notosaurus:diagram", "Sciences::Les parties d'une plante", None),
    ],
}
# Shown in the settings screenshots
INSTRUCTIONS = {
    "en": (
        "Spanish from Spain; always the article with nouns",
        {"Léa": "in year 8", "Paul": "in year 5; short answers"},
    ),
    "fr": (
        "Espagnol d'Espagne ; toujours l'article avec les noms",
        {"Léa": "en 5ᵉ", "Paul": "en CM2 ; réponses courtes"},
    ),
}
CORRECTION_TYPED = {
    "en": "You forgot: el libro = the book",
    "fr": "Tu as oublié : el libro = le livre",
}


# --- A fake Anki: two profiles, every write accepted ---------------------------------


def fake_anki(port: int, profiles: list[str]) -> ThreadingHTTPServer:
    ids = itertools.count(1)
    results = {
        "version": 6,
        "getActiveProfile": profiles[0],
        "getProfiles": profiles,
        "modelNames": [],
        "findNotes": [],
        "notesInfo": [],
        "deckNames": ["Default"],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            action = body.get("action")
            if action == "multi":
                result = [{"result": results.get(a["action"]), "error": None} for a in body["params"]["actions"]]
            else:
                result = results.get(action, next(ids) if action in ("addNote", "createDeck") else None)
            data = json.dumps({"result": result, "error": None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def notosaurus(data: Path):
    """The Notosaurus server on `data`, as in the Anki add-on (settings open on this computer)."""
    anki_port, port = free_port(), free_port()
    anki = fake_anki(anki_port, PROFILES)
    env = {
        **os.environ,
        "NOTOSAURUS_DATA": str(data),
        "NOTOSAURUS_EMBEDDED": "1",
        "NOTOSAURUS_ANKICONNECT_URL": f"http://127.0.0.1:{anki_port}",
        "NOTOSAURUS_ANKICONNECT_KEY": "",
    }
    python = ROOT / ".venv" / "bin" / "python"
    proc = subprocess.Popen(
        [
            str(python if python.exists() else sys.executable),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/"
    try:
        for _ in range(100):
            with contextlib.suppress(OSError):
                urllib.request.urlopen(url, timeout=1)
                break
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait()
        anki.shutdown()


@contextlib.contextmanager
def browser():
    with sync_playwright() as p:
        chromium = os.environ.get("NOTOSAURUS_TEST_CHROMIUM")
        b = p.chromium.launch(executable_path=chromium) if chromium else p.chromium.launch()
        try:
            yield b
        finally:
            b.close()


def phone(b, lang: str):
    ctx = b.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        locale=lang,
        color_scheme="light",
    )
    ctx.add_init_script(f"localStorage.setItem('notosaurus.lang', {json.dumps(lang)})")
    return ctx.new_page()


def wait_for(page, condition: str, timeout: float = 420):
    page.wait_for_function(condition, timeout=timeout * 1000, polling=500)


# --- generate ------------------------------------------------------------------------


def generate(lang: str, settings_file: Path) -> None:
    out = DEMO / lang / "lessons"
    with tempfile.TemporaryDirectory() as tmp, browser() as b:
        tmp = Path(tmp)
        data = tmp / "data"
        data.mkdir()
        shutil.copy(settings_file, data / "settings.json")  # real keys, temporary folder only
        page = b.new_page(viewport={"width": 1200, "height": 1600})
        for name, *_ in LESSONS[lang]:  # the "photos"
            page.goto((DEMO / "inputs" / f"{name}.{lang}.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.evaluate("document.fonts.ready")
            page.screenshot(path=tmp / f"{name}.jpg", type="jpeg", quality=88)
        with notosaurus(data) as url:
            page = phone(b, lang)
            page.goto(url)
            page.wait_for_load_state("networkidle")
            for name, prompt, deck, correction in LESSONS[lang]:
                page.evaluate(f"{APP}.newLesson()")
                page.locator("input[type=file][multiple]").first.set_input_files(tmp / f"{name}.jpg")
                wait_for(page, f"{APP}.photos.length === 1", timeout=30)  # read and resized by the page
                page.evaluate(f"{APP}.choose({json.dumps(prompt)})")
                page.evaluate(f"{APP}.extract()")
                wait_for(page, f"!{APP}.loading && {APP}.cards.length > 0")
                if page.evaluate(f"{APP}.photos.length") != 1:
                    raise RuntimeError(f"{lang}/{name}: the lesson has no photo")
                page.evaluate(f"d => {{ {APP}.deck = d; }}", deck)
                page.evaluate(f"{APP}.saveNow()")
                if correction:
                    page.evaluate(f"t => {{ {APP}.revision.text = t; {APP}.revise(); }}", correction)
                    wait_for(page, f"!{APP}.revision.busy && !!{APP}.revision.summary")
                page.evaluate(f"{APP}.saveNow()")
                time.sleep(1)
                print(f"{lang}: {name} → {page.evaluate(f'{APP}.cards.length')} cards", flush=True)
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(data / "lessons", out, ignore=shutil.ignore_patterns("audio"))
        for lesson in out.glob("*/lesson.json"):  # the demo profile, whoever ran it
            content = json.loads(lesson.read_text(encoding="utf-8"))
            content["owner"] = PROFILES[0]
            lesson.write_text(json.dumps(content, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Lessons saved in {out.relative_to(ROOT)}")


# --- shoot ---------------------------------------------------------------------------


def demo_data(lang: str, tmp: Path) -> Path:
    data = tmp / "data"
    shutil.copytree(DEMO / lang / "lessons", data / "lessons")
    general, per_profile = INSTRUCTIONS[lang]
    settings = {
        "llm": "gemini",
        "gemini_api_key": "AIzaSy-notosaurus-demo-key-1234",
        "instructions": general,
        "profile_instructions": per_profile,
    }
    (data / "settings.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    return data


def scroll_to(page, selector: str, offset: int = 70) -> None:
    page.evaluate(f"""() => {{ const el = document.querySelector({json.dumps(selector)});
        window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - {offset}); }}""")
    time.sleep(0.4)


def lesson_id(page, prefix: str) -> str:
    return page.evaluate(f"p => {APP}.lessons.find(l => l.deck.startsWith(p)).id", prefix)


def shoot(lang: str) -> None:
    out = SHOTS / lang
    out.mkdir(parents=True, exist_ok=True)
    vocab_deck, plant_deck = (d for _, _, d, _ in LESSONS[lang])
    with tempfile.TemporaryDirectory() as tmp, browser() as b, notosaurus(demo_data(lang, Path(tmp))) as url:
        page = phone(b, lang)

        def shot(name):
            time.sleep(0.5)
            page.screenshot(path=out / f"{name}.png")
            print(f"{lang}/{name}.png", flush=True)

        page.goto(url)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        shot("home")

        vocab_photo = next(
            f.parent / "page-1.jpg"
            for f in (DEMO / lang / "lessons").glob("*/lesson.json")
            if json.loads(f.read_text(encoding="utf-8"))["deck"] == vocab_deck
        )
        page.locator("input[type=file][multiple]").first.set_input_files(vocab_photo)
        wait_for(page, f"{APP}.photos.length === 1", timeout=30)
        page.evaluate(f"{APP}.choose('notosaurus:vocabulary')")
        scroll_to(page, "section.panel:has(input[type=file])", 70)
        shot("instructions")
        page.evaluate(f"{APP}.clearPhotos()")

        page.evaluate(f"{APP}.lessonsOpen = true")
        time.sleep(0.8)
        shot("lessons")
        page.evaluate(f"{APP}.lessonsOpen = false")

        page.evaluate(f"id => {APP}.openLesson(id)", lesson_id(page, vocab_deck))
        page.wait_for_load_state("networkidle")
        scroll_to(page, "section.review", 10)
        shot("review")
        page.locator("form.revise textarea").fill(CORRECTION_TYPED[lang])
        scroll_to(page, "form.revise", 120)
        shot("correction")

        page.evaluate(f"id => {APP}.openLesson(id)", lesson_id(page, plant_deck))
        page.wait_for_load_state("networkidle")
        scroll_to(page, "section.review", 10)
        page.evaluate("window.scrollBy(0, 300)")
        shot("diagram")

        page.goto(url + "admin.html")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        shot("settings-ai")
        scroll_to(page, "section.phones", 80)
        shot("settings-phones")
        page.evaluate("""() => {
            const h = [...document.querySelectorAll('section.panel h2')].find(e => e.textContent.includes('📝'));
            window.scrollTo(0, h.getBoundingClientRect().top + scrollY - 90);
        }""")
        time.sleep(0.4)
        shot("settings-instructions")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=("generate", "shoot"))
    parser.add_argument("--lang", action="append", help="language code (several allowed); default: all")
    parser.add_argument(
        "--settings",
        type=Path,
        default=ROOT / "data" / "settings.json",
        help="settings with the API keys, for generate",
    )
    args = parser.parse_args()
    for lang in args.lang or list(LESSONS):
        generate(lang, args.settings) if args.step == "generate" else shoot(lang)


if __name__ == "__main__":
    main()
