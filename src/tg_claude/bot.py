"""Хендлеры Telegram: переводят сообщения aiogram в общий ChatCore."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from .config import Config
from .core import ALIASES, COMMANDS, ChatCore
from .messenger import Conv, FileRef, Incoming
from .rich import rich_of, rich_to_text

log = logging.getLogger(__name__)


async def _log_update(handler, update, data):
    """Логируем каждое входящее обновление — чтобы было видно, дошло ли сообщение до бота."""
    msg = update.message or update.edited_message
    if msg:
        kind = "edited" if update.edited_message else msg.content_type
        text = _text_of(msg)
        log.info("входящее %s [%s:%s] от %s: %d симв. %r", kind, msg.chat.id, msg.message_thread_id or 0,
                 msg.from_user.id if msg.from_user else "?", len(text), text[:60])
        service = msg.forum_topic_created or msg.forum_topic_edited or msg.pinned_message
        if not text and not service:  # непонятное сообщение — пишем его целиком, чтобы разобраться
            log.info("содержимое: %s", msg.model_dump_json(exclude_none=True)[:3000])
    elif update.callback_query:
        log.info("кнопка %r от %s", update.callback_query.data, update.callback_query.from_user.id)
    else:
        log.info("обновление %s: %s", update.event_type, update.model_dump_json(exclude_none=True)[:3000])
    return await handler(update, data)


def conv_of(message: Message) -> Conv:
    return Conv("tg", message.chat.id, message.message_thread_id or 0)


def _text_of(message: Message) -> str:
    rich = rich_of(message)
    return message.text or message.caption or (rich_to_text(rich) if rich else "")


class BotApp:
    def __init__(self, cfg: Config, bot: Bot, core: ChatCore):
        self.cfg = cfg
        self.bot = bot
        self.core = core

    def dispatcher(self) -> Dispatcher:
        dp = Dispatcher()
        dp.update.outer_middleware(_log_update)
        router = Router()
        allowed = F.from_user.id.in_(self.cfg.allowed_users)
        router.message.filter(allowed)
        router.callback_query.filter(allowed)

        router.message(CommandStart())(self._command("help"))
        for name in COMMANDS:
            names = [name, *(a for a, n in ALIASES.items() if n == name and a != "start")]
            router.message(Command(*names))(self._command(name))
        router.message(F.text | F.photo | F.document | F.caption | F.func(rich_of))(self.on_message)
        router.message(F.voice | F.video_note | F.audio)(self.on_message)
        router.message()(self.on_unsupported)
        router.edited_message.filter(allowed)
        router.edited_message()(self.on_edited)
        router.callback_query()(self.on_button)
        dp.include_router(router)
        return dp

    def _incoming(self, message: Message) -> Incoming:
        files = []
        file = message.document or (message.photo[-1] if message.photo else None)
        if file:
            name = getattr(file, "file_name", None) or f"photo_{message.message_id}.jpg"

            async def fetch(path: Path, file=file) -> None:
                await self.bot.download(file, destination=path)

            files.append(FileRef(name, fetch))
        mid = message.message_id
        for audio, name in (
            (message.voice, f"voice_{mid}.ogg"),
            (message.video_note, f"circle_{mid}.mp4"),
            (message.audio, message.audio and (message.audio.file_name or f"audio_{mid}.mp3")),
        ):
            if audio:
                async def fetch(path: Path, audio=audio) -> None:
                    await self.bot.download(audio, destination=path)

                files.append(FileRef(name, fetch, voice=True))
        return Incoming(conv_of(message), message.message_id, _text_of(message), files)

    def _command(self, name: str):
        async def handler(message: Message) -> None:
            await self.core.command(name, self._incoming(message))
        return handler

    async def on_message(self, message: Message) -> None:
        await self.core.on_message(self._incoming(message))

    async def on_unsupported(self, message: Message) -> None:
        if message.forum_topic_created or message.forum_topic_edited or message.pinned_message:
            return  # служебные сообщения тредов
        await self.core.say(conv_of(message), f"Такой тип сообщения ({message.content_type}) пока не понимаю — напиши текстом или пришли файлом.")

    async def on_edited(self, message: Message) -> None:
        await self.core.on_edited(conv_of(message))

    async def on_button(self, cb: CallbackQuery) -> None:
        answered = False

        async def answer(text: str | None = None, alert: bool = False) -> None:
            nonlocal answered
            if not answered:
                answered = True
                await cb.answer(text[:190] if text else None, show_alert=alert)

        await self.core.on_button(conv_of(cb.message), cb.message.message_id, cb.data or "", answer)
        await answer()
