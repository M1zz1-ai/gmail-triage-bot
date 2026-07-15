"""Live end-to-end smoke test for gmail-bot-py (thin CLI wrapper).

All logic lives in ``gmail_bot.live_smoke`` so this CLI and the OAuth
bootstrap (``gmail_bot.auth``) share one implementation. Drives the bot's OWN
modules against real credentials in ~/.config/gmail-triage-bot/.env; the only email it
creates/modifies/trashes is a self-test email sent FROM the account TO itself.
Telegram output goes only to the owner chat and is clearly labelled as a test.
It never runs the poll loop.

Run:
    uv run python scripts/live_smoke.py

Exit code = number of failed steps (0 == all PASS).
"""

from __future__ import annotations

from gmail_bot.live_smoke import run_live_smoke


def main() -> int:
    return run_live_smoke().failures


if __name__ == "__main__":
    raise SystemExit(main())
