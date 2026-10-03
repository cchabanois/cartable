from typing import Literal

from pydantic import BaseModel, Field


class Mask(BaseModel):
    """Where a diagram label is, to hide it on the photo (image occlusion).

    From the AI, `box` is in the format its instructions give (depends on the model);
    once saved, it is [x_min, y_min, x_max, y_max] as fractions of the photo's size."""

    page: int = Field(description="Number of the photo showing the label (1 = first photo).")
    n: int = Field(description="Number drawn in place of the label: 1, 2, 3… on each photo.")
    box: list[float] = Field(description="Bounding box of the label's text, in the format of the instructions.")


class Card(BaseModel):
    front: str = Field(
        description="Front: what is shown first (e.g. the word in the pupil's language), "
        "or the text with its gaps for a cloze card (see the rules)."
    )
    back: str = Field(description="Back: the answer (e.g. the translation in the language being learned).")
    choices: list[str] = Field(
        default_factory=list,
        description="Only for a multiple-choice or true/false card (see the rules): the wrong options. "
        "Empty otherwise.",
    )
    info: str = Field(default="", description="Useful extra info: gender, plural, example… Empty if none.")
    subdeck: str = Field(
        default="", description='Sub-deck (e.g. "Vocabulary", "Conjugation"). Empty for a single deck.'
    )
    tags: list[str] = Field(default_factory=list)
    mask: Mask | None = Field(default=None, description="Only for a diagram label (see the rules); otherwise null.")
    picture_prompt: str = Field(
        default="",
        description="Only when the instructions ask for a picture on the card (see the rules): what to draw, "
        "in English. Empty otherwise.",
    )
    figure: str = Field(
        default="",
        description="Only when the card needs an exact figure (see the rules): what to draw, every label "
        "with its exact text. Empty otherwise.",
    )
    fun_fact: str = Field(
        default="",
        description='Only when fun facts are asked for (see the request): one short "did you know" sentence. '
        "Empty otherwise.",
    )
    picture_on_back: bool = Field(
        default=False,
        description="True when the picture or figure belongs to the answer (see the rules); false when it is "
        "needed to answer.",
    )
    # Set by Notosaurus, not by the AI:
    picture: str = Field(default="", description="Leave empty.")  # file in the lesson's images/ folder
    id: str = Field(default="", description="Leave empty.")  # stable: tells Anki which note a card is


class Deck(BaseModel):
    deck: str = Field(description='Anki deck name, with :: for hierarchy (e.g. "Spanish::Lesson 5 - Family").')
    cards: list[Card]


class TextLine(BaseModel):
    """A line of text on a photo, by its first and last words: its reading direction
    tells how the photo is turned (models place words well, but name directions badly)."""

    page: int = Field(description="Number of the photo (1 = first photo).")
    first_word: list[float] = Field(description="Box of the line's first word, in the format of the instructions.")
    last_word: list[float] = Field(description="Box of the line's last word, in the format of the instructions.")


class Frame(BaseModel):
    """Where a diagram is on a photo: the image Anki gets is cropped to it."""

    page: int = Field(description="Number of the photo (1 = first photo).")
    box: list[float] = Field(
        description="Box of the whole diagram (its drawing and all its labels), in the format of the instructions."
    )


class Extraction(Deck):
    """Cards read from the photos, a line of text on each photo (to save sideways
    photos upright) and the frame of each diagram."""

    text_lines: list[TextLine] = Field(
        default_factory=list,
        description="For each photo: its longest line of printed text (a title, a sentence).",
    )
    frames: list[Frame] = Field(
        default_factory=list, description="For each photo with diagram cards: the frame of the diagram."
    )
    back_language: str = Field(
        default="",
        description="Language of the card backs as a BCP 47 code, in the variety the lesson teaches "
        '(e.g. "es-ES", "en-GB", "de-DE"); empty when the backs aren\'t in a language being learned.',
    )
    choice: str = Field(
        default="",
        description="Whenever the instructions let you choose the kind of cards: one short sentence, in the "
        'language of the instructions, saying what you made (e.g. "Spanish vocabulary: French → Spanish"). '
        "Otherwise empty.",
    )


class Revision(Deck):
    """Cards after a natural-language correction, with what changed."""

    summary: str = Field(description="One short sentence describing what changed, in the requested language.")


