"""Отправка в Telegram с ретраями и фолбэком на plain text."""

from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message, ReactionTypeEmoji

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def _retry(call, *args, **kwargs):
    for _ in range(5):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
    return await call(*args, **kwargs)


async def send(
    bot: Bot, chat_id: int, thread_id: int, text: str, markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    kw = dict(message_thread_id=thread_id or None, reply_markup=markup, link_preview_options=NO_PREVIEW)
    try:
        return await _retry(bot.send_message, chat_id, text, parse_mode="HTML", **kw)
    except TelegramBadRequest as e:
        if "parse" in str(e).lower() or "entit" in str(e).lower() or "tag" in str(e).lower():
            log.warning("Telegram отверг HTML (%s), шлю текстом", e)
            try:
                return await _retry(bot.send_message, chat_id, strip_tags(text)[:4096], parse_mode=None, **kw)
            except TelegramBadRequest as e2:
                log.error("не удалось отправить сообщение: %s", e2)
                return None
        log.error("не удалось отправить сообщение: %s", e)
        return None


async def edit(
    bot: Bot, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await _retry(
            bot.edit_message_text, text=text, chat_id=chat_id, message_id=message_id,
            parse_mode="HTML", reply_markup=markup, link_preview_options=NO_PREVIEW,
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("не удалось отредактировать сообщение: %s", e)


async def react(bot: Bot, chat_id: int, message_id: int, emoji: str) -> None:
    try:
        await bot.set_message_reaction(chat_id, message_id, [ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:  # реакция — не критично
        log.debug("реакция не поставилась: %s", e)


async def typing(bot: Bot, chat_id: int, thread_id: int) -> None:
    try:
        await bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id or None)
    except Exception:
        pass
