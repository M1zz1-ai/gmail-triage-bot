"""Dedup TTL/cap + draft store cap."""

from pathlib import Path

import gmail_bot.state as state_mod
from gmail_bot.state import (
    DRAFTS_CAP,
    PROCESSED_CAP,
    PROCESSED_TTL_MS,
    Draft,
    State,
)


def _state(tmp_path: Path) -> State:
    return State(tmp_path / "state.db")


def test_dedup_same_id_not_re_notified(tmp_path):
    s = _state(tmp_path)
    assert not s.is_processed("m1")
    s.mark_processed("m1")
    assert s.is_processed("m1")
    s.close()


def test_ttl_prune_removes_old_entries(tmp_path):
    s = _state(tmp_path)
    now = 1_000_000_000_000
    s.mark_processed("old", ts=now - PROCESSED_TTL_MS - 1)
    s.mark_processed("fresh", ts=now - 1000)
    deleted = s.prune_processed(now_ms=now)
    assert deleted == 1
    assert not s.is_processed("old")
    assert s.is_processed("fresh")
    s.close()


def test_processed_cap_enforced(tmp_path):
    s = _state(tmp_path)
    for i in range(PROCESSED_CAP + 25):
        s.mark_processed(f"m{i}", ts=i)
    assert s.processed_count() == PROCESSED_CAP
    # Oldest (lowest ts) dropped; newest kept.
    assert not s.is_processed("m0")
    assert s.is_processed(f"m{PROCESSED_CAP + 24}")
    s.close()


def _draft(ts: int) -> Draft:
    return Draft(
        text="body", thread_id="t", last_msg_id="lm",
        chat_id=1, tg_message_id=2, created_at=ts,
    )


def test_draft_save_and_get_roundtrip(tmp_path):
    s = _state(tmp_path)
    s.save_draft("m1", _draft(100))
    got = s.get_draft("m1")
    assert got is not None
    assert got.text == "body"
    assert got.thread_id == "t"
    assert got.last_msg_id == "lm"
    assert s.get_draft("missing") is None
    s.close()


def test_drafts_cap_drops_oldest(tmp_path):
    s = _state(tmp_path)
    for i in range(DRAFTS_CAP + 10):
        s.save_draft(f"m{i}", _draft(ts=i))
    assert s.drafts_count() == DRAFTS_CAP
    assert s.get_draft("m0") is None  # oldest dropped
    assert s.get_draft(f"m{DRAFTS_CAP + 9}") is not None  # newest kept
    s.close()


def test_now_ms_is_int():
    assert isinstance(state_mod._now_ms(), int)
