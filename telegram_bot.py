"""Telegram layer: card templates, callback parsing, keyboards, action routing.

Card text and callback_data formats are VERBATIM from the spec (sections 2.6,
4.5, 8). Pure helpers (templates, keyboards, parse_callback) are unit-tested;
the action handlers wire Gmail + drafts + state together.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from googleapiclient.errors import HttpError

from . import drafts
from .gmail import (
    EmptyBodyError,
    GmailClient,
    build_thread_context,
    extract_message_fields,
    html_escape,
)
from .state import Draft, State, _now_ms

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"read", "delete", "reply", "send", "regen", "cancel"}


# ---- card templates (spec section 8) -----------------------------------

def new_email_card(from_html: str, subject_html: str, snippet_html: str) -> str:
    return (
        "📬 <b>New Email</b>\n\n"
        f"<b>From:</b> <code>{from_html}</code>\n"
        f"<b>Subject:</b> {subject_html}\n\n"
        f"<i>{snippet_html}</i>"
    )


def draft_preview_card(draft_text_html: str) -> str:
    return (
        "✏️ <b>Draft Reply</b>\n\n"
        f"{draft_text_html}\n\n"
        "———\n"
        "<i>Send this reply?</i>"
    )


STATUS_READ = "✅ <b>Read</b>"
STATUS_DELETED = "🗑 <b>Deleted</b>"
STATUS_REPLY_SENT = "📨 <b>Reply sent</b>"
STATUS_ALREADY_SENT = "📨 <b>Already sent</b>"
STATUS_CANCELLED = "❌ <b>Cancelled</b>"

# Cap the sent-confirmation body so a long reply doesn't produce a huge card.
SENT_PREVIEW_CAP = 600


def sent_card(sent_text_html: str) -> str:
    """Card shown on the draft-preview message after a successful send.

    Keeps the (already HTML-escaped, possibly trimmed) sent text for context and
    a clear 'sent' marker. The caller edits the message WITHOUT a keyboard, so
    the Send/Rewrite/Cancel buttons are removed and it cannot be re-sent.
    """
    return (
        "📨 <b>Reply sent</b>\n\n"
        f"{sent_text_html}\n\n"
        "———\n"
        "<i>✅ Sent</i>"
    )


def error_card(detail_html: str) -> str:
    """Not in the n8n workflow (errors routed to a global WF). Python adds one."""
    return f"⚠️ <b>Error</b>\n\n{detail_html}"


# ---- keyboards ----------------------------------------------------------

def new_email_keyboard(msg_id: str, thread_id: str) -> InlineKeyboardMarkup:
    url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📖 Read", callback_data=f"gmail:read:{msg_id}"),
                InlineKeyboardButton(text="🔗 Open", url=url),
            ],
            [
                InlineKeyboardButton(text="✍️ Reply", callback_data=f"gmail:reply:{thread_id}"),
                InlineKeyboardButton(text="🗑 Delete", callback_data=f"gmail:delete:{msg_id}"),
            ],
        ]
    )


def draft_preview_keyboard(thread_id: str, draft_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Send", callback_data=f"gmail:send:{thread_id}:{draft_key}"
                ),
                InlineKeyboardButton(
                    text="🔄 Rewrite", callback_data=f"gmail:regen:{draft_key}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Cancel", callback_data=f"gmail:cancel:{draft_key}"
                ),
            ],
        ]
    )


# ---- callback parsing (spec 3.2) ---------------------------------------

@dataclass
class Callback:
    action: str
    msg_id: str        # parts[2]
    thread_id: str     # parts[2] (overloaded: == msg_id for most actions)
    draft_msg_id: str  # parts[3] or ""
    raw: str


def parse_callback(data: str) -> Callback | None:
    """Parse 'gmail:<action>:<arg>[:extra]'. Returns None for non-gmail/malformed.

    For send, callback is gmail:send:<threadId>:<draftKey> so parts[2]=threadId
    and parts[3]=draftKey.
    """
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "gmail" or not parts[1] or not parts[2]:
        return None
    return Callback(
        action=parts[1].lower(),
        msg_id=parts[2],
        thread_id=parts[2],
        draft_msg_id=parts[3] if len(parts) > 3 else "",
        raw=data,
    )


# ---- action handlers ----------------------------------------------------

class Handlers:
    """Bundles the dependencies needed to act on callbacks.

    Each public method maps to one Switch output in the n8n workflow. Gmail
    calls run in a thread (blocking client) so the asyncio loop stays free.
    """

    def __init__(self, bot: Bot, gmail: GmailClient, draft_builder: drafts.DraftBuilder,
                 state: State, chat_id: int) -> None:
        self._bot = bot
        self._gmail = gmail
        self._drafts = draft_builder
        self._state = state
        self._chat_id = chat_id

    async def _edit(self, message_id: int, text: str) -> None:
        # reply_markup=None explicitly drops any inline keyboard on the edited
        # message, so a terminal card (sent/read/deleted/cancelled) can't be
        # re-tapped.
        await self._bot.edit_message_text(
            chat_id=self._chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=None,
        )

    async def handle_read(self, cb: Callback, tg_message_id: int) -> None:
        await asyncio.to_thread(self._gmail.remove_unread, cb.msg_id)
        await self._edit(tg_message_id, STATUS_READ)

    async def handle_delete(self, cb: Callback, tg_message_id: int) -> None:
        await asyncio.to_thread(self._gmail.trash, cb.msg_id)
        await self._edit(tg_message_id, STATUS_DELETED)

    async def handle_cancel(self, cb: Callback, tg_message_id: int) -> None:
        # Drop the pending draft so a stale Send tap afterwards can't resend it,
        # then edit the preview card in place (never a new message).
        self._state.delete_draft(cb.msg_id)
        await self._edit(tg_message_id, STATUS_CANCELLED)

    async def _resolve_thread(self, thread_id: str) -> tuple[dict, str]:
        """Fetch a thread, tolerating legacy cards that carry a message id.

        New cards pass the real threadId. Older cards (already in Telegram before
        the reply-callback fix) pass a messageId, which 404s for threads inside an
        existing Gmail thread. On 404 we resolve the message's real threadId and
        retry once. Returns (thread, real_thread_id). Raises HttpError(404) if the
        message/thread is truly gone (e.g. hard-deleted).
        """
        try:
            return await asyncio.to_thread(self._gmail.get_thread, thread_id), thread_id
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
        real_thread_id = (
            await asyncio.to_thread(self._gmail.get_message, thread_id)
        ).get("threadId", "")
        return await asyncio.to_thread(self._gmail.get_thread, real_thread_id), real_thread_id

    async def handle_reply(self, cb: Callback, tg_message_id: int) -> None:
        try:
            thread, thread_id = await self._resolve_thread(cb.thread_id)
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            logger.warning("Reply target gone (404) for %s", cb.thread_id)
            await self._edit(
                tg_message_id,
                error_card("Email no longer available — it may have been deleted."),
            )
            return
        try:
            ctx = build_thread_context(thread, include_subject=True)
        except EmptyBodyError:
            await self._edit(tg_message_id, error_card("Empty email body — cannot draft."))
            return
        draft_text = await asyncio.to_thread(self._drafts.generate, ctx.text, None)
        self._store_and_send_preview(cb.msg_id, thread_id, ctx.last_msg_id,
                                     draft_text, tg_message_id)
        await self._send_preview(cb.msg_id, thread_id, draft_text)

    async def handle_regen(self, cb: Callback, tg_message_id: int) -> None:
        # draftKey is parts[2] for regen (gmail:regen:<draftKey>) -> msg_id.
        prev = self._state.get_draft(cb.msg_id)
        if prev is None:
            await self._edit(tg_message_id, error_card("Draft not found for regenerate."))
            return
        thread = await asyncio.to_thread(self._gmail.get_thread, prev.thread_id)
        try:
            ctx = build_thread_context(thread, include_subject=False)
        except EmptyBodyError:
            await self._edit(tg_message_id, error_card("Empty email body — cannot draft."))
            return
        draft_text = await asyncio.to_thread(self._drafts.generate, ctx.text, prev.text)
        self._store_and_send_preview(cb.msg_id, prev.thread_id, ctx.last_msg_id,
                                     draft_text, tg_message_id)
        await self._send_preview(cb.msg_id, prev.thread_id, draft_text)

    async def handle_send(self, cb: Callback, tg_message_id: int) -> None:
        # gmail:send:<threadId>:<draftKey> -> draft keyed by draft_msg_id or msg_id.
        key = cb.draft_msg_id or cb.msg_id
        # Atomically claim the draft: a second (double-tap) send finds it gone and
        # degrades gracefully instead of sending the email twice.
        draft = self._state.pop_draft(key)
        if draft is None:
            await self._edit(tg_message_id, STATUS_ALREADY_SENT)
            return
        try:
            await asyncio.to_thread(
                self._gmail.send_reply, draft.thread_id, draft.last_msg_id, draft.text
            )
        except Exception:
            # Send failed — restore the draft so the (still-visible) card can retry.
            self._state.save_draft(key, draft)
            raise
        body = draft.text.strip()
        if len(body) > SENT_PREVIEW_CAP:
            body = body[:SENT_PREVIEW_CAP].rstrip() + "…"
        # Edit the card that carries the Send button (the preview card = this
        # callback's message), marking it sent and dropping the keyboard.
        await self._edit(tg_message_id, sent_card(html_escape(body)))

    def _store_and_send_preview(self, msg_id: str, thread_id: str, last_msg_id: str,
                                draft_text: str, tg_message_id: int) -> None:
        self._state.save_draft(
            msg_id,
            Draft(
                text=draft_text,
                thread_id=thread_id,
                last_msg_id=last_msg_id,
                chat_id=self._chat_id,
                tg_message_id=tg_message_id,
                created_at=_now_ms(),
            ),
        )

    async def _send_preview(self, draft_key: str, thread_id: str, draft_text: str) -> None:
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=draft_preview_card(html_escape(draft_text)),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=draft_preview_keyboard(thread_id, draft_key),
        )

    async def route(self, cb: Callback, tg_message_id: int) -> None:
        """Dispatch a parsed callback to its handler. Unknown actions are dropped."""
        if cb.action == "read":
            await self.handle_read(cb, tg_message_id)
        elif cb.action == "delete":
            await self.handle_delete(cb, tg_message_id)
        elif cb.action == "reply":
            await self.handle_reply(cb, tg_message_id)
        elif cb.action == "send":
            await self.handle_send(cb, tg_message_id)
        elif cb.action == "regen":
            await self.handle_regen(cb, tg_message_id)
        elif cb.action == "cancel":
            await self.handle_cancel(cb, tg_message_id)
        # else: silently dropped (fallbackOutput: none)


async def send_new_email_card(bot: Bot, chat_id: int, fields: dict) -> None:
    """Send the new-email notification card for one message."""
    await bot.send_message(
        chat_id=chat_id,
        text=new_email_card(fields["fromHtml"], fields["subjectHtml"], fields["snippetHtml"]),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=new_email_keyboard(fields["id"], fields["threadId"]),
    )


def message_fields(message: dict) -> dict:
    """Re-export extract_message_fields for callers building cards."""
    return extract_message_fields(message)
