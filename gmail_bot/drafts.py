"""Reply drafting behind a provider seam (OpenAI default, Anthropic switch-back).

System prompt and regen-extra are VERBATIM from the n8n gmail-bot-v3 spec and are
provider-agnostic. ``build_draft_builder`` selects the backend from config
(``LLM_PROVIDER``). Both builders expose the same ``generate`` contract and return
the same draft-text output, so downstream TG cards / send flow are untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from gmail_bot.config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnerIdentity:
    """Signature identity injected into the system prompt (sourced from config).

    Kept out of the source tree so no personal contact detail is hardcoded.
    """

    name: str
    email: str
    phone: str

MODEL = "claude-sonnet-4-6"
# Verified via client.models.list() (2026-07-14). gpt-4.1-mini: stronger
# instruction-following than gpt-4o-mini, still cheap for short templated email
# drafts. Override via GMAIL_DRAFT_MODEL (e.g. gpt-4o or gpt-5-mini for more).
DEFAULT_DRAFT_MODEL = "gpt-4.1-mini"
MAX_TOKENS = 1024

# VERBATIM from spec section 7 (identity fields parameterized) — used by BOTH
# Draft Reply and Regen Draft. Only ``{name}``/``{email}``/``{phone}`` are
# substituted; the rest of the wording is unchanged.
SYSTEM_PROMPT_TEMPLATE = """You are the owner's email assistant drafting professional replies on their behalf.

Format every reply EXACTLY like this:

Good afternoon,

<body — formal, direct, no filler phrases, no corporate cliches like "I hope this email finds you well">

Kind Regards,
{name}
E| {email}
P|{phone}

Rules:
- Always open with "Good afternoon," — never "Hi", "Hello", "Dear X"
- Always close with the exact signature block above — never modify the contact details
- Body in formal English (the owner handles English business correspondence this way)
- If the incoming email is in Russian and clearly from a personal contact (not business), reply in Russian with a less formal body but keep the same opening "Good afternoon," / closing block
- Output ONLY the reply text — no subject line, no preamble, no markdown, no quoted original"""

# VERBATIM regen-only additional system text (appended after the block above).
REGEN_EXTRA = """

The previous draft was rejected by the owner. Write a meaningfully different version — change angle, structure, or emphasis. Keep the opening "Good afternoon," and the signature block exactly as specified."""


def build_system_prompt(identity: OwnerIdentity, regen: bool = False) -> str:
    """Return the system prompt with the owner signature filled in.

    For regen, the extra instruction is appended.
    """
    base = SYSTEM_PROMPT_TEMPLATE.format(
        name=identity.name, email=identity.email, phone=identity.phone
    )
    return base + REGEN_EXTRA if regen else base


def build_user_prompt(thread_context: str, prev_draft: str | None = None) -> str:
    """Build the user/prompt text for the draft request.

    Matches the spec wording for both the initial draft (4.3) and regen (5.4).
    """
    if prev_draft is None:
        return (
            "Email thread (most recent message last):\n\n"
            f"{thread_context}\n\n"
            "Draft a reply to the most recent message in this thread."
        )
    return (
        "Email thread (most recent message last):\n\n"
        f"{thread_context}\n\n"
        "Previous draft (rejected by the owner):\n"
        f"{prev_draft}\n\n"
        "Write a new, meaningfully different reply draft to the most recent message."
    )


class DraftBuilderProtocol(Protocol):
    """The draft-generation contract downstream code depends on."""

    def generate(self, thread_context: str, prev_draft: str | None = None) -> str: ...


class DraftBuilder:
    """Wraps the Anthropic client to produce reply drafts."""

    def __init__(self, client: object, identity: OwnerIdentity) -> None:
        self._client = client
        self._identity = identity

    def generate(self, thread_context: str, prev_draft: str | None = None) -> str:
        """Generate a draft reply from thread context.

        Note: no prompt caching — the system prompt is ~250 tokens, below the
        1024-token minimum cacheable prefix, so cache_control would silently
        no-op. Token usage is logged for cost visibility.

        Args:
            thread_context: Rendered thread (last 5 msgs, 800-char slices).
            prev_draft: If set, this is a regeneration ("make it different").
        """
        regen = prev_draft is not None
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system_prompt(self._identity, regen=regen),
            messages=[
                {
                    "role": "user",
                    "content": build_user_prompt(thread_context, prev_draft=prev_draft),
                }
            ],
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "draft usage: input=%s output=%s regen=%s",
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
                regen,
            )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return text.strip()


class OpenAIDraftBuilder:
    """Wraps the OpenAI client to produce reply drafts (same output contract)."""

    def __init__(
        self, client: object, identity: OwnerIdentity, model: str = DEFAULT_DRAFT_MODEL
    ) -> None:
        self._client = client
        self._identity = identity
        self._model = model

    def generate(self, thread_context: str, prev_draft: str | None = None) -> str:
        """Generate a draft reply from thread context via OpenAI chat completions.

        Returns the same stripped draft text as the Anthropic path. Token usage
        is logged for cost visibility.

        Args:
            thread_context: Rendered thread (last 5 msgs, 800-char slices).
            prev_draft: If set, this is a regeneration ("make it different").
        """
        regen = prev_draft is not None
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": build_system_prompt(self._identity, regen=regen)},
                {
                    "role": "user",
                    "content": build_user_prompt(thread_context, prev_draft=prev_draft),
                },
            ],
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "draft usage: input=%s output=%s regen=%s model=%s",
                getattr(usage, "prompt_tokens", "?"),
                getattr(usage, "completion_tokens", "?"),
                regen,
                self._model,
            )
        text = response.choices[0].message.content or ""
        return text.strip()


def build_draft_builder(config: Config) -> DraftBuilderProtocol:
    """Construct the draft builder for the configured provider.

    Defaults to OpenAI (``LLM_PROVIDER=openai``); ``anthropic`` is kept for a
    future switch-back. Client SDKs are imported lazily so only the active
    provider's dependency is touched at runtime.
    """
    identity = OwnerIdentity(
        name=config.owner_name, email=config.self_address, phone=config.owner_phone
    )
    if config.llm_provider == "openai":
        from openai import OpenAI

        client = OpenAI(api_key=config.openai_api_key)
        return OpenAIDraftBuilder(
            client, identity, model=config.draft_model or DEFAULT_DRAFT_MODEL
        )

    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    return DraftBuilder(client, identity)
