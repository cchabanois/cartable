# Cartable

> From school bag to flashcards: snap a lesson, get an Anki deck.

Take photos of a lesson with your phone. A vision AI reads the pages and drafts Anki cards. You review and fix them, then send them straight into Anki or download a `.apkg`.

Cartable was built for learning languages (French → Spanish vocabulary and sentences, with audio), but a prompt can ask for any kind of question/answer card.

- **Phone first**: a web page, not an app. It opens the phone's own camera, and you can add it to the home screen.
- **Several pages per lesson**, with a saved prompt that says what to extract ("one word per card, gender and plural in the notes").
- **Review screen**: edit, delete or add cards, or ask the AI to fix them ("remove the card about the father", "you forgot the colours").
- **Audio**: the back of each card is read aloud with [edge-tts](https://github.com/rany2/edge-tts) and embedded in the deck, so it plays everywhere, even offline.
- **Straight into Anki**, or as a `.apkg`. Sending a corrected lesson again updates its cards instead of duplicating them.
- **Lessons are saved** (photos + cards), so you can reopen, fix and re-send them later.
- **One lesson list per Anki profile**, handy when each child has their own profile. The profile that creates a lesson owns it and can share it with the other profiles.
- **Choice of AI**: Gemini, Claude, or any OpenAI-compatible service (OpenAI, OpenRouter, Mistral, Ollama…).
- **English and French**, and adding a language takes a single file.

## Two ways to run it

### As an Anki add-on (recommended)

Cartable starts and stops with Anki desktop and writes cards directly into the open profile. You don't need AnkiConnect.

1. Build the add-on: `python3 tools/build_addon.py` → `dist/cartable.ankiaddon`.
2. Double-click the file (or *Tools → Add-ons → Install from file*) and restart Anki.
3. On first start, the add-on installs its Python dependencies with [uv](https://docs.astral.sh/uv/) into its `user_files/` folder, which add-on updates keep. Official Anki builds ship uv; otherwise uv must be installed on the system.
4. A QR code appears: scan it with the phone (same Wi-Fi) and add the page to the home screen.

Everything else is in the **Tools → Cartable** menu: open, open on the phone (QR code), settings, server status, restart and log.

- **Settings** (AI provider, API keys…) only open on the computer, without a password. Phones are refused, so API keys never travel over the Wi-Fi and children can't change them.
- The server listens on port 8000 by default. The port and other options are in the add-on's config (*Tools → Add-ons → Cartable → Config*).

### Standalone

Cartable runs on its own, even when Anki is closed. The `.apkg` download always works. "Add to Anki" needs Anki desktop open with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0
```

Then open http://localhost:8000, or `http://<computer-ip>:8000` from the phone on the same Wi-Fi.

Or with Docker:

```sh
docker build -t cartable .
docker run -p 8000:8000 -v cartable-data:/data --env-file .env cartable
```

The ⚙️ page (`/admin.html`) is reachable from the network in this mode. It is protected by an admin password, created on the first visit.

## Settings

The ⚙️ page chooses:

- the AI provider and model;
- API keys, which are stored in `data/settings.json` (readable only by the server's user) and never sent back to the browser;
- the voice speed;
- AnkiConnect and syncing options;
- whether a profile may see the other profiles' private lessons.

Environment variables (see `.env.example`) provide defaults. Whatever is saved on the ⚙️ page wins.

### AI providers

| Provider | Notes |
|---|---|
| **Gemini** (default) | Official `google-genai` SDK. A free [AI Studio](https://aistudio.google.com/) key is enough to start. Falls back to other models when the main one is overloaded. |
| **Claude** | Official `anthropic` SDK. |
| **OpenAI-compatible** | Presets for OpenAI and OpenRouter (one key for GPT, Claude, Gemini, Mistral…), or any other URL. Each service keeps its own key. *Load models* lists only models that accept images (and structured output, when the service says so). *Test* sends a small image to check that the model can read it and answer in JSON. |
| **Demo** (`fake`) | Canned cards, no key and no cost, for working on the interface. |

The output format is always forced with a JSON schema. The user's prompt only says *what* to extract.

Local models (Ollama, LM Studio) work through "Other", but they read handwritten pages much less reliably.

## Data

There is no database. Everything is plain files under `data/` (or `CARTABLE_DATA`), so backing up Cartable means copying that folder.

```
data/
  settings.json                              settings and API keys
  prompts.json                               saved prompts
  lessons/
    2026-09-28-espagnol-lecon-5-la-famille/  one folder per lesson
      lesson.json                            deck, cards, prompt, voice, owner, dates
      page-1.jpg, page-2.jpg                 photos
      audio/la-madre-3f2a1c9e.mp3            card audio (included in the .apkg)
  cache/tts/                                 voice previews, safe to delete
```

JSON files are written to a temporary file and then renamed, so a crash never leaves a half-written file.

In the add-on, `data/` lives in the add-on's `user_files/` folder.

## Languages

The interface follows the page's language picker, then Anki's language (in the add-on), then the browser's, and falls back to English.

**To add a language**, copy `static/i18n/en.json` to `static/i18n/<code>.json` (e.g. `es.json`, `pt-br.json`) and translate the values. That single file covers:

- the interface;
- error messages;
- the default prompts;
- the add-on's menu.

A test checks that every language has exactly the same keys as English.

The server never builds sentences: its errors are codes (`llm.overloaded`, `lesson.not_owner`…) that the page translates. Instructions sent to the AI are in English, and the user's prompt decides the language of the cards.

## Development

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .   # lint and formatting (ruff.toml)
```

To develop the add-on against this checkout, link it into Anki's add-ons folder and restart Anki:

```sh
ln -s "$PWD/anki_addon" ~/.local/share/Anki2/addons21/cartable
```

The add-on then runs the server from this repository, with its `.venv` and its `data/` folder.

Stack:

- back end: FastAPI + Pydantic;
- front end: Alpine.js, with no build step;
- decks: [genanki](https://github.com/kerrickstaley/genanki), with stable GUIDs so re-importing updates cards;
- QR codes: [segno](https://github.com/heuer/segno).

| Path | Content |
|---|---|
| `app/main.py` | FastAPI routes and static files |
| `app/llm.py` | card extraction and AI correction (Gemini, Claude, OpenAI-compatible, fake) |
| `app/lessons.py` | saved lessons, one folder each |
| `app/anki.py` | `.apkg` builder (genanki, audio, reverse cards) |
| `app/ankiconnect.py` | direct send through AnkiConnect or the add-on's bridge |
| `app/tts.py` | edge-tts audio |
| `app/prompts.py` | saved prompts |
| `app/settings.py` | settings, API keys, admin password |
| `app/i18n.py`, `static/i18n.js`, `static/i18n/` | languages |
| `app/errors.py` | errors as translatable codes |
| `app/storage.py` | data folder, atomic JSON writes, readable file names |
| `static/` | the phone page and the settings page |
| `anki_addon/` | the Anki add-on: server launcher and an AnkiConnect-compatible bridge |
| `tools/build_addon.py` | builds `dist/cartable.ankiaddon` |

## Good to know

- **Always review the cards.** Even good models misread a word now and then.
- **Children's schoolwork is private data.** Photos go to the AI provider you choose. A local model keeps them at home, at the cost of accuracy.
- **Plain HTTP on the local network.** The camera works over plain HTTP, and the page can be added to the home screen as a shortcut. A fully installed app would need HTTPS.
- **Never expose AnkiConnect or Cartable to the Internet.** Keep them on your local network.
- **edge-tts is unofficial.** Microsoft could shut it down.

## License

[AGPL-3.0](LICENSE)
