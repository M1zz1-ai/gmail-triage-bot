"""Live end-to-end smoke test logic, shared by the CLI and the OAuth bootstrap.

Drives the bot's OWN modules against real credentials in ~/.config/gmail-triage-bot/.env.
Controlled and guard-railed: the only email it creates/modifies/trashes is a
self-test email sent FROM the account TO itself. Telegram output goes only to
the owner chat and is clearly labelled as a test. It never runs the poll loop.

Two entrypoints share :func:`run_live_smoke`:
  * ``scripts/live_smoke.py`` — standalone CLI.
  * ``gmail_bot.auth`` — runs it automatically right after a fresh refresh
    token is minted, so the owner's single OAuth click proves the whole bot.
"""

from __future__ import annotations

import asyncio
import base64
import html
import os
import time
from dataclasses import dataclass, field
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path

# The bot's own modules — no logic is reimplemented here.
from gmail_bot import drafts
from gmail_bot.config import Config, load_config
from gmail_bot.gmail import (
    GmailClient,
    build_thread_context,
    extract_message_fields,
    walk_body,
)
from gmail_bot.telegram_bot import (
    draft_preview_card,
    new_email_card,
    new_email_keyboard,
)

# Self-test address and owner chat id come from config (env), never hardcoded.
DEFAULT_REPORT_PATH = Path(__file__).resolve().parents[1] / "REPORT-2026-06-18.html"


@dataclass
class StepResult:
    """One smoke-test step's outcome."""

    n: int
    title: str
    ok: bool
    detail: str


@dataclass
class LiveSmokeResult:
    """Aggregate outcome of a full live smoke run."""

    steps: list[StepResult] = field(default_factory=list)

    @property
    def failures(self) -> int:
        return sum(1 for s in self.steps if not s.ok)

    def record(self, n: int, title: str, ok: bool, detail: str) -> None:
        """Print a single PASS/FAIL line and remember it for the final table."""
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] step {n} — {title}: {detail}", flush=True)
        self.steps.append(StepResult(n, title, ok, detail))


# --------------------------------------------------------------------------
# Telegram helpers (async; run via asyncio.run from the sync driver)
# --------------------------------------------------------------------------

async def _tg_get_me(token: str) -> str:
    from aiogram import Bot

    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        return me.username or str(me.id)
    finally:
        await bot.session.close()


