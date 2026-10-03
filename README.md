# Notosaurus

> From school bag to flashcards: snap a lesson, get an Anki deck.

Take photos of a lesson with your phone. A vision AI reads the pages and drafts Anki cards. You review and fix them, then send them straight into Anki or download a `.apkg`.

Notosaurus was built for learning languages (French → Spanish vocabulary and sentences, with audio), but a prompt can ask for any kind of question/answer card.

- **Phone first**: a web page, not an app. It opens the phone's own camera, and you can add it to the home screen.
- **Several pages per lesson**, with a saved prompt that says what to extract ("one word per card, gender and plural in the notes").
- **Review screen**: edit, delete or add cards, or ask the AI to fix them ("remove the card about the father", "you forgot the colours").
- **Diagrams**: ask for "one card per label" and the AI hides every label of a diagram behind a number; each card asks "What is (2)?" and shows the answer on the diagram. Masks can be moved and resized in the review. Gemini and GPT place them most precisely.
- **Cloze**: ask for "fill in the blanks" and each card is a sentence with gaps, `La Révolution française commence en {{c1::1789}} avec la prise de {{c2::la Bastille}}.`: Anki makes one card per gap number. The review shows the gaps, numbered.
- **Multiple choice and true/false**: ask for a quiz (QCM) or true/false statements: each card has its right answer and wrong ones, edited in the review. In Anki, the question shows the options (A, B, C…, in the same order every time), the answer marks the right one; plain HTML, the same on every Anki.
- **Formulas**: written in MathJax (`\(…\)`), which Anki displays; the review shows them drawn.
- **Spelling**: "Type the answer" makes Anki compare what the pupil types letter by letter; "Add a dictation" adds a card where the back is heard, then written.
- **Pictures**: with a prompt like "front: the picture of the word", an image model draws a picture for each card, through the cards' own service when it can draw (Gemini, OpenAI, OpenRouter), else another one chosen in ⚙️ (Claude and local models can't draw). From under a cent to a few US cents per picture.
- **Figures**: geometry and labelled figures (a right triangle with its hypotenuse, a circle and its radius, a measured rectangle…) are drawn as SVG by the cards' own AI, exact and with clean text, instead of an image model. They are cleaned (shapes and text only) and go to Anki like the pictures.
- **Audio**: the back of each card is read aloud with [edge-tts](https://github.com/rany2/edge-tts) and embedded in the deck, so it plays everywhere, even offline.
- **Straight into Anki**, or as a `.apkg`. Sending a corrected lesson again updates its cards instead of duplicating them.
- **Lessons are saved** (photos + cards), so you can reopen, fix and re-send them later.
- **One lesson list per Anki profile**, handy when each child has their own profile. The profile that creates a lesson owns it and can share it with the other profiles.
- **Choice of AI**: Gemini, Claude, or any OpenAI-compatible service (OpenAI, OpenRouter, Mistral, Ollama…).
- **English, French, Spanish, German, Italian and Portuguese (Brazil)**, and adding a language takes a single file.

## Two ways to run it

### As an Anki add-on (recommended)

Notosaurus starts and stops with Anki desktop and writes cards directly into the open profile. You don't need AnkiConnect.

1. Download `notosaurus-<version>.ankiaddon` from the [latest release](https://github.com/cchabanois/notosaurus/releases/latest) (to build it yourself, see [CONTRIBUTING.md](CONTRIBUTING.md)).
2. Double-click the file (or *Tools → Add-ons → Install from file*) and restart Anki.
3. On first start, the add-on asks before installing its components into `Anki2/notosaurus-runtime/`, next to the add-ons (about 300 MB, kept by add-on updates, removed with the add-on; outside the add-on's own folder, so that Anki can update it while Notosaurus runs, Windows included): [uv](https://docs.astral.sh/uv/), Python 3.13 and the Python libraries. It uses the uv shipped with Anki 25.07 to 26.05 or one already installed; otherwise it downloads a pinned uv release from GitHub and checks its SHA-256.
4. A QR code appears: scan it with the phone (same Wi-Fi) and add the page to the home screen.

Everything else is in the **Tools → Notosaurus** menu: open, open on the phone (QR code), settings, server status, restart and log.

- **Settings** (AI provider, API keys…) only open on the computer, without a password. Phones are refused, so API keys never travel over the Wi-Fi and children can't change them.
- The server listens on port 8000 by default. The port and other options are in the add-on's config (*Tools → Add-ons → Notosaurus → Config*).

### Standalone

Notosaurus runs on its own, even when Anki is closed. The `.apkg` download always works. "Add to Anki" needs Anki desktop open with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0
```

Then open http://localhost:8000 on the computer. For the phone (same Wi-Fi): ⚙️ → **Phones**, and scan the QR code.

Or with Docker:

```sh
docker build -t notosaurus .
docker run -p 8000:8000 -v notosaurus-data:/data --env-file .env -e NOTOSAURUS_PUBLIC_URL=http://<computer-ip>:8000 notosaurus
```

Inside Docker, Notosaurus can't see the computer's address: `NOTOSAURUS_PUBLIC_URL` puts it in the phones' QR code. The computer itself goes through Docker's network too: open ⚙️ → **Phones** and use the link under the QR code once.

The ⚙️ page (`/admin.html`) is reachable from the network in this mode. It is protected by an admin password, created on the first visit.

## Settings

The ⚙️ page chooses:

- the AI provider and model;
- API keys, which are stored in `data/settings.json` (readable only by the server's user) and never sent back to the browser;
- the voice speed;
- standing instructions for the AI, for every profile and for each Anki profile (e.g. "in year 8"), added to the prompt of each lesson;
- AnkiConnect and syncing options;
- whether a profile may see the other profiles' private lessons.

Environment variables (see `.env.example`) provide defaults. Whatever is saved on the ⚙️ page wins.

### AI providers

| Provider | Notes |
|---|---|
| **Gemini** (default) | Official `google-genai` SDK. A free [AI Studio](https://aistudio.google.com/) key is enough to start. Falls back to other models when the main one is overloaded. |
| **Claude** | Official `anthropic` SDK. |
| **OpenAI** | GPT models, with an OpenAI key. |
| **OpenRouter** | One key for Gemini, Claude, GPT, Mistral…; the exact cost of each call. ⚙️ suggests its `~…-latest` models (always the latest version of a family), the latest Gemini Flash by default. |
| **Other OpenAI-compatible service** | Any address: Ollama or LM Studio at home, Mistral… |
| **Demo** (`fake`) | Canned cards whatever the photo, no key and no cost: for the tests and for working on the interface. Not offered in ⚙️; set `NOTOSAURUS_LLM=fake` in `.env`. |

For the last three, *Load models* lists only models that accept images (and structured output, when the service says so). *Test* sends a small image to check that the model can read it and answer in JSON. The output format is always forced with a JSON schema. The user's prompt only says *what* to extract.

Local models (Ollama, LM Studio) work, but they read handwritten pages much less reliably.

Settings saved before OpenAI, OpenRouter and the other services were separate providers (`NOTOSAURUS_OPENAI_BASE_URL`, `NOTOSAURUS_OPENAI_API_KEY`…) still work: they are read as the matching provider.

## Data

There is no database. Everything is plain files under `data/` (or `NOTOSAURUS_DATA`), so backing up Notosaurus means copying that folder.

```
data/
  settings.json                              settings and API keys
  prompts.json                               saved prompts
  lessons/
    2026-09-28-espagnol-lecon-5-la-famille/  one folder per lesson
      lesson.json                            deck, cards, prompt, voice, owner, dates
      page-1.jpg, page-2.jpg                 photos
      audio/la-madre-3f2a1c9e.mp3            card audio (included in the .apkg)
      images/diagram-1-2-3cdb18fcc2-q.jpg    diagram cards: labels hidden (q) or shown (a)
  cache/tts/                                 voice previews, safe to delete
```

In the add-on, `data/` lives in the add-on's `user_files/` folder.

## Languages

The interface follows the page's language picker, then Anki's language (in the add-on), then the browser's, and falls back to English.

To add a language, see [CONTRIBUTING.md](CONTRIBUTING.md#adding-a-language): it takes a single file.

## Good to know

- **Always review the cards.** Even good models misread a word now and then.
- **Children's schoolwork is private data.** Photos go to the AI provider you choose. A local model keeps them at home, at the cost of accuracy.
- **Only paired devices.** On the Wi-Fi, only the computer itself and the devices that scanned Notosaurus's QR code (Tools → Notosaurus → Open on the phone, or ⚙️ → Phones) can use it: the others can't spend your AI credits or delete lessons. "Disconnect every phone" in ⚙️ → Phones unpairs them all (a phone lost or lent).
- **Plain HTTP on the local network.** The camera works over plain HTTP, and the page can be added to the home screen as a shortcut. Pairing stops the curious and other websites, not someone spying on the Wi-Fi traffic: keep your Wi-Fi protected (WPA2/WPA3). For HTTPS, and to use Notosaurus away from home, [Tailscale](https://tailscale.com) works: `tailscale serve 8000` on the computer, the Tailscale app on the phone, and set `NOTOSAURUS_PUBLIC_URL` to the `https://….ts.net` address.
- **Never expose AnkiConnect or Notosaurus to the Internet.** Keep them on your local network.
- **edge-tts is unofficial.** Microsoft could shut it down.

## Contributing

Setting up, tests, architecture, translations and releases: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[AGPL-3.0](LICENSE)
