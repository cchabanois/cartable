"""Builds the .apkg package with genanki."""

import hashlib
import html
import os
import random
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
.dictation { font-size: 40px; }
"""

# Diagram cards: the masks are HTML over the image, placed in % of its size.
DIAGRAM_CSS = """\
.notosaurus-diagram { position: relative; display: inline-block; max-width: 100%; line-height: 0; }
.notosaurus-diagram img { display: block; }
.notosaurus-mask {
  position: absolute; box-sizing: border-box; display: flex; align-items: center; justify-content: center;
  overflow: hidden; line-height: 1; font-size: 13px; font-weight: 700;
  background: #ffe08a; border: 2px solid #c77700; border-radius: 3px; color: #3d2b00;
}
.notosaurus-mask.target { background: #ff7a59; border-color: #b3261e; color: #fff; }
.notosaurus-mask.revealed { background: transparent; border: 3px solid #1b873f; }
"""

CLOZE_CSS = """\
.cloze { font-weight: 700; color: #0b5cad; }
.extra { margin-top: 12px; }
"""

# A gap in a text: {{c1::1789}}, {{c2::la Bastille::lieu}} (Anki's cloze syntax)
CLOZE = re.compile(r"\{\{c\d+::")


def is_cloze(text: str) -> bool:
    """A text with gaps: a cloze card, one Anki card per gap number."""
    return bool(CLOZE.search(text))


# Multiple choice: the options in a list (A, B, C…), the right one marked on the answer
CHOICE_CSS = """\
.notosaurus-choices {
  display: inline-block; margin: 12px auto 0; padding-left: 1.8em; text-align: left; list-style: upper-alpha;
}
.notosaurus-choices li { margin: 6px 0; }
.notosaurus-choices li.right { color: #1b873f; font-weight: 700; }
.notosaurus-choices li.right::after { content: " ✔"; }
.notosaurus-choices li.wrong { opacity: .5; }
"""


def is_choice(card) -> bool:
    """A multiple-choice or true/false card: a right answer (its back) and wrong ones.
    A text with gaps or a diagram label stays what it is."""
    return bool(
        card.front.strip()
        and card.back.strip()
        and any(c.strip() for c in card.choices)
        and not card.mask
        and not is_cloze(card.front)
    )


def choice_order(card) -> list[str]:
    """The options as Anki shows them: always the same order for a card (on its question
    and its answer, from one review to the next), the right one anywhere. Two options
    (true/false): alphabetical, the same for every card."""
    options = list(dict.fromkeys(o.strip() for o in [card.back, *card.choices] if o.strip()))
    if len(options) == 2:
        return sorted(options, key=str.casefold)
    random.Random(_stable_id("choices", card.front.strip())).shuffle(options)
    return options


def _choices_html(card, reveal: bool) -> str:
    right = card.back.strip()
    items = []
    for option in choice_order(card):
        mark = (' class="right"' if option == right else ' class="wrong"') if reveal else ""
        items.append(f"<li{mark}>{_html(option)}</li>")
    return f'<ol class="notosaurus-choices">{"".join(items)}</ol>'


PICTURE_CSS = """\
.notosaurus-picture img { max-width: min(100%, 320px); max-height: 50vh; border-radius: 12px; }
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
    cloze: bool = False  # Anki makes one card per gap number
    variant: str = ""  # typed answer, dictation: part of the note type's id
    reverse: bool = False  # with the reverse card: part of the note type's id

    @property
    def family(self) -> str:
        return family(self.name)

    def __hash__(self) -> int:
        return hash(self.name)


# Note types of a family hold the same kind of card; their options (voice, reverse,
# typed answer, dictation) differ. A note can change type within its family.
FAMILIES = {
    "Notosaurus recto/verso": "text",
    "Notosaurus légendes": "diagram",
    "Notosaurus image": "picture",
    "Notosaurus texte à trous": "cloze",
    "Notosaurus QCM": "choice",
}


def family(note_type_name: str) -> str:
    """ "text", "diagram", "picture", "cloze", "choice", or "" for a note type not Notosaurus's."""
    return next((f for prefix, f in FAMILIES.items() if note_type_name.startswith(prefix)), "")


def _typed(template: dict, field: str) -> dict:
    """The template asking to type `field`: a box on the question, Anki's letter by
    letter comparison in its place on the answer. The answer repeats the question
    (not {{FrontSide}}, which would show the box twice)."""
    question = template["qfmt"]
    answer = template["afmt"].replace("{{FrontSide}}", question).replace(f"{{{{{field}}}}}", f"{{{{type:{field}}}}}", 1)
    return {**template, "qfmt": f"{question}{{{{type:{field}}}}}", "afmt": answer}


def _variant(name: str, typing: bool, dictation: bool) -> tuple[str, str]:
    """The note type's name and variant ("" for the plain one: its id is unchanged)."""
    parts = [p for p, on in (("typing", typing), ("dictation", dictation)) if on]
    name += (" à taper" if typing else "") + (" + dictée" if dictation else "")
    return name, "+".join(parts)


def note_type(voice: str, reverse: bool, typing: bool = False, dictation: bool = False) -> NoteType:
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
    if typing:
        templates = [_typed(templates[0], "Back"), *[_typed(t, "Front") for t in templates[1:]]]
    dictation = dictation and bool(voice)  # nothing to hear without a voice
    if dictation:
        # Hear the back, write it; the answer shows the comparison and what it means
        heard = f'<div class="dictation">🎧</div>{sound}{{{{type:Back}}}}'
        templates.append({"name": "Dictée", "qfmt": heard, "afmt": f'{heard}<hr id="answer">{{{{Front}}}}{info}'})
    fields = ["Front", "Back", "Info"]
    if anki_tts:
        kind = f"TTS Anki {voice}"
    else:
        fields.append("Audio")
        kind = "audio"
    name, variant = _variant("Notosaurus recto/verso" + (" + inverse" if reverse else ""), typing, dictation)
    return NoteType(f"{name} ({kind})", kind, tuple(fields), tuple(templates), variant=variant, reverse=reverse)


def diagram_note_type(voice: str, typing: bool = False) -> NoteType:
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
            "qfmt": '<div class="notosaurus-diagram">{{Image}}{{Masks}}</div><div>{{Front}}</div>',
            "afmt": '<div class="notosaurus-diagram">{{Image}}{{AnswerMasks}}</div><div>{{Front}}</div>'
            f'<hr id="answer">{{{{Back}}}}{sound}{info}',
        },
    )
    if typing:
        templates = (_typed(templates[0], "Back"),)
    fields = ["Front", "Back", "Info"] + ([] if anki_tts else ["Audio"]) + ["Image", "Masks", "AnswerMasks", "Id"]
    kind = f"labels TTS Anki {voice}" if anki_tts else "labels audio"
    name, variant = _variant("Notosaurus légendes", typing, False)
    name = f"{name} ({kind.removeprefix('labels ')})"
    return NoteType(name, kind, tuple(fields), templates, css=CSS + DIAGRAM_CSS, key="Id", variant=variant)


def picture_note_type(voice: str, typing: bool = False, on_back: bool = False) -> NoteType:
    """A picture card: the picture and the front text (e.g. "How do you say it in
    English?"), then the answer. "Id" (the card's own id) tells which note an update
    is for: the fronts are often all the same. `on_back`: the picture belongs to the
    answer ("What is a tangent?"): shown with it only. Same fields and card name: a note
    moves from one to the other keeping its history."""
    anki_tts = tts.is_anki_locale(voice)
    sound = f"{{{{tts {voice}:Back}}}}" if anki_tts else "{{Audio}}"
    info = '{{#Info}}<div class="info">{{Info}}</div>{{/Info}}'
    picture = '<div class="notosaurus-picture">{{Picture}}</div>'
    if on_back:
        question, answer = "{{Front}}", f'{{{{FrontSide}}}}<hr id="answer">{{{{Back}}}}{sound}{picture}{info}'
    else:
        question = picture + "{{#Front}}<div>{{Front}}</div>{{/Front}}"
        answer = f'{{{{FrontSide}}}}<hr id="answer">{{{{Back}}}}{sound}{info}'
    templates = ({"name": "Image", "qfmt": question, "afmt": answer},)
    if typing:
        templates = (_typed(templates[0], "Back"),)
    fields = ["Front", "Back", "Info"] + ([] if anki_tts else ["Audio"]) + ["Picture", "Id"]
    kind = f"picture TTS Anki {voice}" if anki_tts else "picture audio"
    name, variant = _variant("Notosaurus image au verso" if on_back else "Notosaurus image", typing, False)
    variant = "+".join(filter(None, ["back" if on_back else "", variant]))
    name = f"{name} ({kind.removeprefix('picture ')})"
    return NoteType(name, kind, tuple(fields), templates, css=CSS + PICTURE_CSS, key="Id", variant=variant)


def cloze_note_type() -> NoteType:
    """A text with gaps ("{{c1::1789}}"): Anki makes one card per gap number, the
    others shown. "Extra" (the card's back, often empty) shows with the answer. Not
    read aloud. "Id" (the card's own id) tells which note an update is for: the text
    is what gets corrected."""
    info = '{{#Info}}<div class="info">{{Info}}</div>{{/Info}}'
    templates = (
        {
            "name": "Texte à trous",
            "qfmt": "{{cloze:Text}}",
            "afmt": '{{cloze:Text}}{{#Extra}}<div class="extra">{{Extra}}</div>{{/Extra}}' + info,
        },
    )
    fields = ("Text", "Extra", "Info", "Id")
    return NoteType("Notosaurus texte à trous", "cloze", fields, templates, css=CSS + CLOZE_CSS, key="Id", cloze=True)


def choice_note_type() -> NoteType:
    """A multiple-choice or true/false card: the question and its options, then the
    options again with the right one marked. In plain HTML, no script: the same on every
    Anki. The options' order is fixed per card ("Choices", "AnswerChoices"). Its picture
    or figure, if any, goes on the question ("Picture") or with the answer
    ("BackPicture"). "Id" (the card's own id) tells which note an update is for."""
    info = '{{#Info}}<div class="info">{{Info}}</div>{{/Info}}'
    picture = '{{#Picture}}<div class="notosaurus-picture">{{Picture}}</div>{{/Picture}}'
    back_picture = '{{#BackPicture}}<div class="notosaurus-picture">{{BackPicture}}</div>{{/BackPicture}}'
    question = f"{picture}<div>{{{{Question}}}}</div>"
    templates = (
        {
            "name": "QCM",
            "qfmt": f"{question}{{{{Choices}}}}",
            "afmt": f'{question}<hr id="answer">{{{{AnswerChoices}}}}{back_picture}{info}',
        },
    )
    fields = ("Question", "Answer", "Choices", "AnswerChoices", "Picture", "BackPicture", "Info", "Id")
    css = CSS + PICTURE_CSS + CHOICE_CSS
    return NoteType("Notosaurus QCM", "choice", fields, templates, css=css, key="Id")


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
    images: dict[int, tuple[Path, list[float] | None]] | None = None,
    pictures: dict[int, Path] | None = None,
) -> list[Note]:
    """The notes to send, in both output formats. `audio` maps a card back to its mp3;
    `images` maps the index of a diagram card to its diagram's image and crop;
    `pictures`, the index of a picture card to its picture."""
    audio, images, pictures = audio or {}, images or {}, pictures or {}
    own_tags = [lesson_tag(req.lesson_id)] if req.lesson_id else []  # to find the lesson's notes again
    typing = req.typing or req.dictation
    text_nt = note_type(req.voice, req.reverse, req.typing, req.dictation)
    diagram_nt, picture_nt = diagram_note_type(req.voice, req.typing), picture_note_type(req.voice, req.typing)
    answer_picture_nt = picture_note_type(req.voice, req.typing, on_back=True)
    # A formula isn't typed (its code would be) nor heard
    math_nt, cloze_nt, choice_nt = note_type(req.voice, req.reverse), cloze_note_type(), choice_note_type()
    result = []
    for i, card in enumerate(req.cards):
        front, back = card.front.strip(), card.back.strip()
        if is_cloze(front):  # the gaps are the answers: the back is optional
            result.append(
                Note(
                    nt=cloze_nt,
                    deck=_deck_name(req.deck, card.subdeck),
                    fields={
                        "Text": _html(front),
                        "Extra": _html(back),
                        "Info": _info(card),
                        "Id": card.id or f"{req.lesson_id or ''}:{i}",
                    },
                    tags=[_tag(t) for t in card.tags if t.strip()] + own_tags,
                    media=[],
                )
            )
            continue
        if is_choice(card):  # not typed nor heard: the options are read
            picture = f'<img src="{pictures[i].name}">' if i in pictures else ""
            result.append(
                Note(
                    nt=choice_nt,
                    deck=_deck_name(req.deck, card.subdeck),
                    fields={
                        "Question": _html(front),
                        "Answer": _html(back),
                        "Choices": _choices_html(card, reveal=False),
                        "AnswerChoices": _choices_html(card, reveal=True),
                        "Picture": "" if card.picture_on_back else picture,
                        "BackPicture": picture if card.picture_on_back else "",
                        "Info": _info(card),
                        "Id": card.id or f"{req.lesson_id or ''}:{i}",
                    },
                    tags=[_tag(t) for t in card.tags if t.strip()] + own_tags,
                    media=[pictures[i]] if i in pictures else [],
                )
            )
            continue
        on_back = i in pictures and card.picture_on_back
        if not back or not (front or (i in pictures and not on_back)):  # a picture card may have no front text
            continue
        nt = diagram_nt if i in images else (answer_picture_nt if on_back else picture_nt) if i in pictures else text_nt
        if typing and tts.has_math(back) and nt is text_nt:
            nt = math_nt
        values = {"Front": _html(front), "Back": _html(back), "Info": _info(card)}
        media = []
        if "Audio" in nt.fields:
            mp3 = audio.get(back)
            values["Audio"] = f"[sound:{mp3.name}]" if mp3 else ""
            media += [mp3] if mp3 else []
        if i in images:
            page = [c.mask for c in req.cards if c.mask and c.mask.page == card.mask.page]
            image, box = images[i]
            values["Image"] = f'<img src="{image.name}">'
            values["Masks"] = diagrams.masks_html(page, card.mask.n, reveal=False, box=box)
            values["AnswerMasks"] = diagrams.masks_html(page, card.mask.n, reveal=True, box=box)
            values["Id"] = f"{req.lesson_id or ''}:{card.mask.page}:{card.mask.n}"
            media.append(image)
        elif i in pictures:
            values["Picture"] = f'<img src="{pictures[i].name}">'
            values["Id"] = card.id or f"{req.lesson_id or ''}:{i}"
            media.append(pictures[i])
        result.append(
            Note(
                nt=nt,
                deck=_deck_name(req.deck, card.subdeck),
                fields=values,
                tags=[_tag(t) for t in card.tags if t.strip()] + own_tags,
                media=media,
            )
        )
    if not result:
        raise AppError("export.no_cards")
    return result


TAG_PREFIX = "notosaurus::"


def lesson_tag(lesson_id: str) -> str:
    """The tag of every note sent for a lesson: its notes can be found again (to delete
    them with the lesson), whatever was changed in them."""
    return f"{TAG_PREFIX}{lesson_id}"


def lesson_notes(req: ExportRequest) -> list[Note]:
    """The notes a lesson gives, as sent (note type, deck, key), without their media:
    to find in Anki the notes sent before they had the lesson's tag."""
    diagrams_ = {i: (Path("diagram.jpg"), None) for i, c in enumerate(req.cards) if c.mask}
    pictures_ = {i: Path("picture.jpg") for i, c in enumerate(req.cards) if c.picture and not c.mask}
    try:
        return notes(req, images=diagrams_, pictures=pictures_)
    except AppError:  # no card
        return []


def _info(card) -> str:
    """The Info field: the card's info, then its "did you know" (💡, in italics). Styled
    inline: the note types already in Anki keep their own CSS."""
    info, fact = _html(card.info.strip()), card.fun_fact.strip()
    if not fact:
        return info
    style = "margin-top:8px;font-style:italic"
    return f'{info}<div style="{style}">💡 {_html(fact)}</div>'


def _html(text: str) -> str:
    """A card's text as an Anki field: escaped, its line breaks kept."""
    return html.escape(text).replace("\n", "<br>")


def _model(nt: NoteType) -> genanki.Model:
    return genanki.Model(
        # Reverse card or not, then the options: unique per note type. (Before the
        # dictation card, "two templates" meant the reverse card: same ids as then.)
        _stable_id("model", nt.kind, str(nt.reverse), *([nt.variant] if nt.variant else [])),
        nt.name,
        fields=[{"name": f} for f in nt.fields],
        templates=list(nt.templates),
        css=nt.css,
        model_type=genanki.Model.CLOZE if nt.cloze else genanki.Model.FRONT_BACK,
    )


def _deck_name(base: str, subdeck: str) -> str:
    base, subdeck = base.strip(), subdeck.strip()
    return f"{base}::{subdeck}" if subdeck else base


def _tag(t: str) -> str:
    return re.sub(r"\s+", "_", t.strip())  # Anki tags cannot contain spaces


def build_apkg(
    req: ExportRequest,
    audio: dict[str, Path] | None = None,
    images: dict[int, tuple[Path, list[float] | None]] | None = None,
    pictures: dict[int, Path] | None = None,
) -> str:
    """Write the package to a temporary file and return its path.

    `audio` maps a card back to its mp3 (edge-tts voice); backs missing from it get no
    sound. `images`: see `notes`.
    """
    decks: dict[str, genanki.Deck] = {}
    models: dict[NoteType, genanki.Model] = {}
    all_notes = notes(req, audio, images, pictures)
    seen: dict[tuple[str, str], int] = {}  # the same key twice in a deck ("le vol": vuelo, robo)
    for note in all_notes:
        n = seen[(note.deck, note.key)] = seen.get((note.deck, note.key), -1) + 1
        deck = decks.setdefault(note.deck, genanki.Deck(_stable_id("deck", note.deck), note.deck))
        deck.add_note(
            genanki.Note(
                model=models.setdefault(note.nt, _model(note.nt)),
                fields=[note.fields[f] for f in note.nt.fields],
                tags=note.tags,
                # Stable GUID: re-importing a corrected lesson updates the notes instead
                # of duplicating them (as long as the front, or a label's place, and the
                # deck stay the same).
                guid=genanki.guid_for(note.deck, note.key, *([n] if n else [])),
            )
        )
    media = sorted({str(path) for n in all_notes for path in n.media})
    fd, path = tempfile.mkstemp(suffix=".apkg")
    os.close(fd)
    genanki.Package(list(decks.values()), media_files=media).write_to_file(path)
    return path
