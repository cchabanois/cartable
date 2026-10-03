"""Runtime settings, edited from the admin page and saved in data/settings.json.

Environment variables (.env) give the defaults; values saved from the admin page
override them. API keys are stored in clear text in that file (readable by the
server user only) and never sent back to the browser.
"""

import hashlib
import hmac
import os
import secrets

from pydantic import BaseModel

from . import storage

PROVIDERS = ("gemini", "anthropic", "openai", "openrouter", "compatible", "fake")

# The three providers speaking OpenAI's API: OpenAI itself, OpenRouter (every
# provider's models with one key), and any other compatible service at its address
# (Ollama, LM Studio, Mistral…).
OPENAI_URL = "https://api.openai.com/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENAI_LIKE = ("openai", "openrouter", "compatible")

DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "anthropic": "claude-opus-5",
    "openai": "gpt-6-luna",
    "openrouter": "~google/gemini-flash-latest",  # always the latest Gemini Flash
    "compatible": "",  # depends on the service: chosen in the admin page
    "fake": "",
}

SECRET_FIELDS = (
    "gemini_api_key",
    "anthropic_api_key",
    "openai_api_key",
    "openrouter_api_key",
    "compatible_api_key",
    "ankiconnect_key",
)

# Settings field → environment variable giving its default value.
ENV = {
    "llm": "CARTABLE_LLM",
    "model": "CARTABLE_MODEL",
    "fallback_models": "CARTABLE_FALLBACK_MODEL",
    "gemini_api_key": "GEMINI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "compatible_base_url": "CARTABLE_COMPATIBLE_BASE_URL",
    "compatible_api_key": "CARTABLE_COMPATIBLE_API_KEY",
    "tts_rate": "CARTABLE_TTS_RATE",
    "ankiconnect_url": "CARTABLE_ANKICONNECT_URL",
    "ankiconnect_key": "CARTABLE_ANKICONNECT_KEY",
}


class Settings(BaseModel):
    llm: str = "gemini"
    model: str = ""  # empty = the provider's default model
    fallback_models: str = "gemini-3.5-flash-lite"  # Gemini only, comma-separated
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""  # OpenAI itself
    openrouter_api_key: str = ""
    compatible_base_url: str = ""  # e.g. http://localhost:11434/v1 (Ollama)
    compatible_api_key: str = ""  # often none (local servers)
    tts_rate: str = "-10%"
    ankiconnect_url: str = "http://localhost:8765"  # Anki desktop with the AnkiConnect add-on
    ankiconnect_key: str = ""  # AnkiConnect "apiKey", if one is configured
    anki_sync: bool = True  # sync with AnkiWeb after sending, so phones get the cards
    # Added to the AI's fixed rules (never replacing them): for everyone, and per Anki
    # profile, e.g. "Bastien is in year 8", "short answers, with the article".
    instructions: str = ""
    profile_instructions: dict[str, str] = {}
    # Card pictures: the service drawing them ("" = the cards' own when it draws, see
    # pictures.service; "none" = no pictures) and its model ("" = that service's default)
    picture_service: str = ""
    picture_model: str = ""

    def model_for_provider(self) -> str:
        return self.model.strip() or DEFAULT_MODELS.get(self.llm, "")

    def base_url(self) -> str:
        """Address of the OpenAI-like provider in use."""
        return {"openai": OPENAI_URL, "openrouter": OPENROUTER_URL}.get(self.llm, self.compatible_base_url.strip())

    def api_key(self) -> str:
        """Key of the OpenAI-like provider in use."""
        return {
            "openai": self.openai_api_key,
            "openrouter": self.openrouter_api_key,
            "compatible": self.compatible_api_key,
        }.get(self.llm, "")


def _path():
    return storage.data_dir() / "settings.json"


def _same_url(a: str, b: str) -> bool:
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def _split_openai(values: dict, url: str | None, keys: dict[str, str]) -> None:
    """Before OpenAI, OpenRouter and the compatible services were providers of their
    own, they were one ("openai") with an address and a key per address: the same
    settings, put where they belong now. `url`: the address that was chosen."""
    for address, key in keys.items():
        if _same_url(address, OPENAI_URL):
            values.setdefault("openai_api_key", key)
        elif _same_url(address, OPENROUTER_URL):
            values.setdefault("openrouter_api_key", key)
        elif url and _same_url(address, url):
            values.setdefault("compatible_api_key", key)
    if url is None:
        return
    if _same_url(url, OPENROUTER_URL):
        chosen = "openrouter"
    elif _same_url(url, OPENAI_URL):
        chosen = "openai"
    else:
        chosen = "compatible"
        values.setdefault("compatible_base_url", url)
    if values.get("llm") == "openai":
        values["llm"] = chosen


# settings.json's format; storage.migrate brings older files up to it
FORMAT = 1


