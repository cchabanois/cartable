"""What each lesson's AI calls used and cost."""

import asyncio
import json
import types

import pytest

from app import llm, prices


def test_fake_calls_kept_with_the_lesson(client):
    files = [("images", ("p.png", b"img", "image/png"))]
    lesson = client.post("/api/extract", files=files, data={"prompt": "FR → ES"}).json()
    assert [(c["kind"], c["provider"], c["cost"]) for c in lesson["ai_calls"]] == [("extract", "fake", 0.0)]
    body = {"deck": lesson["deck"], "cards": lesson["cards"], "instruction": "add the colours"}
    client.post(f"/api/lessons/{lesson['id']}/revise", json=body)
    client.post(f"/api/lessons/{lesson['id']}/revise", json=body)
    saved = client.get(f"/api/lessons/{lesson['id']}").json()
    assert [c["kind"] for c in saved["ai_calls"]] == ["extract", "revise", "revise"]
    # An edit afterwards keeps them
    client.put(f"/api/lessons/{lesson['id']}", json={"deck": "D", "cards": []})
    assert len(client.get("/api/lessons").json()[0]["ai_calls"]) == 3


@pytest.mark.parametrize(
    ("provider", "model", "cost"),
    [
        ("openai", "gpt-6.1-sol", 1000 * 0.000002 + 100 * 0.00001),
        ("gemini", "gemini-3.8-flash", 1000 * 0.00000075 + 100 * 0.00000375),
        ("anthropic", "claude-sonnet-4-6", 1000 * 0.000003 + 100 * 0.000015),  # listed as claude-sonnet-4.6
        ("openai", "google/gemini-3.8-flash", 1000 * 0.00000075 + 100 * 0.00000375),  # an OpenRouter id
        ("anthropic", "claude-unknown", None),
    ],
)
def test_estimated_cost(client, provider, model, cost):
    estimate = asyncio.run(prices.estimate(provider, model, 1000, 100))
    assert estimate == (pytest.approx(cost) if cost is not None else None)


def test_prices_fetched_once_a_day(client, monkeypatch):
    asyncio.run(prices.estimate("openai", "gpt-6.1-sol", 1, 1))
    monkeypatch.setattr(prices, "_transport", None)  # the network would now be used: the cache must answer
    assert asyncio.run(prices.estimate("openai", "gpt-6.1-sol", 1000, 0)) == pytest.approx(0.002)


def fake_completion(cost=None, model="google/gemini-3.8-flash", content=None):
    usage = types.SimpleNamespace(prompt_tokens=2000, completion_tokens=300, model_extra={"cost": cost} if cost else {})
    content = content or json.dumps({"deck": "D", "cards": [{"front": "a", "back": "b"}]})
    message = types.SimpleNamespace(content=content)
    response = types.SimpleNamespace(usage=usage, model=model, choices=[types.SimpleNamespace(message=message)])

    class Completions:
        async def create(self, **kwargs):
            Completions.kwargs = kwargs
            return response

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions())), Completions


def test_openrouter_gives_the_exact_cost(client, monkeypatch):
    monkeypatch.setenv("CARTABLE_LLM", "openrouter")
    monkeypatch.setenv("CARTABLE_MODEL", "google/gemini-3.8-flash")
    fake, completions = fake_completion(cost=0.0123)
    monkeypatch.setattr(llm, "_openai_client", lambda s: fake)
    lesson = client.post("/api/extract", data={"prompt": "words: le chat"}).json()
    (call,) = lesson["ai_calls"]
    assert call | {"at": None} == {
        "at": None,
        "kind": "extract",
        "provider": "openrouter.ai",
        "model": "google/gemini-3.8-flash",
        "input_tokens": 2000,
        "output_tokens": 300,
        "cost": 0.0123,
        "exact": True,
    }
    assert completions.kwargs["extra_body"] == {"usage": {"include": True}}  # asked for the cost


def test_other_services_get_an_estimate(client, monkeypatch):
    monkeypatch.setenv("CARTABLE_LLM", "openai")
    monkeypatch.setenv("CARTABLE_MODEL", "gpt-6.1-sol")
    fake, completions = fake_completion(model="gpt-6.1-sol")
    monkeypatch.setattr(llm, "_openai_client", lambda s: fake)
    (call,) = client.post("/api/extract", data={"prompt": "words: le chat"}).json()["ai_calls"]
    assert (call["cost"], call["exact"]) == (pytest.approx(2000 * 0.000002 + 300 * 0.00001), False)
    assert completions.kwargs["extra_body"] is None


def test_deleting_a_lesson_keeps_what_was_spent(admin, monkeypatch, tmp_path):
    from conftest import ADMIN

    monkeypatch.setenv("CARTABLE_LLM", "openrouter")
    monkeypatch.setenv("CARTABLE_MODEL", "google/gemini-3.8-flash")
    fake, _ = fake_completion(cost=0.01)
    monkeypatch.setattr(llm, "_openai_client", lambda s: fake)
    first = admin.post("/api/extract", data={"prompt": "words: le chat"}).json()
    admin.post("/api/extract", data={"prompt": "words: le chien"})

    def costs():
        return admin.get("/api/admin/lessons", headers=ADMIN).json()["costs"]

    assert costs() == {"total": 0.02, "deleted": 0.0, "failed": 0.0, "estimated": False, "unknown": 0}
    admin.delete(f"/api/lessons/{first['id']}")
    assert costs() == {"total": 0.02, "deleted": 0.01, "failed": 0.0, "estimated": False, "unknown": 0}

    # Answered, but unusable: paid for, no lesson
    broken, _ = fake_completion(cost=0.005, content="not json")
    monkeypatch.setattr(llm, "_openai_client", lambda s: broken)
    assert admin.post("/api/extract", data={"prompt": "words"}).status_code == 502
    assert (costs()["total"], costs()["failed"]) == (0.025, 0.005)
    journal = json.loads((tmp_path / "data" / "ai-calls.json").read_text(encoding="utf-8"))["calls"]
    assert [e["lesson_id"] is None for e in journal] == [False, False, True]


def test_journal_starts_from_the_lessons(client, tmp_path):
    files = [("images", ("p.png", b"img", "image/png"))]
    lesson = client.post("/api/extract", files=files, data={"prompt": "FR → ES"}).json()
    journal = tmp_path / "data" / "ai-calls.json"
    journal.unlink()  # as before this version: the calls are only in the lessons
    from app import usage

    assert usage.totals().total == 0.0  # fake calls cost nothing, but they are back in the journal
    assert len(json.loads(journal.read_text(encoding="utf-8"))["calls"]) == 1
    body = {"deck": lesson["deck"], "cards": lesson["cards"], "instruction": "x"}
    client.post(f"/api/lessons/{lesson['id']}/revise", json=body)
    entries = json.loads(journal.read_text(encoding="utf-8"))["calls"]
    assert [e["kind"] for e in entries] == ["extract", "revise"]  # no duplicate
