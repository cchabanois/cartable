# Changelog

All notable changes to Cartable. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/) (while in 0.x, anything may still change).

Each pull request adds a line under **Unreleased**. See [Releasing](README.md#releasing) for how a version is published.

## [Unreleased]

### Added

- Pictures on cards: with a prompt like "front: the picture of the word, back: the English word", an image model draws each card's picture (Gemini Flash Lite Image by default, settable in ⚙️; about 3 to 7 US¢ each, in the cost tracking). The cards show at once, their pictures when drawn; Anki gets them in a "Cartable image" note type.
- Every card has a stable id, so Anki recognises it even when its front changes or is the same as others'.

- Standing instructions for the AI in the settings, for every profile and for each Anki profile ("in year 8", "Spanish from Spain"): added to every generation and correction, on top of the lesson's prompt, never replacing the fixed rules.

- Each lesson keeps its AI calls (generation and corrections): provider, model, tokens and cost. Exact with OpenRouter, estimated from public prices otherwise. The settings page shows the cost of each lesson, the details of its calls and the total, which comes from a journal of every call (`data/ai-calls.json`): deleting a lesson doesn't lower it, and generations that failed after the model answered are counted.

- An AI correction can add a diagram label ("add a mask for the title"): the new card gets its mask, with the next free number; existing masks never move.

- Diagram cards are cropped to the diagram: the AI gives its frame, stretched to hold every mask, so the diagram shows bigger on a phone. In the review, the frame is a dashed line whose corners can be dragged to crop by hand (it never cuts a label).

- Photos taken sideways or upside down are saved upright, diagram masks turning with them. The turn comes from the reading direction of a line of text (where the AI places its first and last words), which is more reliable than asking the model for the page's orientation. A ↻ button on each photo turns it by hand.

- Diagrams: with a prompt like "one card per label, the diagram without the names", the AI finds each label of a diagram and hides it behind a number. Each card shows the diagram with every label hidden and asks "What is (2)?"; the back shows that label again. Masks can be moved and resized in the review. In Anki, the cards of a diagram share one light image, with the masks in HTML over it (direct send and .apkg); a corrected lesson updates its notes instead of duplicating them.

- The review shows the prompt a lesson was generated with ("📝 Prompt used for this lesson"), and reopening a lesson puts that prompt back, so "Generate again" starts from it.

- A 🗑 button on each prompt in the list of all prompts, to delete it without opening it.

- Lessons from the prompt alone, without a photo ("Generate from the prompt alone"): the words to learn written in the prompt, or a topic for the AI to cover. Photos stay the main way.

- Settings page (⚙️), "Lessons" section: every lesson with its owner; give a lesson to another Anki profile or to nobody, share it or not, delete it. Lessons whose owner no longer exists in Anki are flagged.

### Changed

- The settings page saves as you go (after a short pause in a text field; API keys once their field is left or with Enter), with a ✓ Saved pill instead of the Save button. The test buttons are "Test" and "Test the connection with Anki".
- A profile only ever sees its own lessons, shared lessons and lessons without owner: the "see other profiles' lessons" setting and the "All profiles" switch are gone (sharing does it; the settings' Lessons section shows every lesson). With Anki closed, every lesson is listed, read-only. The "send to another profile anyway?" question is gone with it.
- The demo provider (canned cards, whatever the photo) is no longer offered in the settings; it stays available with `CARTABLE_LLM=fake`, for tests and development. If it is the saved choice, the settings still show it, with a warning.
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
