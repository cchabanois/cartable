from typing import Literal

from pydantic import BaseModel, Field


class Card(BaseModel):
    front: str = Field(description="Front: what is shown first (e.g. the word in the pupil's language).")
    back: str = Field(description="Back: the answer (e.g. the translation in the language being learned).")
    info: str = Field(default="", description="Useful extra info: gender, plural, example… Empty if none.")
    subdeck: str = Field(
        default="", description='Sub-deck (e.g. "Vocabulary", "Conjugation"). Empty for a single deck.'
    )
    tags: list[str] = Field(default_factory=list)


class Deck(BaseModel):
    deck: str = Field(description='Anki deck name, with :: for hierarchy (e.g. "Spanish::Lesson 5 - Family").')
    cards: list[Card]


class Revision(Deck):
    """Cards after a natural-language correction, with what changed."""

    summary: str = Field(description="One short sentence describing what changed, in the requested language.")


class LessonIn(Deck):
    # Voice for the back: "es-ES-ElviraNeural" → edge-tts mp3 embedded in the package;
    # "es_ES" → {{tts}} tag read by Anki itself; empty → no sound.
    voice: str = ""
    reverse: bool = False  # adds the reverse card (back → front)
    # Whether every Anki profile sees the lesson (None in an update = unchanged).
    # Only the owner's profile may change it; the owner itself never changes.
    shared: bool | None = None


class Lesson(LessonIn):
    id: str  # folder name, e.g. "2026-09-28-spanish-lesson-5"
    prompt: str
    owner: str = ""  # Anki profile open when the lesson was created ("" = none: shared)
    shared: bool = False
    photo_count: int
    created_at: str
    updated_at: str
    exported_at: str | None = None

    def visible_to(self, profile: str | None) -> bool:
        """Private lessons are only for their owner's profile."""
        return self.shared or not self.owner or self.owner == profile


class LessonSummary(BaseModel):
    id: str
    deck: str
    owner: str = ""
    shared: bool = False
    card_count: int
    photo_count: int
    created_at: str
    updated_at: str
    exported_at: str | None = None


class RevisionRequest(LessonIn):
    """Current (possibly unsaved) state of the lesson + the correction to apply."""

    instruction: str = Field(min_length=1, max_length=2000)


class ExportRequest(LessonIn):
    lesson_id: str | None = None  # saved lesson to update and mark as exported
    force: bool = False  # send even if another Anki profile than the lesson's is open


class PromptIn(BaseModel):
    name: str
    text: str
    deck: str = ""
    voice: str = ""  # see LessonIn.voice


class Prompt(PromptIn):
    id: int
    used_at: str | None = None  # last generation that used this prompt


class SettingsUpdate(BaseModel):
    """Admin page form: None leaves a field unchanged ("" clears an API key)."""

    llm: Literal["gemini", "anthropic", "openai", "fake"] | None = None
    model: str | None = None
    fallback_models: str | None = None
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    tts_rate: str | None = Field(default=None, pattern=r"^[+-]\d{1,2}%$")
    ankiconnect_url: str | None = Field(default=None, pattern=r"^https?://")
    ankiconnect_key: str | None = None
    anki_sync: bool | None = None
    all_profiles_view: bool | None = None


class LessonAccess(BaseModel):
    """Settings page: who a lesson belongs to. None leaves a field unchanged;
    owner "" = no owner (the lesson is everyone's)."""

    owner: str | None = Field(default=None, max_length=200)
    shared: bool | None = None


class AdminPassword(BaseModel):
    current: str | None = None
    new: str = Field(min_length=4)
