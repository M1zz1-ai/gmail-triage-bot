"""Gmail access: unread listing, full message/thread fetch, body walk, mutations.

Body walk and field extraction are pure functions (unit-tested). The Gmail
service is built from a refresh token; RefreshError/invalid_grant is surfaced
to the caller so the process can notify the owner and stay alive.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import Config

logger = logging.getLogger(__name__)

UNREAD_QUERY = "in:inbox category:primary is:unread"
LIST_CAP = 20
THREAD_MSG_LIMIT = 5
BODY_SLICE = 800
SNIPPET_SLICE = 500
DISPLAY_SNIPPET = 200

_FROM_RE = re.compile(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$')

# Re-export so callers can `from gmail_bot.gmail import RefreshError`.
__all__ = [
    "RefreshError",
    "EmptyBodyError",
    "GmailClient",
    "walk_body",
    "parse_from",
    "html_escape",
    "extract_message_fields",
    "build_thread_context",
]


class EmptyBodyError(RuntimeError):
    """Raised when a thread body walk yields no usable text (guard before Claude)."""


def html_escape(text: str) -> str:
    """Escape the three chars Telegram HTML parse_mode cares about: & < >."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_from(raw_from: str) -> tuple[str, str]:
    """Parse a From header into (name, email).

    Falls back to the email local-part as the name when there's no display name.
    """
    match = _FROM_RE.match(raw_from or "")
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip()
        if not name:
            name = email.split("@", 1)[0]
        return name, email
    # Bare email or unparseable — treat the whole thing as the email.
    email = (raw_from or "").strip()
    name = email.split("@", 1)[0] if "@" in email else email
    return name, email


def _header(headers: list[dict], name: str) -> str | None:
    """Case-insensitive header lookup in a Gmail payload.headers list."""
    target = name.lower()
    for h in headers or []:
        if h.get("name", "").lower() == target:
            return h.get("value")
    return None


def walk_body(payload: dict) -> str:
    """Extract the best-effort plain-text body from a Gmail message payload.

    Mirrors the n8n walk():
      1. text/plain with body.data -> base64url-decode (utf-8).
      2. else if part.parts -> recurse children, return first non-empty.
      3. else fall back to part.snippet or "".

    Prefers text/plain; html parts are only reached if they hold nested plain.
    No html->text stripping (snippet is the guard).
    """
    if not payload:
        return ""

    mime = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    data = body.get("data")

    if mime == "text/plain" and data:
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return ""

    parts = payload.get("parts")
    if parts:
        for part in parts:
            child = walk_body(part)
            if child:
                return child

    return payload.get("snippet", "") or ""


def extract_message_fields(message: dict) -> dict:
    """Parse a simplified Gmail get() into card fields (spec 2.5).

    The simplified get puts From/To/Subject at the json root. snippet sliced to
    500; displaySnippet = first 200 + "..." if longer. HTML-escaped variants.
    """
    raw_from = message.get("From", "")
    from_name, from_email = parse_from(raw_from)
    subject = message.get("Subject") or "(no subject)"
    snippet = (message.get("snippet") or "")[:SNIPPET_SLICE]
    display_snippet = snippet
    if len(snippet) > DISPLAY_SNIPPET:
        display_snippet = snippet[:DISPLAY_SNIPPET] + "..."

    return {
        "id": message.get("id", ""),
        "threadId": message.get("threadId", ""),
        "fromName": from_name,
        "fromEmail": from_email,
        "fromHtml": html_escape(from_email or from_name),
        "subjectHtml": html_escape(subject),
        "snippetHtml": html_escape(display_snippet),
    }


@dataclass
class ThreadContext:
    text: str
    last_msg_id: str


