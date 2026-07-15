"""Draft building: system prompt wired, regen differs, provider seam selects backend."""

from types import SimpleNamespace

import pytest

from gmail_bot import drafts
from gmail_bot.drafts import (
    DEFAULT_DRAFT_MODEL,
    REGEN_EXTRA,
    DraftBuilder,
    OpenAIDraftBuilder,
    OwnerIdentity,
    build_draft_builder,
    build_system_prompt,
    build_user_prompt,
)

# Placeholder identity — no personal data in the test tree either.
ID = OwnerIdentity(name="Test Owner", email="owner@example.com", phone="+10000000000")
SYSTEM_PROMPT = build_system_prompt(ID)


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="  Good afternoon,\n\nDraft body  ")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        )


class FakeAnthropic:
    def __init__(self):
        self.messages = FakeMessages()


def test_build_system_prompt_interpolates_identity():
    assert build_system_prompt(ID, regen=False) == SYSTEM_PROMPT
    assert "Good afternoon," in SYSTEM_PROMPT
    assert "Test Owner" in SYSTEM_PROMPT
    assert "owner@example.com" in SYSTEM_PROMPT
    assert "+10000000000" in SYSTEM_PROMPT


def test_build_system_prompt_regen_appends_extra():
    regen_prompt = build_system_prompt(ID, regen=True)
    assert regen_prompt == SYSTEM_PROMPT + REGEN_EXTRA
    assert "rejected by the owner" in regen_prompt
    assert "meaningfully different" in regen_prompt


def test_build_user_prompt_initial_vs_regen():
    initial = build_user_prompt("CTX")
    assert "Draft a reply to the most recent message" in initial
    assert "Previous draft" not in initial

    regen = build_user_prompt("CTX", prev_draft="OLD DRAFT")
    assert "Previous draft (rejected by the owner):" in regen
    assert "OLD DRAFT" in regen
    assert "meaningfully different reply draft" in regen


def test_generate_wires_system_prompt_and_model():
    fake = FakeAnthropic()
    builder = DraftBuilder(fake, ID)
    out = builder.generate("THREAD CONTEXT")
    assert out == "Good afternoon,\n\nDraft body"  # trimmed
    call = fake.messages.calls[0]
    assert call["model"] == drafts.MODEL == "claude-sonnet-4-6"
    assert call["system"] == SYSTEM_PROMPT
    assert "THREAD CONTEXT" in call["messages"][0]["content"]


def test_generate_regen_path_uses_regen_prompt_and_prev_draft():
    fake = FakeAnthropic()
    builder = DraftBuilder(fake, ID)
    builder.generate("CTX", prev_draft="PREVIOUS")
    call = fake.messages.calls[0]
    assert call["system"] == SYSTEM_PROMPT + REGEN_EXTRA
    assert "PREVIOUS" in call["messages"][0]["content"]


def test_initial_and_regen_requests_differ():
    fake = FakeAnthropic()
    builder = DraftBuilder(fake, ID)
    builder.generate("CTX")
    builder.generate("CTX", prev_draft="PREVIOUS")
    initial_call, regen_call = fake.messages.calls
    assert initial_call["system"] != regen_call["system"]
    assert initial_call["messages"][0]["content"] != regen_call["messages"][0]["content"]


# ---- OpenAI provider path (mocked SDK, no network) -----------------------


class FakeChatCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="  Good afternoon,\n\nDraft body  ")
                )
            ],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=22),
        )


class FakeOpenAI:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeChatCompletions())


def test_openai_generate_same_output_contract_and_system_prompt():
    fake = FakeOpenAI()
    builder = OpenAIDraftBuilder(fake, ID, model=DEFAULT_DRAFT_MODEL)
    out = builder.generate("THREAD CONTEXT")
    assert out == "Good afternoon,\n\nDraft body"  # same trimmed contract
    call = fake.chat.completions.calls[0]
    assert call["model"] == DEFAULT_DRAFT_MODEL == "gpt-4.1-mini"
    msgs = call["messages"]
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert "THREAD CONTEXT" in msgs[1]["content"]


def test_openai_regen_path_uses_regen_prompt_and_prev_draft():
    fake = FakeOpenAI()
    builder = OpenAIDraftBuilder(fake, ID)
    builder.generate("CTX", prev_draft="PREVIOUS")
    call = fake.chat.completions.calls[0]
    assert call["messages"][0]["content"] == SYSTEM_PROMPT + REGEN_EXTRA
    assert "PREVIOUS" in call["messages"][1]["content"]


def test_openai_model_override_respected():
    fake = FakeOpenAI()
    builder = OpenAIDraftBuilder(fake, ID, model="gpt-4o")
    builder.generate("CTX")
    assert fake.chat.completions.calls[0]["model"] == "gpt-4o"


# ---- Factory selects the provider from config ----------------------------


def _cfg(**over):
    base = dict(
        llm_provider="openai",
        openai_api_key="test-key",
        draft_model=None,
        anthropic_api_key="test-key",
        owner_name="Test Owner",
        self_address="owner@example.com",
        owner_phone="+10000000000",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_factory_openai_default():
    builder = build_draft_builder(_cfg())
    assert isinstance(builder, OpenAIDraftBuilder)
    assert builder._model == DEFAULT_DRAFT_MODEL
    assert builder._identity == ID


def test_factory_openai_model_override():
    builder = build_draft_builder(_cfg(draft_model="gpt-4o"))
    assert isinstance(builder, OpenAIDraftBuilder)
    assert builder._model == "gpt-4o"


def test_factory_anthropic_switchback():
    builder = build_draft_builder(_cfg(llm_provider="anthropic"))
    assert isinstance(builder, DraftBuilder)
    assert builder._identity == ID


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_factory_returns_builder_honoring_generate_contract(provider):
    builder = build_draft_builder(_cfg(llm_provider=provider))
    assert hasattr(builder, "generate")
