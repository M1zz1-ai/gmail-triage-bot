"""Resilience: Gmail invalid_grant/RefreshError -> notify path, no crash."""

import asyncio

import pytest
from google.auth.exceptions import RefreshError

import gmail_bot.__main__ as main_mod
from gmail_bot.__main__ import REAUTH_MESSAGE, poll_loop, poll_once
from gmail_bot.state import State


class NotifyBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text})


class RefreshErrorGmail:
    def list_unread(self):
        raise RefreshError("invalid_grant: token revoked")


class GenericErrorGmail:
    def list_unread(self):
        raise RuntimeError("transient network blip")


async def test_poll_once_bubbles_refresh_error(tmp_path):
    state = State(tmp_path / "r.db")
    with pytest.raises(RefreshError):
        await poll_once(RefreshErrorGmail(), NotifyBot(), state, chat_id=1)
    state.close()


async def test_poll_loop_notifies_on_refresh_error_and_stays_alive(tmp_path, monkeypatch):
    state = State(tmp_path / "r2.db")
    bot = NotifyBot()
    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise asyncio.CancelledError  # break the infinite loop after 2 cycles

    monkeypatch.setattr(main_mod.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await poll_loop(RefreshErrorGmail(), bot, state, chat_id=123456789)

    # Notified the owner with the exact re-auth message, and did NOT crash the loop.
    assert any(m["text"] == REAUTH_MESSAGE for m in bot.messages)
    assert any(m["chat_id"] == 123456789 for m in bot.messages)
    assert sleeps["n"] >= 1  # loop continued past the error to the sleep
    state.close()


async def test_poll_loop_swallows_generic_errors(tmp_path, monkeypatch):
    state = State(tmp_path / "r3.db")
    bot = NotifyBot()
    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        raise asyncio.CancelledError

    monkeypatch.setattr(main_mod.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await poll_loop(GenericErrorGmail(), bot, state, chat_id=1)

    # Generic error swallowed -> reached sleep -> would retry next cycle.
    assert sleeps["n"] == 1
    # No re-auth message for a generic (non-RefreshError) failure.
    assert all(m["text"] != REAUTH_MESSAGE for m in bot.messages)
    state.close()
