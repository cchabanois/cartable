"""Saved prompts, all in data/prompts.json.

The first time, it is filled with the "defaultPrompts" of the page's language
(static/i18n/<lang>.json).
"""

from . import i18n, storage
from .models import Prompt, PromptIn


def _path():
    return storage.data_dir() / "prompts.json"


def _load(lang: str = i18n.DEFAULT) -> list[Prompt]:
    data = storage.read_json(_path())
    if data is None:  # first start: seed the default prompts
        defaults = i18n.get(lang, "defaultPrompts", [])
        prompts = [Prompt(id=i, **PromptIn(**p).model_dump()) for i, p in enumerate(defaults, start=1)]
        _save(prompts)
        return prompts
    return [Prompt(**p) for p in data]


def _save(prompts: list[Prompt]) -> None:
    storage.write_json(_path(), [p.model_dump() for p in prompts])


def list_all(lang: str = i18n.DEFAULT) -> list[Prompt]:
    with storage.lock:
        return _load(lang)


def get(id: int) -> Prompt | None:
    return next((p for p in list_all() if p.id == id), None)


def add(p: PromptIn) -> Prompt:
    with storage.lock:
        prompts = _load()
        prompt = Prompt(id=max((x.id for x in prompts), default=0) + 1, **p.model_dump())
        _save([*prompts, prompt])
    return prompt


def update(id: int, p: PromptIn) -> Prompt | None:
    with storage.lock:
        prompts = _load()
        for i, old in enumerate(prompts):
            if old.id == id:
                prompts[i] = old.model_copy(update=p.model_dump())
                _save(prompts)
                return prompts[i]
    return None


def mark_used(id: int) -> None:
    """Remember when a prompt was last used, to list recent prompts first."""
    with storage.lock:
        prompts = _load()
        for p in prompts:
            if p.id == id:
                p.used_at = storage.now()
                _save(prompts)


def delete(id: int) -> bool:
    with storage.lock:
        prompts = _load()
        kept = [p for p in prompts if p.id != id]
        if len(kept) == len(prompts):
            return False
        _save(kept)
    return True
