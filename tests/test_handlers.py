"""Callback routing: each button -> correct action with right ids.

Also covers: empty-body guard never calls Claude, HTML escaping of card fields,
and that send targets the original card from the stored draft.
"""

import base64
from types import SimpleNamespace

from googleapiclient.errors import HttpError

from gmail_bot.state import Draft, State, _now_ms
from gmail_bot.telegram_bot import Handlers, new_email_keyboard, parse_callback


def _http_error(status):
    return HttpError(resp=SimpleNamespace(status=status, reason="Not Found"), content=b'{}')


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8")


class FakeBot:
    def __init__(self):
        self.edits = []
        self.sent = []

    async def edit_message_text(self, chat_id, message_id, text, parse_mode):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def send_message(self, chat_id, text, parse_mode=None,
                           disable_web_page_preview=None, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})


class FakeGmail:
    def __init__(self, thread=None):
        self.removed = []
        self.trashed = []
        self.sent_replies = []
        self._thread = thread or {}

    def remove_unread(self, msg_id):
        self.removed.append(msg_id)

    def trash(self, msg_id):
        self.trashed.append(msg_id)

    def get_thread(self, thread_id):
        return self._thread

    def send_reply(self, thread_id, last_msg_id, body):
        self.sent_replies.append((thread_id, last_msg_id, body))


class FakeDraftBuilder:
    def __init__(self):
        self.calls = []

    def generate(self, thread_context, prev_draft=None):
        self.calls.append((thread_context, prev_draft))
        return "Generated draft body"


def _thread_with_body(body="Real email body", subject="Subj"):
    return {
        "messages": [{
            "id": "LASTMSG",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "sender@x.com"},
                    {"name": "Subject", "value": subject},
                ],
                "body": {"data": _b64(body)},
            },
        }]
    }


def _handlers(tmp_path, gmail, drafts_builder=None):
    bot = FakeBot()
    state = State(tmp_path / "h.db")
    h = Handlers(bot, gmail, drafts_builder or FakeDraftBuilder(), state, chat_id=123456789)
    return h, bot, state


class ResolvingGmail(FakeGmail):
    """Fake that 404s get_thread for unknown ids and can resolve a msg->thread map."""

    def __init__(self, thread=None, threads_by_id=None, messages=None):
        super().__init__(thread)
        # id -> thread dict
        self._threads_by_id = threads_by_id or {}
        # msg_id -> {"threadId": ...}
        self._messages = messages or {}
        self.thread_gets = []
        self.message_gets = []

    def get_thread(self, thread_id):
        self.thread_gets.append(thread_id)
        if thread_id in self._threads_by_id:
            return self._threads_by_id[thread_id]
        raise _http_error(404)

    def get_message(self, msg_id):
        self.message_gets.append(msg_id)
        if msg_id in self._messages:
            return self._messages[msg_id]
        raise _http_error(404)


def test_new_email_keyboard_reply_carries_thread_id():
    kb = new_email_keyboard(msg_id="MSG1", thread_id="THREAD1")
    buttons = {b.text: b for row in kb.inline_keyboard for b in row}
    # Reply/Delete/Read semantics: message ops carry msg_id, reply carries thread_id.
    assert buttons["✍️ Reply"].callback_data == "gmail:reply:THREAD1"
    assert buttons["📖 Read"].callback_data == "gmail:read:MSG1"
    assert buttons["🗑 Delete"].callback_data == "gmail:delete:MSG1"


async def test_reply_legacy_card_resolves_thread_on_404(tmp_path):
    # Legacy card carried a MESSAGE id; get_thread(msg) 404s, resolve real thread and retry.
    gmail = ResolvingGmail(
        threads_by_id={"REALTHREAD": _thread_with_body()},
        messages={"LEGACYMSG": {"threadId": "REALTHREAD"}},
    )
    db = FakeDraftBuilder()
    h, bot, state = _handlers(tmp_path, gmail, db)
    await h.route(parse_callback("gmail:reply:LEGACYMSG"), tg_message_id=900)
    assert len(db.calls) == 1  # draft was built
    stored = state.get_draft("LEGACYMSG")
    assert stored is not None
    assert stored.thread_id == "REALTHREAD"  # real thread, not the message id
    assert gmail.thread_gets == ["LEGACYMSG", "REALTHREAD"]  # 404 then retry
    assert "✏️ <b>Draft Reply</b>" in bot.sent[0]["text"]
    state.close()


async def test_reply_double_404_sends_error_card(tmp_path):
    # Message hard-deleted: both get_thread and the resolve retry 404 -> graceful error card.
    gmail = ResolvingGmail(messages={"GONE": {"threadId": "ALSOGONE"}})
    db = FakeDraftBuilder()
    h, bot, state = _handlers(tmp_path, gmail, db)
    await h.route(parse_callback("gmail:reply:GONE"), tg_message_id=901)
    assert db.calls == []  # never drafted
    assert bot.sent == []  # no preview
    assert "⚠️ <b>Error</b>" in bot.edits[0]["text"]
    state.close()


async def test_route_read_removes_unread_and_edits(tmp_path):
    gmail = FakeGmail()
    h, bot, state = _handlers(tmp_path, gmail)
    await h.route(parse_callback("gmail:read:M1"), tg_message_id=555)
    assert gmail.removed == ["M1"]
    assert bot.edits[0] == {"chat_id": 123456789, "message_id": 555, "text": "✅ <b>Read</b>"}
    state.close()


async def test_route_delete_trashes_and_edits(tmp_path):
    gmail = FakeGmail()
    h, bot, state = _handlers(tmp_path, gmail)
    await h.route(parse_callback("gmail:delete:M2"), tg_message_id=600)
    assert gmail.trashed == ["M2"]
    assert bot.edits[0]["text"] == "🗑 <b>Deleted</b>"
    state.close()


