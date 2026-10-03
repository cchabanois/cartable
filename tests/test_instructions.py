"""Standing instructions for the AI, for everyone and per Anki profile."""

import json
import types

from conftest import ADMIN

from app import ankiconnect, llm


def test_saved_in_the_settings(admin):
    body = {"instructions": " Spanish from Spain ", "profile_instructions": {"Léa": "year 8", "Paul": "  "}}
    saved = admin.put("/api/admin/settings", headers=ADMIN, json=body).json()
    assert (saved["instructions"], saved["profile_instructions"]) == ("Spanish from Spain", {"Léa": "year 8"})
    # Other settings saved later leave them alone
    assert admin.put("/api/admin/settings", headers=ADMIN, json={"tts_rate": "-5%"}).json()["profile_instructions"] == {
        "Léa": "year 8"
    }


def test_sent_with_the_request_for_the_profile(admin, monkeypatch):
    admin.put(
        "/api/admin/settings",
        headers=ADMIN,
        json={
            "instructions": "Spanish from Spain",
            "profile_instructions": {"Léa": "Léa is in year 8", "Paul": "year 6"},
        },
    )
    monkeypatch.setenv("NOTOSAURUS_LLM", "openai")
    monkeypatch.setenv("NOTOSAURUS_MODEL", "gpt-6.1-sol")
    sent = []

    class Completions:
        async def create(self, **kwargs):
            sent.append(kwargs["messages"][-1]["content"][-1]["text"])
            content = json.dumps({"deck": "D", "cards": [{"front": "a", "back": "b"}]})
            message = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(usage=None, model="m", choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(
        llm, "_openai_client", lambda s: types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
    )

    async def lea():
        return "Léa"

    monkeypatch.setattr(ankiconnect, "active_profile", lea)
    lesson = admin.post("/api/extract", data={"prompt": "words: le chat"}).json()
    assert "Spanish from Spain" in sent[0] and "Léa is in year 8" in sent[0] and "year 6" not in sent[0]
    assert sent[0].index("Léa is in year 8") < sent[0].index("Instructions: words: le chat")  # before the request

    # A correction: the instructions of the lesson's owner
    body = {"deck": "D", "cards": lesson["cards"], "instruction": "add one"}
    admin.post(f"/api/lessons/{lesson['id']}/revise", json=body)
    assert "Léa is in year 8" in sent[1]


def test_nothing_added_without_instructions():
    s = llm.settings.Settings()
    assert llm.standing_instructions(s, "Léa") == ""
    s = llm.settings.Settings(profile_instructions={"Paul": "year 6"})
    assert llm.standing_instructions(s, "Léa") == "" and llm.standing_instructions(s, None) == ""
