"""Prompts: Cartable's own, and the user's.

- Cartable's prompts ("builtinPrompts" in static/i18n/<lang>.json): in the page's
  language, read-only, never deleted, improved with the app. Their ids are
  "cartable:<key>".
- The user's prompts, in data/prompts.json: added, changed, deleted, or copied from
  any prompt ("Duplicate") to be adapted.

data/prompts.json: {"user": [prompts], "builtin_used": {key: date}}. Before, it was
a list seeded once with the default prompts: read as the user's prompts, without the
old defaults left unchanged that a Cartable prompt now replaces.
"""

from . import i18n, storage
from .errors import AppError
from .models import Prompt, PromptIn

PREFIX = "cartable:"
BUILTIN = (
    "vocabulary",
    "sentences",
    "questions",
    "cloze",
    "formulas",
    "school_formulas",
    "diagram",
    "pictures",
    "wordlist",
    "dictation",
)

# Old default prompts that a Cartable prompt covers: dropped when left unchanged.
# (The old "FR → ES" ones carry a Spanish voice and deck name: kept as the user's.)
REPLACED = {
    "Crée des cartes question → réponse pour réviser le contenu de la leçon (dates, définitions, notions clés). "
    "Questions courtes et précises, réponses brèves, en français.",
    # the same, before the interface had languages
    "Crée des cartes question → réponse pour réviser le contenu de la leçon (dates, définitions, notions clés). "
    "Questions courtes et précises, réponses brèves.",
    "Create question → answer cards to review the content of the lesson (dates, definitions, key ideas). "
    "Short, precise questions and brief answers, in English.",
}


class BuiltinPrompt(AppError):
    status = 403


def _path():
    return storage.data_dir() / "prompts.json"


def _read() -> tuple[list[Prompt], dict[str, str]]:
    data = storage.read_json(_path())
    if data is None:
        return [], {}
    if isinstance(data, list):  # before Cartable's prompts
        return [Prompt(**p) for p in data if p.get("text") not in REPLACED], {}
    return [Prompt(**p) for p in data.get("user", [])], dict(data.get("builtin_used", {}))


def _write(user: list[Prompt], used: dict[str, str]) -> None:
    storage.write_json(_path(), {"user": [p.model_dump() for p in user], "builtin_used": used})


def _builtins(lang: str, used: dict[str, str]) -> list[Prompt]:
    texts = i18n.get(lang, "builtinPrompts", {})
    return [
        Prompt(id=PREFIX + key, builtin=True, used_at=used.get(key), **PromptIn(**texts[key]).model_dump())
        for key in BUILTIN
        if key in texts
    ]


def list_all(lang: str = i18n.DEFAULT) -> list[Prompt]:
    with storage.lock:
        user, used = _read()
    return [*_builtins(lang, used), *user]


def get(id: int | str, lang: str = i18n.DEFAULT) -> Prompt | None:
    return next((p for p in list_all(lang) if str(p.id) == str(id)), None)


def _user_id(id: int | str) -> int:
    """The id of a user prompt; Cartable's prompts can't be changed or deleted."""
    if str(id).startswith(PREFIX):
        raise BuiltinPrompt("prompt.builtin")
    try:
        return int(id)
    except ValueError as e:
        raise AppError("prompt.not_found", 404) from e


def add(p: PromptIn) -> Prompt:
    with storage.lock:
        user, used = _read()
        prompt = Prompt(id=max((x.id for x in user), default=0) + 1, **p.model_dump())
        _write([*user, prompt], used)
    return prompt


def update(id: int | str, p: PromptIn) -> Prompt | None:
    user_id = _user_id(id)
    with storage.lock:
        user, used = _read()
        for i, old in enumerate(user):
            if old.id == user_id:
                user[i] = old.model_copy(update=p.model_dump())
                _write(user, used)
                return user[i]
    return None


def duplicate(id: int | str, lang: str = i18n.DEFAULT) -> Prompt | None:
    """A copy of any prompt (Cartable's or the user's), as a new user prompt to adapt."""
    source = get(id, lang)
    if source is None:
        return None
    name = i18n.get(lang, "app.editor.copyName", "{name} (copy)").format(name=source.name)
    return add(PromptIn(**{**source.model_dump(include=set(PromptIn.model_fields)), "name": name}))


def mark_used(id: int | str) -> None:
    """Remember when a prompt was last used, to list recent prompts first."""
    with storage.lock:
        user, used = _read()
        if str(id).startswith(PREFIX):
            used[str(id).removeprefix(PREFIX)] = storage.now()
        else:
            for p in user:
                if str(p.id) == str(id):
                    p.used_at = storage.now()
        _write(user, used)


def delete(id: int | str) -> bool:
    user_id = _user_id(id)
    with storage.lock:
        user, used = _read()
        kept = [p for p in user if p.id != user_id]
        if len(kept) == len(user):
            return False
        _write(kept, used)
    return True
