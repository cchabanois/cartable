"""Fixtures shared by the tests."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ankiconnect, tts
from app.main import app

synthesized = []


async def fake_synthesize(text, voice, path):
    if "ÉCHEC" in text:
        raise RuntimeError("network down")
    synthesized.append(text)
    path.write_bytes(b"ID3fake mp3 " + text.encode())


def anki_unreachable(request):
    raise httpx.ConnectError("refused")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTABLE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CARTABLE_LLM", "fake")
    monkeypatch.setattr(tts, "_synthesize", fake_synthesize)
    # Never reach a real Anki from the tests: AnkiConnect is "unreachable" unless a test fakes it.
    monkeypatch.setattr(ankiconnect, "_transport", httpx.MockTransport(anki_unreachable))
    synthesized.clear()
    with TestClient(app) as c:
        yield c
