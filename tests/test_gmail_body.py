"""Body-walk + field extraction + thread context."""

import base64

import pytest

from gmail_bot.gmail import (
    EmptyBodyError,
    build_thread_context,
    extract_message_fields,
    html_escape,
    parse_from,
    walk_body,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8")


def test_walk_body_plain_text():
    payload = {"mimeType": "text/plain", "body": {"data": _b64("Hello plain world")}}
    assert walk_body(payload) == "Hello plain world"


def test_walk_body_html_only_falls_back_to_snippet():
    # No text/plain part; html part has no nested plain -> snippet is the guard.
    payload = {
        "mimeType": "multipart/alternative",
        "snippet": "snippet fallback",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>ignored html</p>")},
             "snippet": ""},
        ],
    }
    # html part itself: no plain, no nested parts -> returns its snippet ("").
    # Parent then has snippet "snippet fallback" but parent recursion returns the
    # first NON-empty child; child returned "" so loop continues, none non-empty,
    # parent falls back to its own snippet.
    assert walk_body(payload) == "snippet fallback"


def test_walk_body_multipart_prefers_nested_plain():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}, "snippet": ""},
            {"mimeType": "text/plain", "body": {"data": _b64("nested plain body")}},
        ],
    }
    assert walk_body(payload) == "nested plain body"


def test_walk_body_empty_returns_empty_string():
    assert walk_body({}) == ""
    assert walk_body({"mimeType": "text/plain", "body": {}}) == ""


def test_html_escape_only_three_chars():
    assert html_escape("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    assert html_escape("quotes ' \" stay") == "quotes ' \" stay"


def test_parse_from_with_display_name():
    assert parse_from('"Jane Doe" <jane@example.com>') == ("Jane Doe", "jane@example.com")
    assert parse_from("Jane Doe <jane@example.com>") == ("Jane Doe", "jane@example.com")


def test_parse_from_bare_email_uses_local_part():
    assert parse_from("<bob@example.com>") == ("bob", "bob@example.com")
    assert parse_from("bob@example.com") == ("bob", "bob@example.com")


def test_extract_message_fields_slices_and_escapes():
    long_snippet = "x" * 250
    msg = {
        "id": "m1",
        "threadId": "t1",
        "From": '"A <B>" <a&b@example.com>',
        "Subject": "Hi <there> & you",
        "snippet": long_snippet,
    }
    fields = extract_message_fields(msg)
    assert fields["id"] == "m1"
    assert fields["threadId"] == "t1"
    assert "&amp;" in fields["fromHtml"]
    assert fields["subjectHtml"] == "Hi &lt;there&gt; &amp; you"
    # 200 chars + "..." then escaped (no special chars here).
    assert fields["snippetHtml"].endswith("...")
    assert len(fields["snippetHtml"]) == 203


def test_extract_message_fields_subject_fallback():
    fields = extract_message_fields({"id": "m", "threadId": "t", "From": "x@y.com"})
    assert fields["subjectHtml"] == "(no subject)"


def test_build_thread_context_last_five_and_slice():
    msgs = []
    for i in range(7):
        msgs.append({
            "id": f"m{i}",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": f"sender{i}@x.com"},
                    {"name": "Date", "value": f"date{i}"},
                    {"name": "Subject", "value": f"subj{i}"},
                ],
                "body": {"data": _b64("y" * 1000)},
            },
        })
    ctx = build_thread_context({"messages": msgs}, include_subject=True)
    # Only last 5 messages.
    assert "m6" in ctx.last_msg_id
    assert ctx.text.count("---") == 5
    # Body sliced to 800.
    assert ("y" * 800) in ctx.text
    assert ("y" * 801) not in ctx.text
    assert "Subject: subj6" in ctx.text


def test_build_thread_context_omits_subject_for_regen():
    msgs = [{
        "id": "m1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "secret subj"}],
            "body": {"data": _b64("body")},
        },
    }]
    ctx = build_thread_context({"messages": msgs}, include_subject=False)
    assert "Subject:" not in ctx.text


def test_build_thread_context_empty_body_guard_fires():
    # All parts empty -> EmptyBodyError (so Claude is never called on empty body).
    msgs = [{
        "id": "m1",
        "payload": {"mimeType": "text/plain", "headers": [], "body": {}, "snippet": ""},
    }]
    with pytest.raises(EmptyBodyError):
        build_thread_context({"messages": msgs})

    with pytest.raises(EmptyBodyError):
        build_thread_context({"messages": []})
