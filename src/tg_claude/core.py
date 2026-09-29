"""Логика чата, общая для Telegram и Mattermost: команды, выбор проекта, пересылка в Claude."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import tmux, usage
from .config import Config
from .messenger import Buttons, Conv, Incoming, Messenger, MsgId
from .questions import Interactions
from .render import esc
from .sessions import SessionManager

log = logging.getLogger(__name__)
PAGE = 8

# ответ на нажатие кнопки: текст уведомления и «важное» (показать как предупреждение)
Answer = Callable[..., Awaitable[None]]
COMMANDS = ("help", "new", "project", "limits", "status", "stop", "esc")
ALIASES = {"start": "help", "projects": "project", "usage": "limits"}


def help_text(m: Messenger) -> str:
    p = m.cmd_prefix
    if m.platform == "tg":
        where = (
            "• Каждый <b>тред</b> в этом чате — отдельная сессия Claude. Создай новый тред (или /new) "
            "и напиши задачу — я предложу выбрать проект.\n"
        )
        new = "/new — создать новый тред\n"
    else:
        where = (
            "• Каждое новое сообщение (не в треде) — отдельная сессия Claude, дальше общаемся в его треде. "
            "В каналах позови меня упоминанием, в треде с сессией можно писать уже без него.\n"
        )
        new = ""
    return (
        "👋 Я запускаю <b>Claude Code</b> в твоих проектах.\n\n"
        f"{where}"
        "• «💬 Без проекта» — просто чат с веб-поиском, без доступа к файлам.\n\n"
        "Команды в треде:\n"
        f"{p}status — что за сессия и как подключиться к ней с компьютера\n"
        f"{p}esc — прервать текущий ответ\n"
        f"{p}stop — закрыть сессию (следующее сообщение предложит выбрать проект заново)\n"
        f"{p}project — выбрать или сменить проект в этом треде\n"
        f"{p}limits — сколько осталось лимитов подписки Claude\n"
        f"{new}\n"
        f"Остальные команды через {p} (например {p}compact) уходят прямо в Claude."
    )


class ChatCore:
    def __init__(self, cfg: Config, sessions: SessionManager, interactions: Interactions, messengers: dict[str, Messenger]):
        self.cfg = cfg
        self.sessions = sessions
        self.interactions = interactions
        self.messengers = messengers
        self.pending: dict[str, list[Incoming]] = {}  # сообщения до выбора проекта
        self.pickers: dict[str, tuple[list[str], str]] = {}  # снимок списка проектов и заголовок пикера

    def _m(self, conv: Conv) -> Messenger:
        return self.messengers[conv.platform]

    async def say(self, conv: Conv, text: str, buttons: Buttons | None = None) -> MsgId | None:
        return await self._m(conv).send(conv, text, buttons)

    def knows(self, key: str) -> bool:
        """Есть ли в этом треде сессия или начатый выбор проекта."""
        return bool(self.sessions.get(key)) or key in self.pending or key in self.pickers

    # ---------- команды ----------

    async def command(self, name: str, inc: Incoming) -> None:
        name = ALIASES.get(name, name)
        await getattr(self, f"cmd_{name}")(inc)

    async def cmd_help(self, inc: Incoming) -> None:
        await self.say(inc.conv, help_text(self._m(inc.conv)))

    async def cmd_new(self, inc: Incoming) -> None:
        m = self._m(inc.conv)
        if m.platform != "tg":
            await self.say(inc.conv, "Новая сессия — просто напиши новое сообщение вне треда.")
            return
        try:
            conv = await m.new_thread(inc.conv, f"Сессия {time.strftime('%d.%m %H:%M')}")
        except Exception as e:
            await self.say(inc.conv, f"Не смог создать тред: {esc(str(e))}")
            return
        await self._show_picker(conv, "Выбери проект для новой сессии:")

    async def cmd_project(self, inc: Incoming) -> None:
        live = self.sessions.get(inc.conv.key)
        if live:
            title = f"Сейчас: <b>{esc(live.rec.title)}</b>. Сменить проект? Текущая сессия закроется."
            await self._show_picker(inc.conv, title, switch=True)
        else:
            await self._show_picker(inc.conv, "В каком проекте работаем?")

    async def cmd_limits(self, inc: Incoming) -> None:
        m = self._m(inc.conv)
        await m.react(inc.conv, inc.message_id, "👀")
        try:
            limits = await usage.fetch(self.cfg)
        except Exception as e:
            log.exception("не удалось получить лимиты")
            await self.say(inc.conv, f"❌ Не удалось получить лимиты: {esc(str(e))}")
            return
        await self.say(inc.conv, usage.format_html(limits))
        await m.react(inc.conv, inc.message_id, "👍")

    async def cmd_status(self, inc: Incoming) -> None:
        live = self.sessions.get(inc.conv.key)
        if not live:
            await self.say(inc.conv, "В этом треде нет сессии. Напиши задачу — предложу выбрать проект.")
            return
        rec = live.rec
        alive = await tmux.has_session(rec.tmux)
        state = "🟢 работает" if live.busy else ("🟡 ждёт сообщения" if alive else "⚪️ остановлена (запустится при следующем сообщении)")
        await self.say(inc.conv, (
            f"<b>Проект:</b> {esc(rec.title)}\n<b>Папка:</b> <code>{esc(rec.cwd)}</code>\n"
            f"<b>Состояние:</b> {state}\n<b>Сессия Claude:</b> <code>{rec.session_id}</code>\n\n"
            f"Подключиться с компьютера:\n<code>{tmux.attach_cmd(rec.tmux)}</code>\n"
            f"<i>(выйти, не закрывая сессию: Ctrl+B, затем D)</i>"
        ))

    async def cmd_stop(self, inc: Incoming) -> None:
        key = inc.conv.key
        self.pending.pop(key, None)
        if not self.sessions.get(key):
            await self.say(inc.conv, "Здесь и так нет сессии.")
            return
        await self.sessions.stop(key)
        await self.say(inc.conv, "⏹ Сессия закрыта. Следующее сообщение предложит выбрать проект.")

    async def cmd_esc(self, inc: Incoming) -> None:
        if self.sessions.get(inc.conv.key):
            await self.sessions.interrupt(inc.conv.key)
            await self.say(inc.conv, "⏸ Прервал.")

    # ---------- сообщения ----------

    async def on_edited(self, conv: Conv) -> None:
        await self.say(conv, "✏️ Правку уже отправленного сообщения Claude не увидит — отправь исправленный текст новым сообщением.")

    async def on_message(self, inc: Incoming) -> None:
        conv, key = inc.conv, inc.conv.key
        m = self._m(conv)
        await m.react(conv, inc.message_id, "👀")
        if inc.text and self.interactions.take_text_answer(key, inc.text):
            await m.react(conv, inc.message_id, "👍")
            return
        live = self.sessions.get(key)
        if live:
            prompt = await self._prompt_of(inc, Path(live.rec.cwd) if live.rec.project is None else None)
            if prompt:
                self.sessions.add_pending_reaction(key, inc.message_id)
                await self.sessions.send(key, prompt)
            return
        if inc.text.startswith("/"):
            # незнакомая команда до выбора проекта — не копим её как первое сообщение для Claude
            await self._show_picker(conv, "Сначала выбери проект:")
            return
        first = key not in self.pending
        self.pending.setdefault(key, []).append(inc)
        if first:
            await self._show_picker(conv, "В каком проекте работаем?")

    async def _prompt_of(self, inc: Incoming, chat_dir: Path | None) -> str:
        text = inc.text
        if not inc.files:
            return text
        folder = (chat_dir or self.cfg.state_dir / "uploads" / inc.conv.slug) / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        notes = []
        for f in inc.files:
            path = folder / f"{inc.message_id}_{Path(f.name).name}"
            try:
                await f.fetch(path)
            except Exception as e:
                log.warning("не удалось скачать файл %s: %s", f.name, e)
                await self.say(inc.conv, f"Не смог скачать файл: {esc(str(e))}")
                continue
            notes.append(f"[Пользователь прислал файл: {path}]")
        return "\n\n".join([text, *notes]).strip()

    # ---------- кнопки ----------

    async def on_button(self, conv: Conv, message_id: MsgId, data: str, answer: Answer) -> None:
        if data.startswith(("pj:", "ps:", "pc")):
            await self._on_pick(conv, message_id, data, answer)
        elif data.startswith(("p:", "q:")):
            text, alert = await self.interactions.on_callback(data)
            await answer(text or None, alert)
        else:
            await answer()

    async def _show_picker(self, conv: Conv, title: str, switch: bool = False) -> None:
        projects = self.sessions.projects()
        text = f"📁 {title}"
        self.pickers[conv.key] = (projects, text)
        await self.say(conv, text, self._picker_kb(projects, 0, switch))

    def _picker_kb(self, projects: list[str], page: int, switch: bool = False) -> Buttons:
        # суффикс ":s" — кнопка из /project: разрешено закрыть текущую сессию и открыть новую
        sfx = ":s" if switch else ""
        chunk = projects[page * PAGE : (page + 1) * PAGE]
        rows, row = [], []
        for i, name in enumerate(chunk, start=page * PAGE):
            row.append((name[:40], f"ps:{i}{sfx}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        nav = []
        if page > 0:
            nav.append(("◀️", f"pj:{page - 1}{sfx}"))
        if (page + 1) * PAGE < len(projects):
            nav.append(("▶️", f"pj:{page + 1}{sfx}"))
        if nav:
            rows.append(nav)
        rows.append([("💬 Без проекта (чат + веб-поиск)", f"pc{sfx}")])
        return rows

    async def _on_pick(self, conv: Conv, message_id: MsgId, raw: str, answer: Answer) -> None:
        key = conv.key
        m = self._m(conv)
        projects, picker_text = self.pickers.get(key) or (self.sessions.projects(), "📁 В каком проекте работаем?")
        data, switch = raw.removesuffix(":s"), raw.endswith(":s")
        if data.startswith("pj:"):
            await answer()
            await m.edit(conv, message_id, picker_text, self._picker_kb(projects, int(data[3:]), switch))
            return
        if self.sessions.get(key):
            if not switch:
                await answer(f"Сессия уже запущена. Сменить проект — {m.cmd_prefix}project", True)
                return
            await self.sessions.stop(key)
        project = None if data == "pc" else projects[int(data[3:])]
        if project is not None and not (self.cfg.repos_dir / project).is_dir():
            await answer("Папка пропала", True)
            return
        await answer()
        self.pickers.pop(key, None)
        title = project or "💬 без проекта"
        await m.edit(conv, message_id, f"🚀 Запускаю Claude: <b>{esc(title)}</b>…")
        messages = self.pending.pop(key, [])
        chat_dir = self.cfg.state_dir / "chat" / conv.slug if project is None else None
        prompts = [p for p in [await self._prompt_of(inc, chat_dir) for inc in messages] if p]
        try:
            await self.sessions.start(
                conv, project, "\n\n".join(prompts) or None, reaction_ids=[inc.message_id for inc in messages]
            )
        except Exception as e:
            log.exception("не удалось запустить сессию")
            await m.edit(conv, message_id, f"❌ Не удалось запустить Claude: {esc(str(e))}")
            return
        hint = "" if prompts else "\nПиши задачу."
        await m.edit(conv, message_id, f"✅ Сессия запущена: <b>{esc(title)}</b>{hint}\n<i>{m.cmd_prefix}status — подробности</i>")
        if m.supports_rename:
            await m.rename_thread(conv, title)