class LessonIn(Deck):
    # Voice for the back: "es-ES-ElviraNeural" → edge-tts mp3 embedded in the package;
    # "es_ES" → {{tts}} tag read by Anki itself; empty → no sound.
    voice: str = ""
    reverse: bool = False  # adds the reverse card (back → front)
    typing: bool = False  # the answer is typed, Anki compares it letter by letter
    dictation: bool = False  # adds a dictation card: hear the back, type it (needs a voice)
    # Whether every Anki profile sees the lesson (None in an update = unchanged).
    # Only the owner's profile may change it; the owner itself never changes.
    shared: bool | None = None
    # Diagram frames: what Anki shows of each photo (None in an update = unchanged)
    frames: list[Frame] | None = None
    # The prompt it was generated with and what the AI chose, sent back when a
    # regeneration is undone (None in an update = unchanged)
    prompt: str | None = None
    choice: str | None = None


class AiCall(BaseModel):
    """One request to the AI for a lesson: what it used and cost."""

    at: str
    kind: Literal["extract", "revise", "picture"]
    provider: str  # gemini, anthropic; the service's host for the OpenAI-like ones ("openrouter.ai")
    model: str  # the model that answered (a fallback, if the main one was overloaded)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None  # US dollars; None: unknown
    exact: bool = False  # cost given by the service (OpenRouter), not estimated from public prices


class Lesson(LessonIn):
    id: str  # folder name, e.g. "2026-09-28-spanish-lesson-5"
    prompt: str
    owner: str = ""  # Anki profile open when the lesson was created ("" = none: shared)
    shared: bool = False
    photo_count: int
    frames: list[Frame] = []  # fractions of the photos: from the AI, then as the user set them
    ai_calls: list[AiCall] = []  # generation, then corrections
    choice: str = ""  # what the AI chose to make, when the prompt let it choose
    created_at: str
    updated_at: str
    exported_at: str | None = None


class LessonSummary(BaseModel):
    id: str
    deck: str
    owner: str = ""
    shared: bool = False
    card_count: int
    photo_count: int
    frames: list[Frame] = []  # fractions of the photos: from the AI, then as the user set them
    ai_calls: list[AiCall] = []  # generation, then corrections
    created_at: str
    updated_at: str
    exported_at: str | None = None


class RevisionRequest(LessonIn):
    """Current (possibly unsaved) state of the lesson + the correction to apply."""

    instruction: str = Field(min_length=1, max_length=2000)


class ExportRequest(LessonIn):
    lesson_id: str | None = None  # saved lesson to update and mark as exported


class PromptIn(BaseModel):
    name: str
    text: str
    deck: str = ""
    voice: str = ""  # see LessonIn.voice
    typing: bool = False  # the lessons made with it start with these options (see LessonIn)
    dictation: bool = False


class Prompt(PromptIn):
    id: int | str  # the user's: a number; Notosaurus's: "notosaurus:<key>"
    builtin: bool = False  # Notosaurus's own: read-only, can be duplicated
    used_at: str | None = None  # last generation that used this prompt


class SettingsUpdate(BaseModel):
    """Admin page form: None leaves a field unchanged ("" clears an API key)."""

    llm: Literal["gemini", "anthropic", "openai", "openrouter", "compatible", "fake"] | None = None
    model: str | None = None
    fallback_models: str | None = None
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    compatible_base_url: str | None = Field(default=None, pattern=r"^(https?://.*)?$")
    compatible_api_key: str | None = None
    tts_rate: str | None = Field(default=None, pattern=r"^[+-]\d{1,2}%$")
    ankiconnect_url: str | None = Field(default=None, pattern=r"^https?://")
    ankiconnect_key: str | None = None
    anki_sync: bool | None = None
    instructions: str | None = Field(default=None, max_length=4000)
    profile_instructions: dict[str, str] | None = None  # replaces them all ("" removes one)
    picture_service: Literal["", "gemini", "openai", "openrouter", "none"] | None = None
    picture_model: str | None = None


class PictureRequest(BaseModel):
    """Draw a card's picture again, with what to draw (None: the card's own): its subject,
    or a figure's description."""

    subject: str | None = Field(default=None, max_length=1000)


class LessonAccess(BaseModel):
    """Settings page: who a lesson belongs to. None leaves a field unchanged;
    owner "" = no owner (the lesson is everyone's)."""

    owner: str | None = Field(default=None, max_length=200)
    shared: bool | None = None


class AdminPassword(BaseModel):
    current: str | None = None
    new: str = Field(min_length=4)
