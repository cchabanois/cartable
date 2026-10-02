# Changelog

All notable changes to Cartable. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/) (while in 0.x, anything may still change).

Each pull request adds a line under **Unreleased**. See [Releasing](README.md#releasing) for how a version is published.

## [Unreleased]

### Added

- Tests of the pages in a real browser (Playwright, headless Chromium), in the CI: the page saving as it goes, generating again and undoing, deleting the open lesson, the settings showing what each AI service needs.

- With OpenRouter, the settings suggest a short list: its "~…-latest" models, always the latest version of each main family (Gemini Flash ⭐ recommended, Claude Sonnet, GPT Mini…), with their readable names; every model on demand. The list loads by itself when OpenRouter has a key. Choosing OpenRouter now suggests `~google/gemini-flash-latest`.

- Only paired devices can use Cartable over the Wi-Fi: the computer itself always, other devices once they scanned the QR code (Tools → Cartable → Open on the phone, or the new ⚙️ → Phones section), which leaves a token in a long-lived cookie (`SameSite=Strict`, so other websites can't use it). The home screen icon carries it too (on an iPhone it doesn't share Safari's cookies). "Disconnect every phone" changes the token. Without HTTPS it stops the curious and other websites, not someone spying on the Wi-Fi; the README explains Tailscale for HTTPS. `CARTABLE_PUBLIC_URL` gives the address for the QR code where Cartable can't see it (Docker, proxy).

- Changing a lesson's options after it was sent (voice, reverse card, typed answer, dictation) no longer duplicates its cards in Anki: the notes move to the new note type and keep their review history; a card whose option was switched off goes, a new option adds its card. Through the add-on and recent AnkiConnect versions (`updateNoteModel`; an older one is said to add them as before). Re-importing an `.apkg` already did it.

- Starting a new lesson is clearer: "＋ New" in the app bar (it was a bare "＋"), and "＋ New lesson" in the message after a send to Anki or an export; leaving a lesson says it stays in 📚 Lessons. On phones the lessons button shows its icon and count only, so the app bar fits.

- "✏️ Free" prompt, first of the chips: write a prompt for this time only, not saved (it stays with the lesson; "💾 Save as a new prompt" if it's worth keeping). Its voice is "auto". The chip that creates a saved prompt is now "💾 New".

- Deleting a lesson can delete its cards in Anki too: the confirmation says how many the open profile has and offers to delete them (unticked; their review history goes with them), then the decks left empty (the lesson's and their parents, never Anki's "Default"), then syncs. Notes sent now carry a `cartable::<lesson>` tag; those sent before are found by deck, note type and key. From the open lesson too (🗑 next to its title), and in the settings page. The add-on answers `findCards`, `addTags`, `deleteNotes` and `deleteDecks`.

- "✨ Generate again" replaces the lesson's cards (same photos, or the photos as changed, with the prompt now chosen) instead of making a second lesson; "↩ Undo" brings the previous cards and prompt back. Warned when the lesson was already sent to Anki (the cards sent stay there). Someone else's lesson still gives a new lesson of one's own.

- Cartable prompt ⭐ "Automatic (from the lesson)", first in the list (so picked when nothing was picked before): the AI looks at the lesson and chooses the cards (vocabulary, questions, cloze, formulas, diagram to complete, or a mix), with the voice of the language if any. One line in the review says what it chose.

- Existing decks reused: the AI gets the decks of the open Anki profile and of the lessons it sees, and puts a lesson into the matching one ("Maths" if it exists, not a new "Mathématiques"), or makes a new name when none matches. The deck field suggests them. The add-on answers `deckNames`.

- Typed answers and dictation, two options next to "Add the reverse card", kept with the lesson. "Type the answer": the pupil types it and Anki compares it letter by letter (text, picture and diagram cards; not formulas). "Add a dictation" (needs a voice): one more card per note, where the back is heard and written. New note types ("… à taper", "… + dictée"); the existing ones are unchanged. A prompt can carry these options (in its editor): the lessons made with it start with them. New Cartable prompt ⭐ "Dictée de mots": a sentence with the word replaced by "…", the word heard and typed.

- Cloze cards (text with gaps): with a prompt asking for gaps, each card is a sentence like "La Révolution française commence en {{c1::1789}} avec la prise de {{c2::la Bastille}}."; Anki makes one card per gap number ("Cartable texte à trous" note type, through the add-on, AnkiConnect or the .apkg). The review shows the gaps, numbered; the back becomes an optional extra; not read aloud. In the review, the front wraps and grows with its text, and can hold several lines (Enter), kept in Anki. New Cartable prompt ⭐ "Texte à trous".

- Formulas: the AI writes them in MathJax (`\(…\)`, `\[…\]`), which Anki displays natively; the review shows them drawn (KaTeX) under the text. A back with a formula isn't read aloud. New Cartable prompts ⭐ "Formulas (maths, physics…)" (from the lesson) and "Middle school maths formulas" (no photo needed).

- Cartable's own prompts (⭐): vocabulary and sentences of a language, questions / answers, diagram to complete, words in pictures, word list. In the page's language, read-only, never deleted, improved with the app. Any prompt can be duplicated (⧉) to be adapted. Their language prompts use the voice "auto": the AI tells the language of the backs ("es-ES"), and Cartable picks its voice; "auto" works in any prompt.

- A 🖼️ panel on each card: what to draw, 🎨 draw / draw again, 📷 your own photo (free), ✕ no picture. A subject drawn once is reused by the next lessons for free (cache).

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

- OpenAI, OpenRouter and the other OpenAI-compatible services are three providers of their own in ⚙️ (and in the code), each with its key and its way to choose the model, instead of one "OpenAI-compatible" provider with services. Saved settings and `.env` are read as before.
- The default prompts are no longer copied into the user's prompts on first start; old copies left unchanged that a Cartable prompt replaces are dropped (the "FR → ES" ones, with their Spanish voice, are kept).
- In the Anki add-on, no AnkiWeb sync is tried for a profile that isn't logged in (no more warning at each send); the settings say whether the open profile is logged in.
- The settings page saves as you go (after a short pause in a text field; API keys once their field is left or with Enter), with a ✓ Saved pill instead of the Save button. The test buttons are "Test" and "Test the connection with Anki".
- A profile only ever sees its own lessons, shared lessons and lessons without owner: the "see other profiles' lessons" setting and the "All profiles" switch are gone (sharing does it; the settings' Lessons section shows every lesson). With Anki closed, every lesson is listed, read-only. The "send to another profile anyway?" question is gone with it.
- The demo provider (canned cards, whatever the photo) is no longer offered in the settings; it stays available with `CARTABLE_LLM=fake`, for tests and development. If it is the saved choice, the settings still show it, with a warning.
- A lesson is read-only for the other Anki profiles: only the profile that created it can edit, correct with the AI, share or delete it. The others can still read it, send it to their own Anki and export it (without changing it).

### Fixed

- The settings no longer offer OpenRouter's `:batch` models: half price, but answered up to hours later, while a lesson is waited for.
- In the settings page, deleting a lesson showed an error (it was deleted anyway) and never offered to delete its cards in Anki.
- The home screen icon: phones were given an SVG only, so Android made a plain letter icon. PNG icons now (192, 512, a maskable one for Android's shapes, 180 for iPhone), made from `icon.svg` by `tools/make_icons.py`.
- Note types with a dictation card, with and without the reverse card, had the same id: in a package, Anki took them for one.
- "Generate again" no longer switches off the options set in the review (reverse card, typed answer, dictation).
- Deleting a lesson with its cards in Anki never deletes a card another lesson uses too (the same lesson made twice in a deck).
- Sending to Anki or exporting while pictures are being drawn waits for them, instead of sending the cards without their pictures.
- A prompt's "auto" voice stays "auto" after a generation: generating again finds the voice of the new backs' language.
- Two cards with the same front in a deck ("le vol": vuelo, robo) each keep their own note in Anki.
- The AI no longer puts HTML tags or Markdown in the cards (bold words of the page came out as "<b>…</b>" in Anki).
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
