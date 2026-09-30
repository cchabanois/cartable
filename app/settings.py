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

PROVIDERS = ("gemini", "anthropic", "openai", "fake")

DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "anthropic": "claude-opus-5",
    "openai": "",  # depends on the service (OpenAI, OpenRouter…): chosen in the admin page
    "fake": "",
}

SECRET_FIELDS = ("gemini_api_key", "anthropic_api_key", "openai_api_key", "ankiconnect_key")

# Settings field → environment variable giving its default value.
ENV = {
    "llm": "CARTABLE_LLM",
    "model": "CARTABLE_MODEL",
    "fallback_models": "CARTABLE_FALLBACK_MODEL",
    "gemini_api_key": "GEMINI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_base_url": "CARTABLE_OPENAI_BASE_URL",
    "openai_api_key": "CARTABLE_OPENAI_API_KEY",
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
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""  # default key (.env), for services without a key of their own
    openai_keys: dict[str, str] = {}  # service address → its key: OpenAI, OpenRouter…
    tts_rate: str = "-10%"
    ankiconnect_url: str = "http://localhost:8765"  # Anki desktop with the AnkiConnect add-on
    ankiconnect_key: str = ""  # AnkiConnect "apiKey", if one is configured
    anki_sync: bool = True  # sync with AnkiWeb after sending, so phones get the cards
    # "All profiles" toggle in "My lessons": off = a profile never sees another
    # profile's private lessons (enforced by the server, not only hidden).
    all_profiles_view: bool = True

    def model_for_provider(self) -> str:
        return self.model.strip() or DEFAULT_MODELS.get(self.llm, "")

    def openai_key(self) -> str:
        """Key of the OpenAI-compatible service at `openai_base_url`."""
        return self.openai_keys.get(service_id(self.openai_base_url)) or self.openai_api_key


def service_id(url: str) -> str:
    """Normalized service address, the key of `openai_keys`."""
    return url.strip().rstrip("/").lower()


def _path():
    return storage.data_dir() / "settings.json"


def _stored() -> dict:
    stored = storage.read_json(_path(), default={})
    # Before keys per service, the OpenAI-compatible key was saved on its own:
    # it belongs to the service saved with it, not to every service.
    legacy = stored.pop("openai_api_key", None)
    if legacy:
        url = stored.get("openai_base_url") or Settings().openai_base_url
        stored["openai_keys"] = {service_id(url): legacy, **stored.get("openai_keys", {})}
    return stored


# Set by the Anki add-on: the server talks to the add-on's bridge inside Anki,
# whose address and key change at every start and must win over saved values.
EMBEDDED_FIELDS = ("ankiconnect_url", "ankiconnect_key")


def embedded() -> bool:
    return os.environ.get("CARTABLE_EMBEDDED") == "1"


def current() -> Settings:
    """Defaults ← environment ← saved values (← the add-on's bridge, when embedded)."""
    values = {field: os.environ[var] for field, var in ENV.items() if var in os.environ}
    values.update({k: v for k, v in _stored().items() if k in Settings.model_fields})
    if embedded():
        values.update({f: os.environ[ENV[f]] for f in EMBEDDED_FIELDS if ENV[f] in os.environ})
    return Settings(**values)


def save(changes: dict) -> Settings:
    """Save the given fields; None means "leave unchanged".

    `openai_api_key` is the key of the service being saved (its address after
    these changes): each OpenAI-compatible service keeps its own key."""
    changes = dict(changes)
    openai_key = changes.pop("openai_api_key", None)
    with storage.lock:
        stored = _stored()
        if openai_key is not None:
            url = changes.get("openai_base_url") or stored.get("openai_base_url") or current().openai_base_url
            keys = dict(stored.get("openai_keys", {}))
            if openai_key.strip():
                keys[service_id(url)] = openai_key.strip()
            else:
                keys.pop(service_id(url), None)
            stored["openai_keys"] = keys
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
    storage.write_json(_path(), stored)
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


def masked(value: str) -> str:
    """ "AIzaSyD…a1b2" → "•••• a1b2" (empty stays empty)."""
    return f"•••• {value[-4:]}" if value else ""