async def _tg_send_cards(token: str, chat_id: int, fields: dict, draft_text: str) -> None:
    from aiogram import Bot

    from gmail_bot.gmail import html_escape

    bot = Bot(token=token)
    try:
        # Labelled test banner first, so the owner knows these are smoke-test cards.
        await bot.send_message(
            chat_id=chat_id,
            text="🧪 <b>gmail-bot-py LIVE E2E</b> — the next two cards are test renders.",
            parse_mode="HTML",
        )
        await bot.send_message(
            chat_id=chat_id,
            text=new_email_card(
                fields["fromHtml"], fields["subjectHtml"], fields["snippetHtml"]
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=new_email_keyboard(fields["id"], fields["threadId"]),
        )
        await bot.send_message(
            chat_id=chat_id,
            text=draft_preview_card(html_escape(draft_text)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    finally:
        await bot.session.close()


async def _tg_send_text(token: str, chat_id: int, text: str) -> None:
    from aiogram import Bot

    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    finally:
        await bot.session.close()


# --------------------------------------------------------------------------
# The guarded E2E sequence
# --------------------------------------------------------------------------

def run_live_smoke(
    config: Config | None = None,
    *,
    report_path: Path | None = DEFAULT_REPORT_PATH,
) -> LiveSmokeResult:
    """Run the full guarded live Gmail E2E and return the per-step result.

    Hard guardrails: the ONLY mail mutated is a self-test email sent from the
    account to itself; third-party mail is never touched; the continuous poll
    loop is never started.

    Args:
        config: a loaded Config; if None, ``load_config()`` is called.
        report_path: HTML report to append a dated Russian section to. Pass
            ``None`` to skip the report append entirely.

    Returns:
        LiveSmokeResult with ``.failures`` == number of failed steps.
    """
    result = LiveSmokeResult()
    ts = int(time.time())
    subject = f"🧪 gmail-bot-py live test {ts}"

    # ---- step 1: --check --------------------------------------------------
    if config is None:
        try:
            config = load_config()
            result.record(1, "config --check", True, "Config OK — all required keys present")
        except Exception as exc:  # noqa: BLE001 — top-level smoke driver, report and stop
            result.record(1, "config --check", False, f"{type(exc).__name__}: {exc}")
            print("\nConfig failed to load — cannot continue.", flush=True)
            return _finish(result, report_path, ts)
    else:
        result.record(1, "config --check", True, "Config OK — passed in by caller")

    # ---- step 2: Gmail auth ----------------------------------------------
    # Access-token mode (GMAIL_ACCESS_TOKEN set): a bare ~1h bearer token, so
    # there is nothing to refresh — its validity is proven by the read in step 3.
    # Refresh-token mode: force a refresh to prove the grant is still valid.
    refresh_valid = False
    if config.gmail_access_token:
        refresh_valid = True
        result.record(
            2,
            "Gmail token refresh",
            True,
            "access-token mode — bearer token used directly (validated by step 3 read)",
        )
    else:
        try:
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

            creds = Credentials(
                token=None,
                refresh_token=config.gmail_refresh_token,
                token_uri=config.gmail_token_uri,
                client_id=config.gmail_client_id,
                client_secret=config.gmail_client_secret,
                scopes=config.gmail_scopes,
            )
            creds.refresh(Request())
            refresh_valid = bool(creds.token)
            result.record(
                2,
                "Gmail token refresh",
                refresh_valid,
                "refresh token VALID — access token minted (no invalid_grant)",
            )
        except RefreshError as exc:
            result.record(
                2, "Gmail token refresh", False, f"INVALID — RefreshError/invalid_grant: {exc}"
            )
        except Exception as exc:  # noqa: BLE001
            result.record(2, "Gmail token refresh", False, f"{type(exc).__name__}: {exc}")

    gmail = None
    if refresh_valid:
        try:
            gmail = GmailClient(config)
        except Exception as exc:  # noqa: BLE001
            result.record(2, "Gmail client build", False, f"{type(exc).__name__}: {exc}")
            gmail = None

    # ---- step 3: read-only list unread, COUNT only -----------------------
    if gmail is not None:
        try:
            stubs = gmail.list_unread()
            result.record(3, "list unread primary (count only)", True, f"{len(stubs)} unread")
        except Exception as exc:  # noqa: BLE001
            result.record(
                3, "list unread primary (count only)", False, f"{type(exc).__name__}: {exc}"
            )
    else:
        result.record(
            3, "list unread primary (count only)", False, "skipped — no valid Gmail client"
        )

    # ---- step 4: send self-test email, fetch back, body-walk -------------
    self_msg_id = ""
    self_thread_id = ""
    if gmail is not None:
        try:
            body_text = (
                "This is an automated gmail-bot-py live E2E test message. "
                "Safe to delete. Please reply with availability for a quick sync."
            )
            mime = MIMEText(body_text, "plain", "utf-8")
            mime["To"] = config.self_address
            mime["From"] = config.self_address
            mime["Subject"] = subject
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
            sent = (
                gmail._service.users()  # bot's own service handle; no new logic
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )
            self_msg_id = sent.get("id", "")
            self_thread_id = sent.get("threadId", "")

            # Fetch the full message (raw API) so we can run the bot's body walk.
            full_raw = (
                gmail._service.users()
                .messages()
                .get(userId="me", id=self_msg_id, format="full")
                .execute()
            )
            walked = walk_body(full_raw.get("payload", {}))
            # And the simplified shape -> card fields (bot's own extractor).
            simplified = gmail.get_message(self_msg_id)
            fields = extract_message_fields(simplified)
            ok = bool(self_msg_id) and bool(walked.strip()) and bool(fields["id"])
            result.record(
                4,
                "send self-test + fetch + body walk",
                ok,
                f"sent id ok; body-walk extracted {len(walked.strip())} chars",
            )
        except Exception as exc:  # noqa: BLE001
            result.record(
                4, "send self-test + fetch + body walk", False, f"{type(exc).__name__}: {exc}"
            )
    else:
        result.record(
            4, "send self-test + fetch + body walk", False, "skipped — no valid Gmail client"
        )

    # ---- step 5: LLM draft via the bot's drafts module (configured provider) --
    draft_label = f"LLM draft [{config.llm_provider}] (signature format)"
    draft_text = ""
    if gmail is not None and self_thread_id:
        try:
            # Anthropic path: ignore any non-default ANTHROPIC_BASE_URL in the
            # process env so the standard api.anthropic.com endpoint is used.
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            builder = drafts.build_draft_builder(config)
            thread = gmail.get_thread(self_thread_id)
            ctx = build_thread_context(thread, include_subject=True)
            draft_text = builder.generate(ctx.text, None)
            has_sig = (
                draft_text.strip().startswith("Good afternoon")
                and config.owner_name in draft_text
            )
            result.record(
                5,
                draft_label,
                bool(draft_text.strip()) and has_sig,
                f"draft {len(draft_text)} chars, signature block present={has_sig}",
            )
        except Exception as exc:  # noqa: BLE001
            result.record(5, draft_label, False, f"{type(exc).__name__}: {exc}")
    else:
        result.record(5, draft_label, False, "skipped — no self-test email")

    # ---- step 6: Telegram getMe + send the two real cards ----------------
    bot_username = ""
    try:
        bot_username = asyncio.run(_tg_get_me(config.telegram_bot_token))
        result.record(6, "Telegram getMe", True, f"bot username=@{bot_username}")
    except Exception as exc:  # noqa: BLE001
        result.record(6, "Telegram getMe", False, f"{type(exc).__name__}: {exc}")

    if bot_username:
        try:
            # Build card fields for the self-test email (fallback to a stub if
            # the Gmail steps were skipped, so the card render path still runs).
            if gmail is not None and self_msg_id:
                fields = extract_message_fields(gmail.get_message(self_msg_id))
            else:
                fields = {
                    "id": "smoketest",
                    "threadId": "smoketest",
                    "fromHtml": config.self_address,
                    "subjectHtml": subject,
                    "snippetHtml": "(self-test stub — Gmail step skipped)",
                }
            preview = (
                draft_text
                or f"Good afternoon,\n\n(self-test stub)\n\nKind Regards,\n{config.owner_name}"
            )
            asyncio.run(
                _tg_send_cards(config.telegram_bot_token, config.telegram_chat_id, fields, preview)
            )
            result.record(
                6, "Telegram send cards", True, "new-email + draft-preview cards sent to owner"
            )
        except Exception as exc:  # noqa: BLE001
            result.record(6, "Telegram send cards", False, f"{type(exc).__name__}: {exc}")

    # ---- step 7: Gmail actions on the self-test email ONLY ----------------
    if gmail is not None and self_msg_id:
        try:
            gmail.remove_unread(self_msg_id)
            after_read = gmail._service.users().messages().get(
                userId="me", id=self_msg_id, format="minimal"
            ).execute()
            unread_gone = "UNREAD" not in (after_read.get("labelIds") or [])

            gmail.trash(self_msg_id)
            after_trash = gmail._service.users().messages().get(
                userId="me", id=self_msg_id, format="minimal"
            ).execute()
            in_trash = "TRASH" in (after_trash.get("labelIds") or [])

            result.record(
                7,
                "mark-read + trash self-test only",
                unread_gone and in_trash,
                f"UNREAD gone={unread_gone}, in TRASH={in_trash}",
            )
        except Exception as exc:  # noqa: BLE001
            result.record(
                7, "mark-read + trash self-test only", False, f"{type(exc).__name__}: {exc}"
            )
    else:
        result.record(7, "mark-read + trash self-test only", False, "skipped — no self-test email")

    # ---- step 8: final Telegram completion message -----------------------
    try:
        asyncio.run(
            _tg_send_text(
                config.telegram_bot_token,
                config.telegram_chat_id,
                "✅ gmail-bot-py live E2E complete",
            )
        )
        result.record(8, "Telegram completion message", True, "sent to owner")
    except Exception as exc:  # noqa: BLE001
        result.record(8, "Telegram completion message", False, f"{type(exc).__name__}: {exc}")

    return _finish(result, report_path, ts)


def _finish(result: LiveSmokeResult, report_path: Path | None, ts: int) -> LiveSmokeResult:
    print_summary(result)
    if report_path is not None:
        try:
            append_report_section(report_path, result, ts)
            print(f"\nReport updated: {report_path}", flush=True)
        except Exception as exc:  # noqa: BLE001 — report append is best-effort, never fatal
            print(f"\nReport append skipped ({type(exc).__name__}: {exc}).", flush=True)
    return result


def print_summary(result: LiveSmokeResult) -> None:
    """Print the per-step PASS/FAIL table to stdout."""
    print("\n=== LIVE E2E SUMMARY ===", flush=True)
    for s in sorted(result.steps, key=lambda r: (r.n, r.title)):
        print(f"  step {s.n}: {'PASS' if s.ok else 'FAIL'} — {s.title}", flush=True)
    print(f"\n{result.failures} failure(s).", flush=True)


# --------------------------------------------------------------------------
# HTML report append (matches existing .pill .p-ok .p-bad styling)
# --------------------------------------------------------------------------

def append_report_section(report_path: Path, result: LiveSmokeResult, ts: int) -> None:
    """Append a dated Russian "Живой Gmail-E2E — результат" section.

    Inserts before the closing ``</footer>`` so the report reflects the live
    run automatically. Reuses the report's existing ``.pill .p-ok .p-bad``
    classes and ``table`` markup. Idempotency is not required — each run
    appends a fresh dated block.
    """
    text = report_path.read_text(encoding="utf-8")
    today = date.today().isoformat()

    rows = []
    for s in sorted(result.steps, key=lambda r: (r.n, r.title)):
        pill = "p-ok" if s.ok else "p-bad"
        label = "PASS" if s.ok else "FAIL"
        rows.append(
            f'      <tr><td>{s.n}</td><td>{html.escape(s.title)}</td>'
            f'<td><span class="pill {pill}">{label}</span></td>'
            f"<td>{html.escape(s.detail)}</td></tr>"
        )
    rows_html = "\n".join(rows)
    summary_pill = "p-ok" if result.failures == 0 else "p-bad"
    summary_text = (
        "все шаги PASS" if result.failures == 0 else f"{result.failures} шаг(ов) FAIL"
    )

    section = f"""
  <h2><span class="n">7</span> Живой Gmail-E2E — результат</h2>
  <p><span class="pill {summary_pill}">{summary_text}</span> <span class="muted">\
автоматический прогон из <code>gmail_bot.auth</code> сразу после минта свежего \
refresh-токена · {today}</span></p>
  <table>
    <thead><tr><th>#</th><th>Шаг</th><th>Результат</th><th>Деталь</th></tr></thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
  <p class="sub">Гард не менялся: мутировалось только self-письмо \
(account → самому себе), чужая почта не тронута, \
бесконечный опрос не запускался.</p>
"""

    # Insert before the report's footer so the live result lands inside the
    # main content. Fall back to appending if the footer marker is absent.
    marker = "  <footer>"
    if marker in text:
        text = text.replace(marker, section + "\n" + marker, 1)
    else:
        text = text + section
    report_path.write_text(text, encoding="utf-8")


def main() -> int:
    return run_live_smoke().failures


if __name__ == "__main__":
    raise SystemExit(main())
