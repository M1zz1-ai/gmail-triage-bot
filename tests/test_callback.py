"""Callback parsing + keyboards + card templates."""

from gmail_bot.telegram_bot import (
    Callback,
    draft_preview_card,
    draft_preview_keyboard,
    new_email_card,
    new_email_keyboard,
    parse_callback,
)


def test_parse_read():
    cb = parse_callback("gmail:read:M123")
    assert cb == Callback(action="read", msg_id="M123", thread_id="M123",
                          draft_msg_id="", raw="gmail:read:M123")


def test_parse_reply_delete():
    assert parse_callback("gmail:reply:M9").action == "reply"
    assert parse_callback("gmail:delete:M9").action == "delete"


def test_parse_send_overloads_thread_and_draft_key():
    cb = parse_callback("gmail:send:THREAD42:DRAFTKEY")
    assert cb.action == "send"
    assert cb.thread_id == "THREAD42"   # parts[2]
    assert cb.msg_id == "THREAD42"
    assert cb.draft_msg_id == "DRAFTKEY"  # parts[3]


def test_parse_regen_and_cancel_use_draft_key_as_msg_id():
    cb = parse_callback("gmail:regen:DKEY")
    assert cb.action == "regen"
    assert cb.msg_id == "DKEY"
    assert parse_callback("gmail:cancel:DKEY").action == "cancel"


def test_parse_case_insensitive_action():
    assert parse_callback("gmail:READ:M1").action == "read"


def test_parse_rejects_non_gmail_and_malformed():
    assert parse_callback("other:read:M1") is None
    assert parse_callback("gmail:read") is None      # missing parts[2]
    assert parse_callback("gmail::M1") is None        # empty action
    assert parse_callback("gmail:read:") is None      # empty arg
    assert parse_callback("") is None


def test_new_email_card_template_verbatim():
    text = new_email_card("from&amp;", "subj", "snip")
    assert text == (
        "📬 <b>New Email</b>\n\n"
        "<b>From:</b> <code>from&amp;</code>\n"
        "<b>Subject:</b> subj\n\n"
        "<i>snip</i>"
    )


def test_draft_preview_card_template_verbatim():
    text = draft_preview_card("DRAFT")
    assert text == (
        "✏️ <b>Draft Reply</b>\n\n"
        "DRAFT\n\n"
        "———\n"
        "<i>Send this reply?</i>"
    )


def test_new_email_keyboard_callback_data_and_url():
    kb = new_email_keyboard("M1", "T1")
    row1, row2 = kb.inline_keyboard
    assert row1[0].callback_data == "gmail:read:M1"
    assert row1[1].url == "https://mail.google.com/mail/u/0/#inbox/T1"
    # Reply operates on the thread (get_thread); message ops carry the message id.
    assert row2[0].callback_data == "gmail:reply:T1"
    assert row2[1].callback_data == "gmail:delete:M1"


def test_draft_preview_keyboard_callback_data():
    kb = draft_preview_keyboard("T1", "DKEY")
    row1, row2 = kb.inline_keyboard
    assert row1[0].callback_data == "gmail:send:T1:DKEY"
    assert row1[1].callback_data == "gmail:regen:DKEY"
    assert row2[0].callback_data == "gmail:cancel:DKEY"
