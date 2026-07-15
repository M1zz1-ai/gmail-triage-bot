"""Config loading: missing key -> clear ConfigError naming the key."""

import pytest

from gmail_bot.config import REQUIRED_KEYS, Config, ConfigError, load_config

FULL_ENV = """\
TELEGRAM_BOT_TOKEN=tok
TELEGRAM_CHAT_ID=123456789
GMAIL_CLIENT_ID=cid
GMAIL_CLIENT_SECRET=csecret
GMAIL_REFRESH_TOKEN=rtok
GMAIL_TOKEN_URI=https://oauth2.googleapis.com/token
GMAIL_SCOPES=https://www.googleapis.com/auth/gmail.modify
GMAIL_SELF_ADDRESS=owner@example.com
GMAIL_OWNER_NAME=Test Owner
GMAIL_OWNER_PHONE=+10000000000
OPENAI_API_KEY=test-key
ANTHROPIC_API_KEY=test-key
"""


def _write(tmp_path, content):
    p = tmp_path / ".env"
    p.write_text(content)
    return p


def test_load_config_full(tmp_path):
    cfg = load_config(_write(tmp_path, FULL_ENV))
    assert isinstance(cfg, Config)
    assert cfg.telegram_chat_id == 123456789
    assert cfg.gmail_scopes == ["https://www.googleapis.com/auth/gmail.modify"]
    assert cfg.self_address == "owner@example.com"
    assert cfg.owner_name == "Test Owner"
    assert cfg.owner_phone == "+10000000000"
    assert cfg.anthropic_api_key == "test-key"
    assert cfg.llm_provider == "openai"  # default
    assert cfg.openai_api_key == "test-key"
    assert cfg.draft_model is None  # no override -> drafts default


def test_provider_defaults_to_openai_requiring_openai_key(tmp_path):
    lines = [ln for ln in FULL_ENV.splitlines() if not ln.startswith("OPENAI_API_KEY=")]
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, "\n".join(lines) + "\n"))
    assert "OPENAI_API_KEY" in str(exc.value)


def test_anthropic_provider_requires_anthropic_key(tmp_path):
    env = FULL_ENV + "LLM_PROVIDER=anthropic\n"
    lines = [ln for ln in env.splitlines() if not ln.startswith("ANTHROPIC_API_KEY=")]
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, "\n".join(lines) + "\n"))
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_anthropic_provider_selected(tmp_path):
    cfg = load_config(_write(tmp_path, FULL_ENV + "LLM_PROVIDER=anthropic\n"))
    assert cfg.llm_provider == "anthropic"
    assert cfg.anthropic_api_key == "test-key"


def test_invalid_provider_raises(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, FULL_ENV + "LLM_PROVIDER=gemini\n"))
    assert "gemini" in str(exc.value)


def test_draft_model_override_read(tmp_path):
    cfg = load_config(_write(tmp_path, FULL_ENV + "GMAIL_DRAFT_MODEL=gpt-4o\n"))
    assert cfg.draft_model == "gpt-4o"


def test_openai_provider_does_not_require_anthropic_key(tmp_path):
    lines = [ln for ln in FULL_ENV.splitlines() if not ln.startswith("ANTHROPIC_API_KEY=")]
    cfg = load_config(_write(tmp_path, "\n".join(lines) + "\n"))
    assert cfg.llm_provider == "openai"
    assert cfg.anthropic_api_key is None


@pytest.mark.parametrize("missing", REQUIRED_KEYS)
def test_missing_required_key_raises_named_error(tmp_path, missing):
    lines = [ln for ln in FULL_ENV.splitlines() if not ln.startswith(f"{missing}=")]
    p = _write(tmp_path, "\n".join(lines) + "\n")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert missing in str(exc.value)


def test_missing_refresh_token_alone_is_fatal(tmp_path):
    # No access token present, so a missing refresh token must fail loud.
    lines = [ln for ln in FULL_ENV.splitlines() if not ln.startswith("GMAIL_REFRESH_TOKEN=")]
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, "\n".join(lines) + "\n"))
    assert "GMAIL_REFRESH_TOKEN" in str(exc.value)


def test_access_token_satisfies_gmail_auth_without_refresh_token(tmp_path):
    # Drop the refresh token, add a short-lived access token instead.
    lines = [ln for ln in FULL_ENV.splitlines() if not ln.startswith("GMAIL_REFRESH_TOKEN=")]
    env = "\n".join(lines) + "\nGMAIL_ACCESS_TOKEN=test-access-token\n"
    cfg = load_config(_write(tmp_path, env))
    assert cfg.gmail_access_token == "test-access-token"
    assert cfg.gmail_refresh_token == ""


def test_refresh_token_only_leaves_access_token_none(tmp_path):
    cfg = load_config(_write(tmp_path, FULL_ENV))
    assert cfg.gmail_access_token is None
    assert cfg.gmail_refresh_token == "rtok"


def test_empty_value_treated_as_missing(tmp_path):
    # The active provider's key (OPENAI_API_KEY by default) empty -> fatal.
    env = FULL_ENV.replace("OPENAI_API_KEY=test-key", "OPENAI_API_KEY=")
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, env))
    assert "OPENAI_API_KEY" in str(exc.value)


def test_scopes_split_on_whitespace(tmp_path):
    env = FULL_ENV.replace(
        "GMAIL_SCOPES=https://www.googleapis.com/auth/gmail.modify",
        "GMAIL_SCOPES=scope.a scope.b scope.c",
    )
    cfg = load_config(_write(tmp_path, env))
    assert cfg.gmail_scopes == ["scope.a", "scope.b", "scope.c"]


def test_suffixed_telegram_token_wins_over_generic(tmp_path):
    # On the shared master env the generic token belongs to another bot; the
    # per-bot suffixed key must take precedence.
    env = FULL_ENV.replace(
        "TELEGRAM_BOT_TOKEN=tok",
        "TELEGRAM_BOT_TOKEN=brain-bot\nTELEGRAM_BOT_TOKEN_GMAIL=gmail-bot",
    )
    cfg = load_config(_write(tmp_path, env))
    assert cfg.telegram_bot_token == "gmail-bot"


def test_generic_telegram_token_used_when_suffixed_absent(tmp_path):
    # Local dev without the suffixed key keeps working via the generic fallback.
    cfg = load_config(_write(tmp_path, FULL_ENV))
    assert cfg.telegram_bot_token == "tok"


def test_missing_both_telegram_tokens_names_both_keys(tmp_path):
    lines = [ln for ln in FULL_ENV.splitlines() if not ln.startswith("TELEGRAM_BOT_TOKEN=")]
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, "\n".join(lines) + "\n"))
    msg = str(exc.value)
    assert "TELEGRAM_BOT_TOKEN_GMAIL" in msg
    assert "TELEGRAM_BOT_TOKEN" in msg
