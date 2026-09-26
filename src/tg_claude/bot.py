"""Хендлеры Telegram."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup

from . import tg, tmux, usage
from .config import Config
from .questions import Interactions
from .render import esc
from .sessions import SessionManager

log = logging.getLogger(__name__)
PAGE = 8
HELP = (
    "👋 Я запускаю <b>Claude Code</b> в твоих проектах.\n\n"
    "• Каждый <b>тред</b> в этом чате — отдельная сессия Claude. Создай новый тред (или /new) "
    "и напиши задачу — я предложу выбрать проект.\n"
    "• «💬 Без проекта» — просто чат с веб-поиском, без доступа к файлам.\n\n"
    "Команды в треде:\n"
    "/status — что за сессия и как подключиться к ней с компьютера\n"
    "/esc — прервать текущий ответ\n"
    "/stop — закрыть сессию (следующее сообщение предложит выбрать проект заново)\n"
    "/project — выбрать или сменить проект в этом треде\n"
    "/limits — сколько осталось лимитов подписки Claude\n"
    "/new — создать новый тред\n\n"
    "Остальные команды со слешем (например /compact) уходят прямо в Claude."
)


async def _log_update(handler, update, data):
    """Логируем каждое входящее обновление — чтобы было видно, дошло ли сообщение до бота."""
    msg = update.message or update.edited_message
    if msg:
        kind = "edited" if update.edited_message else msg.content_type
        text = msg.text or msg.caption or ""
        log.info("входящее %s [%s:%s] от %s: %d симв. %r", kind, msg.chat.id, msg.message_thread_id or 0,
                 msg.from_user.id if msg.from_user else "?", len(text), text[:60])
        if not text:  # непонятное сообщение — пишем его целиком, чтобы разобраться
            log.info("содержимое: %s", msg.model_dump_json(exclude_none=True)[:3000])
    elif update.callback_query:
        log.info("кнопка %r от %s", update.callback_query.data, update.callback_query.from_user.id)
    else:
        log.info("обновление %s: %s", update.event_type, update.model_dump_json(exclude_none=True)[:3000])
    return await handler(update, data)


def key_of(message: Message) -> tuple[int, int, str]:
    thread = message.message_thread_id or 0
    return message.chat.id, thread, f"{message.chat.id}:{thread}"


class BotApp:
    def __init__(self, cfg: Config, bot: Bot, sessions: SessionManager, interactions: Interactions):
        self.cfg = cfg
        self.bot = bot
        self.sessions = sessions
        self.interactions = interactions
        self.pending: dict[str, list[Message]] = {}  # сообщения до выбора проекта
        self.pickers: dict[str, list[str]] = {}  # снимок списка проектов для кнопок

    def dispatcher(self) -> Dispatcher:
        dp = Dispatcher()
        dp.update.outer_middleware(_log_update)
        router = Router()
        allowed = F.from_user.id.in_(self.cfg.allowed_users)
        router.message.filter(allowed)
        router.callback_query.filter(allowed)

        router.message(CommandStart())(self.cmd_start)
        router.message(Command("help"))(self.cmd_start)
        router.message(Command("new"))(self.cmd_new)
        router.message(Command("project", "projects"))(self.cmd_project)
        router.message(Command("limits", "usage"))(self.cmd_limits)
        router.message(Command("status"))(self.cmd_status)
        router.message(Command("stop"))(self.cmd_stop)
        router.message(Command("esc"))(self.cmd_esc)
        router.message(F.text | F.photo | F.document | F.caption)(self.on_message)
        router.message(F.voice | F.video_note | F.audio)(self.on_voice)
        router.message()(self.on_unsupported)
        router.edited_message.filter(allowed)
        router.edited_message()(self.on_edited)
        router.callback_query(F.data.startswith(("pj:", "ps:", "pc")))(self.on_pick)
        router.callback_query(F.data.startswith(("p:", "q:")))(self.on_interaction)
        dp.include_router(router)
        return dp

    # ---------- команды ----------

    async def cmd_start(self, message: Message) -> None:
        chat, thread, _ = key_of(message)
        await tg.send(self.bot, chat, thread, HELP)

    async def cmd_new(self, message: Message) -> None:
        chat, thread, _ = key_of(message)
        try:
            topic = await self.bot.create_forum_topic(chat, f"Сессия {time.strftime('%d.%m %H:%M')}")
        except Exception as e:
            await tg.send(self.bot, chat, thread, f"Не смог создать тред: {esc(str(e))}\n\nВключи Threaded Mode боту в @BotFather или создай тред вручную.")
            return
        key = f"{chat}:{topic.message_thread_id}"
        await self._show_picker(chat, topic.message_thread_id, key, "Выбери проект для новой сессии:")

    async def cmd_project(self, message: Message) -> None:
        chat, thread, key = key_of(message)
        live = self.sessions.get(key)
        if live:
            title = f"Сейчас: <b>{esc(live.rec.title)}</b>. Сменить проект? Текущая сессия закроется."
            await self._show_picker(chat, thread, key, title, switch=True)
        else:
            await self._show_picker(chat, thread, key, "В каком проекте работаем?")

    async def cmd_limits(self, message: Message) -> None:
        chat, thread, _ = key_of(message)
        await tg.react(self.bot, chat, message.message_id, "👀")
        try:
            limits = await usage.fetch(self.cfg)
        except Exception as e:
            log.exception("не удалось получить лимиты")
            await tg.send(self.bot, chat, thread, f"❌ Не удалось получить лимиты: {esc(str(e))}")
            return
        await tg.send(self.bot, chat, thread, usage.format_html(limits))
        await tg.react(self.bot, chat, message.message_id, "👍")

    async def cmd_status(self, message: Message) -> None:
        chat, thread, key = key_of(message)
        live = self.sessions.get(key)
        if not live:
            await tg.send(self.bot, chat, thread, "В этом треде нет сессии. Напиши задачу — предложу выбрать проект.")
            return
        rec = live.rec
        alive = await tmux.has_session(rec.tmux)
        state = "🟢 работает" if live.busy else ("🟡 ждёт сообщения" if alive else "⚪️ остановлена (запустится при следующем сообщении)")
        await tg.send(self.bot, chat, thread, (
            f"<b>Проект:</b> {esc(rec.title)}\n<b>Папка:</b> <code>{esc(rec.cwd)}</code>\n"
            f"<b>Состояние:</b> {state}\n<b>Сессия Claude:</b> <code>{rec.session_id}</code>\n\n"
            f"Подключиться с компьютера:\n<code>{tmux.attach_cmd(rec.tmux)}</code>\n"
            f"<i>(выйти, не закрывая сессию: Ctrl+B, затем D)</i>"
        ))

    async def cmd_stop(self, message: Message) -> None:
        chat, thread, key = key_of(message)
        self.pending.pop(key, None)
        if not self.sessions.get(key):
            await tg.send(self.bot, chat, thread, "Здесь и так нет сессии.")
            return
        await self.sessions.stop(key)
        await tg.send(self.bot, chat, thread, "⏹ Сессия закрыта. Следующее сообщение предложит выбрать проект.")

    async def cmd_esc(self, message: Message) -> None:
        chat, thread, key = key_of(message)
        if self.sessions.get(key):
            await self.sessions.interrupt(key)
            await tg.send(self.bot, chat, thread, "⏸ Прервал.")

    # ---------- сообщения ----------

    async def on_unsupported(self, message: Message) -> None:
        if message.forum_topic_created or message.forum_topic_edited or message.pinned_message:
            return  # служебные сообщения тредов
        chat, thread, _ = key_of(message)
        await tg.send(self.bot, chat, thread, f"Такой тип сообщения ({esc(message.content_type)}) пока не понимаю — напиши текстом или пришли файлом.")

    async def on_edited(self, message: Message) -> None:
        chat, thread, _ = key_of(message)
        await tg.send(self.bot, chat, thread, "✏️ Правку уже отправленного сообщения Claude не увидит — отправь исправленный текст новым сообщением.")

    async def on_voice(self, message: Message) -> None:
        chat, thread, _ = key_of(message)
        await tg.send(self.bot, chat, thread, "Голосовые пока не понимаю — напиши текстом 🙏")

    async def on_message(self, message: Message) -> None:
        chat, thread, key = key_of(message)
        await tg.react(self.bot, chat, message.message_id, "👀")
        if message.text and self.interactions.take_text_answer(key, message.text):
            await tg.react(self.bot, chat, message.message_id, "👍")
            return
        if self.sessions.get(key):
            await self._forward(key, message)
            return
        if (message.text or "").startswith("/"):
            # незнакомая команда до выбора проекта — не копим её как первое сообщение для Claude
            await self._show_picker(chat, thread, key, "Сначала выбери проект:")
            return
        first = key not in self.pending
        self.pending.setdefault(key, []).append(message)
        if first:
            await self._show_picker(chat, thread, key, "В каком проекте работаем?")

    async def _forward(self, key: str, message: Message) -> None:
        live = self.sessions.get(key)
        prompt = await self._prompt_of(message, Path(live.rec.cwd) if live.rec.project is None else None)
        if not prompt:
            return
        self.sessions.add_pending_reaction(key, message.message_id)
        await self.sessions.send(key, prompt)

    async def _prompt_of(self, message: Message, chat_dir: Path | None) -> str:
        text = message.text or message.caption or ""
        file = message.document or (message.photo[-1] if message.photo else None)
        if not file:
            return text
        chat, thread, _ = key_of(message)
        folder = (chat_dir or self.cfg.state_dir / "uploads" / f"{chat}_{thread}") / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        name = getattr(file, "file_name", None) or f"photo_{message.message_id}.jpg"
        path = folder / f"{message.message_id}_{Path(name).name}"
        try:
            await self.bot.download(file, destination=path)
        except Exception as e:
            await tg.send(self.bot, chat, thread, f"Не смог скачать файл: {esc(str(e))}")
            return text
        return f"{text}\n\n[Пользователь прислал файл: {path}]".strip()

    # ---------- выбор проекта ----------

    async def _show_picker(self, chat: int, thread: int, key: str, title: str, switch: bool = False) -> None:
        projects = self.sessions.projects()
        self.pickers[key] = projects
        await tg.send(self.bot, chat, thread, f"📁 {title}", self._picker_kb(projects, 0, switch))

    def _picker_kb(self, projects: list[str], page: int, switch: bool = False) -> InlineKeyboardMarkup:
        # суффикс ":s" — кнопка из /project: разрешено закрыть текущую сессию и открыть новую
        sfx = ":s" if switch else ""
        chunk = projects[page * PAGE : (page + 1) * PAGE]
        rows, row = [], []
        for i, name in enumerate(chunk, start=page * PAGE):
            row.append(Btn(text=name[:40], callback_data=f"ps:{i}{sfx}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        nav = []
        if page > 0:
            nav.append(Btn(text="◀️", callback_data=f"pj:{page - 1}{sfx}"))
        if (page + 1) * PAGE < len(projects):
            nav.append(Btn(text="▶️", callback_data=f"pj:{page + 1}{sfx}"))
        if nav:
            rows.append(nav)
        rows.append([Btn(text="💬 Без проекта (чат + веб-поиск)", callback_data=f"pc{sfx}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def on_pick(self, cb: CallbackQuery) -> None:
        msg = cb.message
        chat, thread, key = key_of(msg)
        projects = self.pickers.get(key) or self.sessions.projects()
        data, switch = cb.data.removesuffix(":s"), cb.data.endswith(":s")
        if data.startswith("pj:"):
            await cb.answer()
            await msg.edit_reply_markup(reply_markup=self._picker_kb(projects, int(data[3:]), switch))
            return
        if self.sessions.get(key):
            if not switch:
                await cb.answer("Сессия уже запущена. Сменить проект — /project")
                return
            await self.sessions.stop(key)
        project = None if data == "pc" else projects[int(data[3:])]
        if project is not None and not (self.cfg.repos_dir / project).is_dir():
            await cb.answer("Папка пропала", show_alert=True)
            return
        await cb.answer()
        title = project or "💬 без проекта"
        await tg.edit(self.bot, chat, msg.message_id, f"🚀 Запускаю Claude: <b>{esc(title)}</b>…")
        messages = self.pending.pop(key, [])
        chat_dir = self.cfg.state_dir / "chat" / f"{chat}_{thread}" if project is None else None
        prompts = [p for p in [await self._prompt_of(m, chat_dir) for m in messages] if p]
        try:
            await self.sessions.start(
                chat, thread, project, "\n\n".join(prompts) or None, reaction_ids=[m.message_id for m in messages]
            )
        except Exception as e:
            log.exception("не удалось запустить сессию")
            await tg.edit(self.bot, chat, msg.message_id, f"❌ Не удалось запустить Claude: {esc(str(e))}")
            return
        hint = "" if prompts else "\nПиши задачу."
        await tg.edit(self.bot, chat, msg.message_id, f"✅ Сессия запущена: <b>{esc(title)}</b>{hint}\n<i>/status — подробности</i>")
        if thread:
            try:
                await self.bot.edit_forum_topic(chat, thread, name=title[:128])
            except Exception:
                pass

    async def on_interaction(self, cb: CallbackQuery) -> None:
        text = await self.interactions.on_callback(cb.data)
        await cb.answer(text[:190] if text else None)
