"""Configuration loading from the single master env file.

All secrets/config come from ~/.config/gmail-triage-bot/.env. Required keys fail loud
(ConfigError naming the missing key) so a misconfigured deploy never silently
runs with empty credentials.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

MASTER_ENV_PATH = Path.home() / ".config" / "gmail-triage-bot" / ".env"

# Keys always required. Gmail auth is validated separately: either a long-lived
# GMAIL_REFRESH_TOKEN (the production flow) OR a short-lived GMAIL_ACCESS_TOKEN
# (a ~1h token minted via the OAuth Playground, enough to drive a live proof run).
# Note: the Telegram bot token is resolved separately (see _require_telegram_token)
# so a per-bot suffixed key can override the shared generic one. The LLM provider
# key (OPENAI_API_KEY or ANTHROPIC_API_KEY) is required conditionally on
# LLM_PROVIDER — see load_config.
REQUIRED_KEYS = (
    "TELEGRAM_CHAT_ID",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_TOKEN_URI",
    "GMAIL_SCOPES",
    # Owner signature identity, interpolated into the draft system prompt and
    # used as the self-test address by the live smoke run. Kept in env so no
    # personal contact detail is ever hardcoded in the source tree.
    "GMAIL_SELF_ADDRESS",
    "GMAIL_OWNER_NAME",
    "GMAIL_OWNER_PHONE",
)

# Draft-generation backend. "openai" is the default (the Anthropic key ran out
# of credits, 2026-07-14); "anthropic" is kept for a future switch-back.
DEFAULT_LLM_PROVIDER = "openai"
VALID_PROVIDERS = ("openai", "anthropic")


class ConfigError(RuntimeError):
    """Raised when a required configuration key is missing or empty."""


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: int
    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    gmail_token_uri: str
    gmail_scopes: list[str]
    # Owner signature identity (env-only; never hardcoded). Defaults are empty so
    # auth/live-run tests can build a Config without them; load_config requires
    # them so a real deploy always renders a complete signature.
    self_address: str = ""
    owner_name: str = ""
    owner_phone: str = ""
    # Draft-generation provider selection. The key for the active provider is
    # required; the other is optional (kept for switch-back).
    llm_provider: str = DEFAULT_LLM_PROVIDER
    openai_api_key: str | None = None
    # Optional override (GMAIL_DRAFT_MODEL); None -> the drafts module default.
    draft_model: str | None = None
    anthropic_api_key: str | None = None
    # Optional short-lived bearer token (OAuth Playground). When set, GmailClient
    # builds creds from it directly and GMAIL_REFRESH_TOKEN may be absent.
    gmail_access_token: str | None = None


def _require(values: dict[str, str | None], key: str) -> str:
    """Return the value for ``key`` or raise ConfigError naming the missing key."""
    value = values.get(key)
    if value is None or value.strip() == "":
        raise ConfigError(f"Missing required config key: {key}")
    return value.strip()


def _require_telegram_token(values: dict[str, str | None]) -> str:
    """Resolve the Telegram bot token, preferring the per-bot suffixed key.

    The shared master env's generic ``TELEGRAM_BOT_TOKEN`` belongs to another
    bot on the VPS, so ``TELEGRAM_BOT_TOKEN_GMAIL`` wins when present. The
    generic key is the fallback for local dev, where only it is set.
    """
    token = _optional(values, "TELEGRAM_BOT_TOKEN_GMAIL") or _optional(
        values, "TELEGRAM_BOT_TOKEN"
    )
    if token is None:
        raise ConfigError(
            "Missing required config key: TELEGRAM_BOT_TOKEN_GMAIL "
            "(or generic TELEGRAM_BOT_TOKEN as a fallback)"
        )
    return token


def _optional(values: dict[str, str | None], key: str) -> str | None:
    """Return the stripped value for ``key`` or ``None`` if absent/empty."""
    value = values.get(key)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def load_config(env_path: Path = MASTER_ENV_PATH) -> Config:
    """Load and validate configuration from the master env file.

    Does not mutate ``os.environ`` (uses ``dotenv_values``), so loading the
    config for a ``--check`` never leaks secrets into the process environment.

    Gmail auth is satisfied by EITHER a GMAIL_REFRESH_TOKEN (production) OR a
    short-lived GMAIL_ACCESS_TOKEN (live-proof run). At least one must be present.

    Raises:
        ConfigError: if any required key is absent/empty, or if neither a Gmail
            refresh token nor a Gmail access token is provided.
    """
    values = dict(dotenv_values(env_path))
    scopes_raw = _require(values, "GMAIL_SCOPES")
    access_token = _optional(values, "GMAIL_ACCESS_TOKEN")
    refresh_token = _optional(values, "GMAIL_REFRESH_TOKEN")
    if refresh_token is None and access_token is None:
        raise ConfigError(
            "Missing required config key: GMAIL_REFRESH_TOKEN "
            "(or GMAIL_ACCESS_TOKEN for a short-lived live run)"
        )
    provider = _resolve_provider(values)
    openai_key = _optional(values, "OPENAI_API_KEY")
    anthropic_key = _optional(values, "ANTHROPIC_API_KEY")
    if provider == "openai" and openai_key is None:
        raise ConfigError("Missing required config key: OPENAI_API_KEY (LLM_PROVIDER=openai)")
    if provider == "anthropic" and anthropic_key is None:
        raise ConfigError("Missing required config key: ANTHROPIC_API_KEY (LLM_PROVIDER=anthropic)")
    return Config(
        telegram_bot_token=_require_telegram_token(values),
        telegram_chat_id=int(_require(values, "TELEGRAM_CHAT_ID")),
        gmail_client_id=_require(values, "GMAIL_CLIENT_ID"),
        gmail_client_secret=_require(values, "GMAIL_CLIENT_SECRET"),
        gmail_refresh_token=refresh_token or "",
        gmail_token_uri=_require(values, "GMAIL_TOKEN_URI"),
        gmail_scopes=scopes_raw.split(),
        self_address=_require(values, "GMAIL_SELF_ADDRESS"),
        owner_name=_require(values, "GMAIL_OWNER_NAME"),
        owner_phone=_require(values, "GMAIL_OWNER_PHONE"),
        llm_provider=provider,
        openai_api_key=openai_key,
        draft_model=_optional(values, "GMAIL_DRAFT_MODEL"),
        anthropic_api_key=anthropic_key,
        gmail_access_token=access_token,
    )


def _resolve_provider(values: dict[str, str | None]) -> str:
    """Return the validated LLM provider, defaulting to ``openai``."""
    provider = (_optional(values, "LLM_PROVIDER") or DEFAULT_LLM_PROVIDER).lower()
    if provider not in VALID_PROVIDERS:
        raise ConfigError(
            f"Invalid LLM_PROVIDER: {provider!r} (expected one of {VALID_PROVIDERS})"
        )
    return provider
