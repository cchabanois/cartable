"""Fixtures shared by the tests."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ankiconnect, prices, tts
from app.main import app

synthesized = []


async def fake_synthesize(text, voice, path):
    if "ÉCHEC" in text:
        raise RuntimeError("network down")
    synthesized.append(text)
    path.write_bytes(b"ID3fake mp3 " + text.encode())


# Public model prices (OpenRouter's list), per token: no network in the tests
PRICES = {
    "openai/gpt-6.1-sol": ("0.000002", "0.00001"),
    "google/gemini-3.8-flash": ("0.00000075", "0.00000375"),
    "anthropic/claude-sonnet-4.6": ("0.000003", "0.000015"),
}


def model_list(request):
    data = [{"id": i, "pricing": {"prompt": p, "completion": c}} for i, (p, c) in PRICES.items()]
    return httpx.Response(200, json={"data": data})


def anki_unreachable(request):
    raise httpx.ConnectError("refused")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTABLE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CARTABLE_LLM", "fake")
    monkeypatch.setattr(tts, "_synthesize", fake_synthesize)
    # Never reach a real Anki from the tests: AnkiConnect is "unreachable" unless a test fakes it.
    monkeypatch.setattr(ankiconnect, "_transport", httpx.MockTransport(anki_unreachable))
    monkeypatch.setattr(prices, "_transport", httpx.MockTransport(model_list))
    synthesized.clear()
    with TestClient(app) as c:
        yield c


ADMIN = {"X-Admin-Password": "secret"}


@pytest.fixture
def admin(client):
    assert client.get("/api/admin").json()["password_set"] is False
    assert client.post("/api/admin/password", json={"new": "secret"}).status_code == 204
    return client
