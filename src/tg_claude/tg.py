"""Telegram: отправка с ретраями и фолбэком на plain text + реализация Messenger."""

from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import GetUpdates
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message, ReactionTypeEmoji

from .messenger import Buttons, Conv
from .render import render

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


class PollingFloodWait(BaseRequestMiddleware):
    """aiogram на flood control в getUpdates не ждёт retry_after, а повторяет через 1–5с —
    каждый такой повтор продлевает бан. Ждём столько, сколько просит Telegram."""

    async def __call__(self, make_request, bot, method):
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        while True:
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                log.warning("Telegram просит подождать %sс перед getUpdates — жду", e.retry_after)
                await asyncio.sleep(e.retry_after + 0.5)


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


def keyboard(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row] for row in buttons])


class TelegramMessenger:
    platform = "tg"
    name = "Telegram"
    cmd_prefix = "/"
    supports_rename = True
    supports_delete_thread = True

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, conv: Conv, text: str, buttons: Buttons | None = None) -> int | None:
        msg = await send(self.bot, conv.chat, conv.thread, text, keyboard(buttons))
        return msg.message_id if msg else None

    async def send_markdown(self, conv: Conv, text: str) -> None:
        for chunk in render(text):
            await self.send(conv, chunk)

    async def edit(self, conv: Conv, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        await edit(self.bot, conv.chat, message_id, text, keyboard(buttons))

    async def react(self, conv: Conv, message_id: int, emoji: str) -> None:
        await react(self.bot, conv.chat, message_id, emoji)

    async def typing(self, conv: Conv) -> None:
        await typing(self.bot, conv.chat, conv.thread)

    async def rename_thread(self, conv: Conv, name: str) -> bool:
        if not conv.thread:
            return False
        try:
            await self.bot.edit_forum_topic(conv.chat, conv.thread, name=name[:128])
            return True
        except Exception as e:
            log.warning("не удалось переименовать тред %s: %s", conv.key, e)
            return False

    async def new_thread(self, conv: Conv, title: str) -> Conv:
        try:
            topic = await self.bot.create_forum_topic(conv.chat, title)
        except Exception as e:
            raise RuntimeError(f"{e}\n\nВключи Threaded Mode боту в @BotFather или создай тред вручную.") from e
        return Conv("tg", conv.chat, topic.message_thread_id)

    async def delete_thread(self, conv: Conv) -> None:
        await self.bot.delete_forum_topic(conv.chat, conv.thread)
