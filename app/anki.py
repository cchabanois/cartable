"""Builds the .apkg package with genanki."""

import hashlib
import html
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import genanki

from . import diagrams, tts
from .errors import AppError
from .models import ExportRequest

CSS = """\
.card { font-family: system-ui, sans-serif; font-size: 26px; text-align: center; }
.info { font-size: 18px; color: #777; margin-top: 12px; }
.card img { max-width: 100%; height: auto; }
"""

# Diagram cards: the masks are HTML over the image, placed in % of its size.
DIAGRAM_CSS = """\
.cartable-diagram { position: relative; display: inline-block; max-width: 100%; line-height: 0; }
.cartable-diagram img { display: block; }
.cartable-mask {
  position: absolute; box-sizing: border-box; display: flex; align-items: center; justify-content: center;
  overflow: hidden; line-height: 1; font-size: 13px; font-weight: 700;
  background: #ffe08a; border: 2px solid #c77700; border-radius: 3px; color: #3d2b00;
}
.cartable-mask.target { background: #ff7a59; border-color: #b3261e; color: #fff; }
.cartable-mask.revealed { background: transparent; border: 3px solid #1b873f; }
"""


def _stable_id(*parts: str) -> int:
    """Deterministic id that fits in the 31 bits Anki expects."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:4], "big") >> 1


@dataclass(frozen=True)
class NoteType:
    name: str
    kind: str
    fields: tuple[str, ...]
    templates: tuple[dict, ...]  # {"name", "qfmt", "afmt"}
    css: str = CSS
    key: str = "Front"  # field telling which note an update is for

    def __hash__(self) -> int:
        return hash(self.name)


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
    return NoteType(name, kind, tuple(fields), tuple(templates))


def diagram_note_type(voice: str) -> NoteType:
    """A diagram label: the diagram with every label hidden and the question, then the
    answer with that label shown again. One image per diagram, shared by its cards;
    the masks are HTML ("Masks", "AnswerMasks"). "Id" (lesson, photo, label number)
    tells which note an update is for, as the question alone ("What is (1)?") repeats."""
    anki_tts = tts.is_anki_locale(voice)
    sound = f"{{{{tts {voice}:Back}}}}" if anki_tts else "{{Audio}}"
    info = '{{#Info}}<div class="info">{{Info}}</div>{{/Info}}'
    templates = (
        {
            "name": "Schéma",
            "qfmt": '<div class="cartable-diagram">{{Image}}{{Masks}}</div><div>{{Front}}</div>',
            "afmt": '<div class="cartable-diagram">{{Image}}{{AnswerMasks}}</div><div>{{Front}}</div>'
            f'<hr id="answer">{{{{Back}}}}{sound}{info}',
        },
    )
    fields = ["Front", "Back", "Info"] + ([] if anki_tts else ["Audio"]) + ["Image", "Masks", "AnswerMasks", "Id"]
    kind = f"labels TTS Anki {voice}" if anki_tts else "labels audio"
    name = f"Cartable légendes ({kind.removeprefix('labels ')})"
    return NoteType(name, kind, tuple(fields), templates, css=CSS + DIAGRAM_CSS, key="Id")


@dataclass
class Note:
    nt: NoteType
    deck: str
    fields: dict[str, str]  # field name → HTML value
    tags: list[str]
    media: list[Path]  # files the note uses: mp3, images

    @property
    def key(self) -> str:
        return self.fields[self.nt.key]


def notes(
    req: ExportRequest,
    audio: dict[str, Path] | None = None,
    images: dict[int, Path] | None = None,
) -> list[Note]:
    """The notes to send, in both output formats. `audio` maps a card back to its mp3;
    `images` maps the index of a diagram card to its diagram's image."""
    audio, images = audio or {}, images or {}
    text_nt, diagram_nt = note_type(req.voice, req.reverse), diagram_note_type(req.voice)
    result = []
    for i, card in enumerate(req.cards):
        front, back = card.front.strip(), card.back.strip()
        if not front or not back:
            continue
        nt = diagram_nt if i in images else text_nt
        values = {"Front": html.escape(front), "Back": html.escape(back), "Info": html.escape(card.info.strip())}
        media = []
        if "Audio" in nt.fields:
            mp3 = audio.get(back)
            values["Audio"] = f"[sound:{mp3.name}]" if mp3 else ""
            media += [mp3] if mp3 else []
        if i in images:
            page = [c.mask for c in req.cards if c.mask and c.mask.page == card.mask.page]
            values["Image"] = f'<img src="{images[i].name}">'
            values["Masks"] = diagrams.masks_html(page, card.mask.n, reveal=False)
            values["AnswerMasks"] = diagrams.masks_html(page, card.mask.n, reveal=True)
            values["Id"] = f"{req.lesson_id or ''}:{card.mask.page}:{card.mask.n}"
            media.append(images[i])
        result.append(
            Note(
                nt=nt,
                deck=_deck_name(req.deck, card.subdeck),
                fields=values,
                tags=[_tag(t) for t in card.tags if t.strip()],
                media=media,
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
        templates=list(nt.templates),
        css=nt.css,
    )


def _deck_name(base: str, subdeck: str) -> str:
    base, subdeck = base.strip(), subdeck.strip()
    return f"{base}::{subdeck}" if subdeck else base


def _tag(t: str) -> str:
    return re.sub(r"\s+", "_", t.strip())  # Anki tags cannot contain spaces


def build_apkg(
    req: ExportRequest,
    audio: dict[str, Path] | None = None,
    images: dict[int, Path] | None = None,
) -> str:
    """Write the package to a temporary file and return its path.

    `audio` maps a card back to its mp3 (edge-tts voice); backs missing from it get no
    sound. `images`: see `notes`.
    """
    decks: dict[str, genanki.Deck] = {}
    models: dict[NoteType, genanki.Model] = {}
    all_notes = notes(req, audio, images)
    for note in all_notes:
        deck = decks.setdefault(note.deck, genanki.Deck(_stable_id("deck", note.deck), note.deck))
        deck.add_note(
            genanki.Note(
                model=models.setdefault(note.nt, _model(note.nt)),
                fields=[note.fields[f] for f in note.nt.fields],
                tags=note.tags,
                # Stable GUID: re-importing a corrected lesson updates the notes instead
                # of duplicating them (as long as the front, or a label's place, and the
                # deck stay the same).
                guid=genanki.guid_for(note.deck, note.key),
            )
        )
    media = sorted({str(path) for n in all_notes for path in n.media})
    fd, path = tempfile.mkstemp(suffix=".apkg")
    os.close(fd)
    genanki.Package(list(decks.values()), media_files=media).write_to_file(path)
    return path