async def test_route_cancel_edits_only(tmp_path):
    gmail = FakeGmail()
    h, bot, state = _handlers(tmp_path, gmail)
    await h.route(parse_callback("gmail:cancel:DKEY"), tg_message_id=601)
    assert gmail.removed == [] and gmail.trashed == []
    assert bot.edits[0]["text"] == "❌ <b>Cancelled</b>"
    state.close()


async def test_route_reply_builds_draft_stores_and_sends_preview(tmp_path):
    gmail = FakeGmail(thread=_thread_with_body())
    db = FakeDraftBuilder()
    h, bot, state = _handlers(tmp_path, gmail, db)
    await h.route(parse_callback("gmail:reply:M3"), tg_message_id=700)
    # Claude was called with thread context.
    assert len(db.calls) == 1
    assert "Real email body" in db.calls[0][0]
    assert db.calls[0][1] is None  # not a regen
    # Draft stored under msg_id with the original tg_message_id.
    stored = state.get_draft("M3")
    assert stored is not None
    assert stored.tg_message_id == 700
    assert stored.last_msg_id == "LASTMSG"
    # Preview card sent.
    assert "✏️ <b>Draft Reply</b>" in bot.sent[0]["text"]
    state.close()


async def test_reply_empty_body_never_calls_claude(tmp_path):
    empty_thread = {"messages": [{
        "id": "M", "payload": {"mimeType": "text/plain", "headers": [], "body": {},
                               "snippet": ""},
    }]}
    gmail = FakeGmail(thread=empty_thread)
    db = FakeDraftBuilder()
    h, bot, state = _handlers(tmp_path, gmail, db)
    await h.route(parse_callback("gmail:reply:M4"), tg_message_id=800)
    assert db.calls == []  # Claude NEVER called on empty body
    assert "⚠️" in bot.edits[0]["text"]  # error card instead
    assert state.get_draft("M4") is None
    state.close()


async def test_regen_uses_prev_draft_and_differs(tmp_path):
    gmail = FakeGmail(thread=_thread_with_body())
    db = FakeDraftBuilder()
    h, bot, state = _handlers(tmp_path, gmail, db)
    # Seed a prior draft keyed by msg_id (regen callback: gmail:regen:<draftKey>).
    state.save_draft("M5", Draft(text="OLD DRAFT", thread_id="T5", last_msg_id="LM5",
                                 chat_id=123456789, tg_message_id=900, created_at=_now_ms()))
    await h.route(parse_callback("gmail:regen:M5"), tg_message_id=901)
    assert len(db.calls) == 1
    assert db.calls[0][1] == "OLD DRAFT"  # prev draft passed -> regen path
    # New preview sent.
    assert "✏️ <b>Draft Reply</b>" in bot.sent[0]["text"]
    state.close()


async def test_regen_missing_draft_shows_error_no_claude(tmp_path):
    gmail = FakeGmail(thread=_thread_with_body())
    db = FakeDraftBuilder()
    h, bot, state = _handlers(tmp_path, gmail, db)
    await h.route(parse_callback("gmail:regen:NOPE"), tg_message_id=902)
    assert db.calls == []
    assert "⚠️" in bot.edits[0]["text"]
    state.close()


async def test_send_uses_stored_draft_and_edits_original_card(tmp_path):
    gmail = FakeGmail()
    h, bot, state = _handlers(tmp_path, gmail)
    state.save_draft("DKEY", Draft(text="reply text", thread_id="T7", last_msg_id="LM7",
                                   chat_id=123456789, tg_message_id=1000, created_at=_now_ms()))
    # gmail:send:<threadId>:<draftKey>
    await h.route(parse_callback("gmail:send:T7:DKEY"), tg_message_id=9999)
    assert gmail.sent_replies == [("T7", "LM7", "reply text")]
    # Status edit targets the ORIGINAL card (tg_message_id=1000 from the draft),
    # NOT the callback's tg_message_id (9999).
    assert bot.edits[0]["message_id"] == 1000
    assert bot.edits[0]["text"] == "📨 <b>Reply sent</b>"
    state.close()


async def test_send_missing_draft_escapes_key_in_error(tmp_path):
    gmail = FakeGmail()
    h, bot, state = _handlers(tmp_path, gmail)
    await h.route(parse_callback("gmail:send:T:<bad&key>"), tg_message_id=1)
    text = bot.edits[0]["text"]
    assert "Draft not found" in text
    assert "&lt;bad&amp;key&gt;" in text  # key HTML-escaped
    state.close()


async def test_unknown_action_dropped(tmp_path):
    gmail = FakeGmail()
    h, bot, state = _handlers(tmp_path, gmail)
    # parse_callback yields None for malformed; route only sees valid Callbacks.
    # Simulate an out-of-set action via a hand-built Callback-like object.
    from gmail_bot.telegram_bot import Callback
    await h.route(Callback(action="bogus", msg_id="x", thread_id="x",
                           draft_msg_id="", raw="gmail:bogus:x"), tg_message_id=1)
    assert bot.edits == [] and bot.sent == []
    state.close()


async def test_reply_preview_html_escapes_draft(tmp_path):
    gmail = FakeGmail(thread=_thread_with_body())

    class EscapingBuilder:
        def generate(self, ctx, prev_draft=None):
            return "Tom & <Jerry>"

    h, bot, state = _handlers(tmp_path, gmail, EscapingBuilder())
    await h.route(parse_callback("gmail:reply:M8"), tg_message_id=42)
    assert "Tom &amp; &lt;Jerry&gt;" in bot.sent[0]["text"]
    state.close()
