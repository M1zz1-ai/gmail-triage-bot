"""Entrypoint: single asyncio process running the poll loop + Telegram polling.

Resilience is the point of this rewrite: a failed Gmail/Telegram/Anthropic call
in one poll cycle is logged and retried next cycle — the process never dies. On
Gmail OAuth invalid_grant/RefreshError, the owner is pinged and the loop keeps
running (no crash-loop).

Run modes:
  python -m gmail_bot           # run the bot (long-polling)
  python -m gmail_bot --check   # validate config loading, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery

from . import drafts
from .config import Config, ConfigError, load_config
from .gmail import GmailClient, RefreshError
from .state import State
from .telegram_bot import (
    Handlers,
    message_fields,
    parse_callback,
    send_new_email_card,
)

logger = logging.getLogger("gmail_bot")

POLL_INTERVAL_SECONDS = 120
REAUTH_MESSAGE = "⚠️ Gmail re-auth needed — run `python -m gmail_bot.auth`"


async def poll_once(gmail: GmailClient, bot: Bot, state: State, chat_id: int) -> None:
    """One poll cycle: prune, list unread, dedup, notify per new message."""
    state.prune_processed()
    stubs = await asyncio.to_thread(gmail.list_unread)
    for stub in stubs:
        msg_id = stub.get("id")
        if not msg_id or state.is_processed(msg_id):
            continue
        try:
            full = await asyncio.to_thread(gmail.get_message, msg_id)
            fields = message_fields(full)
            await send_new_email_card(bot, chat_id, fields)
            state.mark_processed(msg_id)
        except RefreshError:
            raise  # bubble up so the loop can notify + stay alive
        except Exception:
            logger.exception("failed to notify for message %s; will retry next cycle", msg_id)


async def poll_loop(gmail: GmailClient, bot: Bot, state: State, chat_id: int) -> None:
    """Forever: poll every 2 min, swallowing errors so the process never dies."""
    while True:
        try:
            await poll_once(gmail, bot, state, chat_id)
        except RefreshError:
            logger.error("Gmail RefreshError (invalid_grant) — notifying owner, staying alive")
            try:
                await bot.send_message(chat_id=chat_id, text=REAUTH_MESSAGE)
            except Exception:
                logger.exception("failed to send re-auth notice")
        except Exception:
            logger.exception("poll cycle failed; will retry next cycle")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def build_dispatcher(handlers: Handlers) -> Dispatcher:
    """Wire the aiogram dispatcher to answer callbacks and route them."""
    dp = Dispatcher()

    @dp.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        # Answer immediately to stop the Telegram spinner (spec 3.1).
        try:
            await query.answer()
        except Exception:
            logger.exception("failed to answer callback query")
        cb = parse_callback(query.data or "")
        if cb is None:
            return
        message = query.message
        if message is None:
            return
        try:
            await handlers.route(cb, message.message_id)
        except RefreshError:
            await handlers._bot.send_message(chat_id=handlers._chat_id, text=REAUTH_MESSAGE)
        except Exception:
            logger.exception("callback handler failed for %s", cb.raw)

    return dp


async def run(config: Config) -> None:
    """Build everything and run the poll loop + dispatcher concurrently."""
    bot = Bot(token=config.telegram_bot_token)
    gmail = GmailClient(config)
    draft_builder = drafts.build_draft_builder(config)
    state = State()
    handlers = Handlers(bot, gmail, draft_builder, state, config.telegram_chat_id)
    dp = build_dispatcher(handlers)

    logger.info("gmail-bot started; polling every %ss", POLL_INTERVAL_SECONDS)
    poller = asyncio.create_task(poll_loop(gmail, bot, state, config.telegram_chat_id))
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        poller.cancel()
        state.close()
        await bot.session.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="gmail_bot")
    parser.add_argument(
        "--check", action="store_true", help="Validate config loading and exit."
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        auth_mode = "access-token" if config.gmail_access_token else "refresh-token"
        print(f"Config OK — all required keys present (Gmail auth: {auth_mode} mode).")
        return 0

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