def _format_1(stored: dict) -> dict:
    """Format 0 → 1: OpenAI, OpenRouter and the compatible services become providers of
    their own; the pictures get a service of their own."""
    stored = dict(stored)
    if "openai_base_url" in stored or "openai_keys" in stored:  # one provider, a key per address
        url = stored.pop("openai_base_url", None) or OPENAI_URL
        keys = dict(stored.pop("openai_keys", {}))
        if "openai_api_key" in stored:  # older still: one key, for the address saved with it
            keys.setdefault(url, stored.pop("openai_api_key"))
        _split_openai(stored, url, keys)
    if stored.get("picture_model") and "picture_service" not in stored:  # its service was told by its name
        stored["picture_service"] = _picture_service_of(stored["picture_model"])
    return stored


def _stored() -> dict:
    stored = storage.read_json(_path(), default=None)
    if stored is None:
        return {"format": FORMAT}
    return storage.migrate(stored, "settings", FORMAT, {0: _format_1})


def _picture_service_of(model: str) -> str:
    """The service a picture model was used through, before it was a setting: told by
    its name ("google/…" OpenRouter, "gemini-…" Gemini, "gpt-image…" OpenAI)."""
    if "/" in model:
        return "openrouter"
    return "gemini" if model.startswith("gemini") else "openai"


def _environment() -> dict:
    values = {field: os.environ[var] for field, var in ENV.items() if var in os.environ}
    # Before the split: CARTABLE_OPENAI_BASE_URL and CARTABLE_OPENAI_API_KEY (.env)
    url = os.environ.get("CARTABLE_OPENAI_BASE_URL")
    if url or "CARTABLE_OPENAI_API_KEY" in os.environ:
        url = url or OPENAI_URL
        key = os.environ.get("CARTABLE_OPENAI_API_KEY")
        _split_openai(values, url, {url: key} if key else {})
    return values


# Set by the Anki add-on: the server talks to the add-on's bridge inside Anki,
# whose address and key change at every start and must win over saved values.
EMBEDDED_FIELDS = ("ankiconnect_url", "ankiconnect_key")


def embedded() -> bool:
    return os.environ.get("CARTABLE_EMBEDDED") == "1"


def current() -> Settings:
    """Defaults ← environment ← saved values (← the add-on's bridge, when embedded)."""
    values = _environment()
    values.update({k: v for k, v in _stored().items() if k in Settings.model_fields})
    if embedded():
        values.update({f: os.environ[ENV[f]] for f in EMBEDDED_FIELDS if ENV[f] in os.environ})
    return Settings(**values)


def save(changes: dict) -> Settings:
    """Save the given fields; None means "leave unchanged" ("" clears an API key)."""
    changes = dict(changes)
    with storage.lock:
        stored = _stored()
        if changes.get("profile_instructions") is not None:  # empty ones aren't kept
            changes["profile_instructions"] = {
                profile: text.strip() for profile, text in changes["profile_instructions"].items() if text.strip()
            }
        stored.update(
            {
                k: v.strip() if isinstance(v, str) else v
                for k, v in changes.items()
                if k in Settings.model_fields and v is not None
            }
        )
        Settings(**{**current().model_dump(), **stored})  # validate before writing
        _write(stored)
    return current()


def _write(stored: dict) -> None:
    storage.write_json(_path(), {**stored, "format": FORMAT})
    os.chmod(_path(), 0o600)  # API keys inside


# --- Admin password ---------------------------------------------------------


def password_is_set() -> bool:
    return bool(_stored().get("admin_password"))


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def set_password(password: str) -> None:
    salt = secrets.token_hex(16)
    with storage.lock:
        stored = _stored()
        stored["admin_password"] = f"pbkdf2-sha256${salt}${_hash(password, salt)}"
        _write(stored)


def check_password(password: str | None) -> bool:
    stored = _stored().get("admin_password", "")
    if not password or not stored:
        return False
    _, salt, expected = stored.split("$")
    return hmac.compare_digest(_hash(password, salt), expected)


# --- Paired devices -----------------------------------------------------------
# Phones and other computers reach Cartable over the Wi-Fi: only those given this
# token (the QR code in Anki or in the settings) can use it. Changing it unpairs them.


def device_token() -> str:
    with storage.lock:
        stored = _stored()
        if not stored.get("device_token"):
            stored["device_token"] = secrets.token_urlsafe(24)
            _write(stored)
        return stored["device_token"]


def new_device_token() -> str:
    with storage.lock:
        stored = _stored()
        stored["device_token"] = secrets.token_urlsafe(24)
        _write(stored)
        return stored["device_token"]


def is_device_token(value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value, device_token())


def masked(value: str) -> str:
    """ "AIzaSyD…a1b2" → "•••• a1b2" (empty stays empty)."""
    return f"•••• {value[-4:]}" if value else ""
