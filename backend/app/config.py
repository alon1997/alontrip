"""Application settings, loaded from environment variables / .env.

D-010 zero-key mode: SerpApi and DeepSeek keys are independently optional —
missing one must not block the other, and both may be empty for a fully
offline run. MySQL is likewise optional: an empty password means "don't use
MySQL", not "fail to start".

Setup: copy .env.example to .env and fill in real values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# CORS allowlist defaults: Vite dev (5173) + preview (4173)
DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://localhost:4173"

# Local layout: 04app/.env (parents[2]). Server layout: trip_api/backend/.env
# (parents[1]). Prefer whichever file exists so a deploy does not silently
# drop SerpApi / DeepSeek keys.
_ENV_CANDIDATES = (
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[1] / ".env",
)
_ENV_FILE = next((path for path in _ENV_CANDIDATES if path.is_file()), _ENV_CANDIDATES[0])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")

    serpapi_key: str = ""
    amap_key: str = ""  # Amap (Gaode) web service; mainland-China transit. Empty = mainland legs fall back to the taxi estimate, never Google
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/anthropic"
    app_port: int = 5003
    cors_origins: str = DEFAULT_CORS_ORIGINS

    # Independent MySQL (hackathontrip only, D-006/D-014). Empty password
    # means "no database configured" — callers fall back to local JSON.
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "hackathontrip"
    mysql_user: str = "hackathontrip"
    mysql_password: str = ""


@lru_cache
def get_settings() -> Settings:
    """Always returns a Settings instance — every field has a default, so
    this never raises. Zero-key/zero-db mode is the normal case, not an
    error path.
    """
    return Settings()


def get_settings_optional() -> Settings | None:
    """Back-compat shim: callers that pre-date the all-optional Settings
    still call this expecting "None means unconfigured". Now that every
    field has a default, this is just an alias for :func:`get_settings`.
    """
    return get_settings()


def get_cors_origins() -> list[str]:
    """Comma-separated allowed browser origins (CORS_ORIGINS env var)."""
    raw = get_settings().cors_origins
    return [origin.strip() for origin in raw.split(",") if origin.strip()]
