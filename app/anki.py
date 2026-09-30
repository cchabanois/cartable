"""Builds the .apkg package with genanki."""

import hashlib
import html
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import genanki

from . import tts
from .errors import AppError
from .models import ExportRequest

CSS = """\
.card { font-family: system-ui, sans-serif; font-size: 26px; text-align: center; }
.info { font-size: 18px; color: #777; margin-top: 12px; }
"""


def _stable_id(*parts: str) -> int:
    """Deterministic id that fits in the 31 bits Anki expects."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:4], "big") >> 1


@dataclass
class NoteType:
    name: str
    kind: str
    fields: list[str]
    templates: list[dict]  # {"name", "qfmt", "afmt"}
    css: str = CSS


def note_type(voice: str, reverse: bool) -> NoteType:
    # Two ways to read the back aloud:
    # - Anki locale ("es_ES"): {{tts}} tag, spoken by the device's speech engine;
    # - edge-tts voice: embedded mp3, in an "Audio" field ([sound:…]).
    # Each combination gets its own note type (stable id, distinct name), so we
    # never overwrite a note type the user already has.
    anki_tts = tts.is_anki_locale(voice)
    sound = f"{{{{tts {voice}:Back}}}}" if anki_tts else "{{Audio}}"
    info = '{{#Info}}<div class="info">{{Info}}</div>{{/Info}}'
    templates = [
        {
            "name": "Recto → Verso",
            "qfmt": "{{Front}}",
            "afmt": f'{{{{FrontSide}}}}<hr id="answer">{{{{Back}}}}{sound}{info}',
        }
    ]
    if reverse:
        templates.append(
            {
                "name": "Verso → Recto",
                "qfmt": f"{{{{Back}}}}{sound}",
                "afmt": f'{{{{FrontSide}}}}<hr id="answer">{{{{Front}}}}{info}',
            }
        )
    fields = ["Front", "Back", "Info"]
    if anki_tts:
        kind = f"TTS Anki {voice}"
    else:
        fields.append("Audio")
        kind = "audio"
    name = "Cartable recto/verso" + (" + inverse" if reverse else "") + f" ({kind})"
    return NoteType(name, kind, fields, templates)


@dataclass
class Note:
    deck: str
    front: str
    fields: dict[str, str]  # field name → HTML value
    tags: list[str]
    mp3: Path | None


def notes(req: ExportRequest, nt: NoteType, audio: dict[str, Path] | None = None) -> list[Note]:
    """The notes to send, in both output formats. `audio` maps a card back to its mp3."""
    audio = audio or {}
    result = []
    for card in req.cards:
        front, back = card.front.strip(), card.back.strip()
        if not front or not back:
            continue
        values = {"Front": html.escape(front), "Back": html.escape(back), "Info": html.escape(card.info.strip())}
        mp3 = audio.get(back) if "Audio" in nt.fields else None
        if "Audio" in nt.fields:
            values["Audio"] = f"[sound:{mp3.name}]" if mp3 else ""
        result.append(
            Note(
                deck=_deck_name(req.deck, card.subdeck),
                front=front,
                fields=values,
                tags=[_tag(t) for t in card.tags if t.strip()],
                mp3=mp3,
            )
        )
    if not result:
        raise AppError("export.no_cards")
    return result


def _model(nt: NoteType) -> genanki.Model:
    return genanki.Model(
        _stable_id("model", nt.kind, str(len(nt.templates) > 1)),
        nt.name,
        fields=[{"name": f} for f in nt.fields],
        templates=nt.templates,
        css=nt.css,
    )


def _deck_name(base: str, subdeck: str) -> str:
    base, subdeck = base.strip(), subdeck.strip()
    return f"{base}::{subdeck}" if subdeck else base


def _tag(t: str) -> str:
    return re.sub(r"\s+", "_", t.strip())  # Anki tags cannot contain spaces


def build_apkg(req: ExportRequest, audio: dict[str, Path] | None = None) -> str:
    """Write the package to a temporary file and return its path.

    `audio` maps a card back to its mp3 (edge-tts voice); backs missing from it get no sound.
    """
    nt = note_type(req.voice, req.reverse)
    model = _model(nt)
    decks: dict[str, genanki.Deck] = {}
    all_notes = notes(req, nt, audio)
    for note in all_notes:
        deck = decks.setdefault(note.deck, genanki.Deck(_stable_id("deck", note.deck), note.deck))
        deck.add_note(
            genanki.Note(
                model=model,
                fields=[note.fields[f] for f in nt.fields],
                tags=note.tags,
                # Stable GUID: re-importing a corrected lesson updates the notes instead
                # of duplicating them (as long as the front and the deck stay the same).
                guid=genanki.guid_for(note.deck, note.front),
            )
        )
    media = sorted({str(n.mp3) for n in all_notes if n.mp3})
    fd, path = tempfile.mkstemp(suffix=".apkg")
    os.close(fd)
    genanki.Package(list(decks.values()), media_files=media).write_to_file(path)
    return path
