# Changelog

All notable changes to Cartable. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/) (while in 0.x, anything may still change).

Each pull request adds a line under **Unreleased**. See [Releasing](README.md#releasing) for how a version is published.

## [Unreleased]

### Added

- A 🗑 button on each prompt in the list of all prompts, to delete it without opening it.

- Lessons from the prompt alone, without a photo ("Generate from the prompt alone"): the words to learn written in the prompt, or a topic for the AI to cover. Photos stay the main way.

- Settings page (⚙️), "Lessons" section: every lesson with its owner; give a lesson to another Anki profile or to nobody, share it or not, delete it. Lessons whose owner no longer exists in Anki are flagged.

### Changed

- A lesson is read-only for the other Anki profiles: only the profile that created it can edit, correct with the AI, share or delete it. The others can still read it, send it to their own Anki and export it (without changing it).

### Fixed

- After an update, browsers could keep old copies of the page's files (untranslated texts, missing styles): they are now checked on every load.
- Every automatic save of a lesson was sent twice.

- Anki add-on on Anki 26.08 and later (and on 25.02 and earlier), which don't provide uv: the add-on now downloads a pinned uv release from GitHub, checked against its SHA-256, after asking the user. Python 3.13 is installed inside the add-on's folder, never shared with the system's Python.

## [0.1.0]

First version.

### Added

- Photos of a lesson → Anki cards drafted by a vision AI: Gemini, Claude or any OpenAI-compatible service (OpenAI and OpenRouter presets, one key per service, model list and image + JSON test).
- Saved prompts, with recent ones as chips and a search.
- Review screen: edit, add or delete cards, or ask the AI to fix them in plain words (with undo).
- Audio of the card backs with edge-tts, embedded in the deck.
- Export as `.apkg` (stable GUIDs: re-importing updates cards), or direct send to Anki (updates instead of duplicates, then sync).
- Lessons saved as folders (photos, cards, audio), reopened from "My lessons".
- Anki add-on: runs the server with Anki, AnkiConnect-compatible bridge, QR code for the phone, settings only on the computer.
- Lessons per Anki profile: the creating profile owns a lesson and may share it; the page follows profile switches.
- English and French interface; a language is one JSON file.
- Admin page (standalone mode: password protected).

[Unreleased]: https://github.com/cchabanois/cartable/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cchabanois/cartable/releases/tag/v0.1.0