def build_thread_context(thread: dict, include_subject: bool = True) -> ThreadContext:
    """Render the last 5 thread messages into a context string (spec 4.2 / 5.3).

    Each message: From / Date / [Subject] / body(800 slice) / ---. Headers read
    case-insensitively from payload.headers with root-field fallback. Raises
    EmptyBodyError if the combined body walk produces nothing (guard).
    """
    messages = (thread.get("messages") or [])[-THREAD_MSG_LIMIT:]
    if not messages:
        raise EmptyBodyError("thread has no messages")

    blocks: list[str] = []
    any_body = False
    last_msg_id = ""
    for m in messages:
        last_msg_id = m.get("id", last_msg_id)
        payload = m.get("payload", {}) or {}
        headers = payload.get("headers", [])
        m_from = _header(headers, "From") or m.get("From", "")
        m_date = _header(headers, "Date") or m.get("Date", "")
        m_subject = _header(headers, "Subject") or m.get("Subject", "")
        body = walk_body(payload)[:BODY_SLICE]
        if body.strip():
            any_body = True

        if include_subject:
            block = f"From: {m_from}\nDate: {m_date}\nSubject: {m_subject}\n\n{body}\n---"
        else:
            block = f"From: {m_from}\nDate: {m_date}\n\n{body}\n---"
        blocks.append(block)

    if not any_body:
        raise EmptyBodyError("thread body walk produced no text")

    return ThreadContext(text="\n\n".join(blocks), last_msg_id=last_msg_id)


class GmailClient:
    """Thin Gmail wrapper. Blocking calls; wrap in asyncio.to_thread from async code."""

    def __init__(self, config: Config) -> None:
        if config.gmail_access_token:
            # Short-lived bearer token (OAuth Playground): no refresh_token /
            # client_id needed — it authorizes API calls directly for ~1h.
            creds = Credentials(token=config.gmail_access_token)
        else:
            creds = Credentials(
                token=None,
                refresh_token=config.gmail_refresh_token,
                token_uri=config.gmail_token_uri,
                client_id=config.gmail_client_id,
                client_secret=config.gmail_client_secret,
                scopes=config.gmail_scopes,
            )
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def list_unread(self) -> list[dict]:
        """List unread Primary inbox message stubs (id, threadId). Cap 20."""
        resp = (
            self._service.users()
            .messages()
            .list(userId="me", q=UNREAD_QUERY, maxResults=LIST_CAP)
            .execute()
        )
        return resp.get("messages", []) or []

    def get_message(self, msg_id: str) -> dict:
        """Fetch a full message and flatten key headers to root (simplified shape)."""
        msg = (
            self._service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
        headers = (msg.get("payload", {}) or {}).get("headers", [])
        return {
            "id": msg.get("id", ""),
            "threadId": msg.get("threadId", ""),
            "snippet": msg.get("snippet", ""),
            "internalDate": msg.get("internalDate", ""),
            "From": _header(headers, "From") or "",
            "To": _header(headers, "To") or "",
            "Subject": _header(headers, "Subject") or "",
        }

    def get_thread(self, thread_id: str) -> dict:
        """Fetch the full thread (format=full) for the body walk."""
        return (
            self._service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )

    def remove_unread(self, msg_id: str) -> None:
        self._service.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()

    def trash(self, msg_id: str) -> None:
        self._service.users().messages().trash(userId="me", id=msg_id).execute()

    def send_reply(self, thread_id: str, last_msg_id: str, body: str) -> None:
        """Send a plain-text reply in-thread, referencing the last message."""
        original = (
            self._service.users()
            .messages()
            .get(userId="me", id=last_msg_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Message-ID", "References"])
            .execute()
        )
        headers = (original.get("payload", {}) or {}).get("headers", [])
        to_addr = _header(headers, "From") or ""
        subject = _header(headers, "Subject") or ""
        message_id = _header(headers, "Message-ID") or ""
        references = _header(headers, "References") or ""
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        lines = [f"To: {to_addr}", f"Subject: {subject}"]
        if message_id:
            lines.append(f"In-Reply-To: {message_id}")
            refs = (references + " " + message_id).strip() if references else message_id
            lines.append(f"References: {refs}")
        lines.append("Content-Type: text/plain; charset=utf-8")
        lines.append("")
        lines.append(body)
        raw = base64.urlsafe_b64encode("\r\n".join(lines).encode("utf-8")).decode("utf-8")

        self._service.users().messages().send(
            userId="me", body={"raw": raw, "threadId": thread_id}
        ).execute()
