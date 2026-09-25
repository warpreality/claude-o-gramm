"""Сессии Claude в tmux: запуск, ввод, трансляция ответов в Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot

from . import hook as hook_module
from . import tg, tmux
from .config import Config
from .render import esc, render
from .store import SessionRec, Store
from .transcript import find_transcript, read_new, to_events

log = logging.getLogger(__name__)

# переменные окружения родительского Claude, которые ломают запуск вложенного
_UNSET_VARS = [
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_EXECPATH", "AI_AGENT", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_DESKTOP_APP_VERSION", "VIRTUAL_ENV",
]
CHAT_TOOLS = "WebSearch,WebFetch,Read,AskUserQuestion"
SYSTEM_PROMPT = (
    "Пользователь общается с тобой через Telegram-бота: он видит только твои текстовые ответы "
    "и короткие строки о вызванных инструментах, но не экран терминала. Пиши обычным Markdown. "
    "Если нужно уточнение или выбор — используй инструмент AskUserQuestion, пользователь ответит кнопками. "
    "Файлы, которые пользователь присылает, сохраняются на диск, путь приходит в сообщении."
)
CHAT_PROMPT = (
    " Сейчас режим «без проекта»: доступны только веб-поиск, загрузка страниц и чтение присланных файлов; "
    "доступа к проектам и командам нет."
)
_READY = ("for shortcuts", "esc to interrupt", "? for", "shift+tab to cycle", "auto mode on", "accept edits on", "plan mode on")


@dataclass
class Live:
    """Рантайм-состояние сессии (не сохраняется)."""

    rec: SessionRec
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tail_task: asyncio.Task | None = None
    typing_task: asyncio.Task | None = None
    busy: bool = False
    waiting: bool = False  # ждём ответа пользователя на кнопки — «печатает» не показываем
    activity_id: int | None = None
    activity: list[str] = field(default_factory=list)
    activity_dirty: bool = False
    activity_edit_at: float = 0.0
    sent_texts: deque = field(default_factory=lambda: deque(maxlen=50))
    starting: asyncio.Event = field(default_factory=asyncio.Event)
    watchdog: asyncio.Task | None = None


class SessionManager:
    def __init__(self, cfg: Config, store: Store, bot: Bot):
        self.cfg = cfg
        self.store = store
        self.bot = bot
        self.live: dict[str, Live] = {}
        (cfg.state_dir / "settings").mkdir(exist_ok=True)
        (cfg.state_dir / "chat").mkdir(exist_ok=True)
        (cfg.state_dir / "uploads").mkdir(exist_ok=True)

    # ---------- публичное API ----------

    def projects(self) -> list[str]:
        return sorted(
            (p.name for p in self.cfg.repos_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=str.lower,
        )

    def get(self, key: str) -> Live | None:
        return self.live.get(key)

    async def restore(self) -> None:
        for rec in list(self.store.sessions.values()):
            live = self._live(rec)
            live.starting.set()
            if await tmux.has_session(rec.tmux):
                self._start_tail(live)
                log.info("подхватил живую сессию %s (%s)", rec.tmux, rec.title)

    async def start(
        self, chat_id: int, thread_id: int, project: str | None, prompt: str | None, reaction_ids: list[int] = (),
    ) -> Live:
        if project is None:
            cwd = self.cfg.state_dir / "chat" / f"{chat_id}_{thread_id}"
            cwd.mkdir(parents=True, exist_ok=True)
        else:
            cwd = self.cfg.repos_dir / project
        rec = SessionRec(
            chat_id=chat_id, thread_id=thread_id, session_id=str(uuid.uuid4()),
            cwd=str(cwd), project=project, tmux=f"tgc-{chat_id}-{thread_id}", pending_reactions=list(reaction_ids),
        )
        await tmux.kill_session(rec.tmux)
        old = self.live.pop(rec.key, None)
        if old:
            self._cancel(old)
        self.store.put(rec)
        live = self._live(rec)
        if prompt:
            live.sent_texts.append(_norm(prompt))
            self._set_busy(live, True)
        await self._launch(live, resume=False, prompt=prompt)
        return live

    async def send(self, key: str, text: str) -> None:
        live = self.live[key]
        await live.starting.wait()
        live.sent_texts.append(_norm(text))
        self._set_busy(live, True)
        if not await tmux.has_session(live.rec.tmux):
            await tg.send(self.bot, live.rec.chat_id, live.rec.thread_id, "♻️ <i>Сессия была закрыта — поднимаю заново…</i>")
            await self._launch(live, resume=True, prompt=text)
            return
        await self._submit(live, text)

    async def _submit(self, live: Live, text: str) -> None:
        """Набирает сообщение в TUI, отправляет и проверяет, что оно ушло."""
        name = live.rec.tmux
        offset_before = live.rec.offset
        await tmux.type_text(name, text)
        await asyncio.sleep(0.4)
        await tmux.send_keys(name, "Enter")
        for _ in range(2):
            await asyncio.sleep(1.5)
            if not _input_text(await tmux.capture(name)):
                break
            log.warning("сообщение осталось в поле ввода %s — жму Enter ещё раз", name)
            await tmux.send_keys(name, "Enter")
        if live.watchdog and not live.watchdog.done():
            live.watchdog.cancel()
        live.watchdog = asyncio.create_task(self._watchdog(live, offset_before))

    async def _watchdog(self, live: Live, offset_before: int) -> None:
        """Если Claude никак не отреагировал — показываем пользователю экран терминала."""
        rec = live.rec
        await asyncio.sleep(20)
        while True:
            if rec.offset != offset_before:
                return
            screen = await tmux.capture(rec.tmux)
            if "esc to interrupt" not in screen:
                break
            await asyncio.sleep(20)  # Claude занят (долгая команда) — это не зависание
        lines = [ln.rstrip() for ln in screen.splitlines() if ln.strip() and not set(ln.strip()) <= set("─━")]
        tail = "\n".join(lines[-25:])[-3000:]
        log.warning("Claude не отреагировал на сообщение в %s, экран:\n%s", rec.tmux, tail)
        self._set_busy(live, False)
        # нет строки ввода «❯» — значит, открыто окно (например, /cost или /config), оно блокирует ввод
        modal = not any(ln.startswith("❯") for ln in screen.splitlines())
        note = "\n<i>Закрыл это окно, можно писать дальше.</i>" if modal else ""
        await tg.send(
            self.bot, rec.chat_id, rec.thread_id,
            f"🤔 <i>Claude не ответил. Вот что на экране терминала:</i>\n<pre>{esc(tail)}</pre>{note}",
        )
        if modal:
            await tmux.send_keys(rec.tmux, "Escape")

    async def interrupt(self, key: str) -> None:
        live = self.live[key]
        await tmux.send_keys(live.rec.tmux, "Escape")
        self._set_busy(live, False)

    async def stop(self, key: str) -> None:
        live = self.live.pop(key, None)
        if live:
            self._cancel(live)
            await tmux.kill_session(live.rec.tmux)
        self.store.drop(key)

    def on_turn_end(self, key: str) -> None:
        """Хук Stop: Claude закончил ход. Отвечаем хуку сразу, а финализируем в фоне:
        последняя реплика попадает в транскрипт чуть позже самого хука."""
        live = self.live.get(key)
        if live:
            asyncio.create_task(self._finish_turn(live))

    async def _finish_turn(self, live: Live) -> None:
        path = find_transcript(live.rec.session_id)
        if path:
            stable_since, last_size = time.monotonic(), -1
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                size = path.stat().st_size
                if size != last_size:
                    last_size, stable_since = size, time.monotonic()
                elif time.monotonic() - stable_since > 1.2:
                    break
                await asyncio.sleep(0.3)
            await self._drain(live, path)
        await self._flush_activity(live, force=True)
        live.activity_id, live.activity = None, []
        self._set_busy(live, False)
        rec = live.rec
        for mid in rec.pending_reactions:
            await tg.react(self.bot, rec.chat_id, mid, "👍")
        rec.pending_reactions.clear()
        self.store.save()

    async def flush(self, key: str) -> None:
        """Досылаем всё, что Claude успел написать (перед вопросом/запросом разрешения)."""
        live = self.live.get(key)
        path = find_transcript(live.rec.session_id) if live else None
        if live and path:
            await asyncio.sleep(0.6)  # даём Claude дописать реплику перед вопросом
            await self._drain(live, path)
            await self._flush_activity(live, force=True)

    def add_pending_reaction(self, key: str, message_id: int) -> None:
        live = self.live.get(key)
        if live:
            live.rec.pending_reactions.append(message_id)
            self.store.save()

    def set_waiting(self, key: str, waiting: bool) -> None:
        live = self.live.get(key)
        if live:
            live.waiting = waiting

    # ---------- запуск ----------

    def _live(self, rec: SessionRec) -> Live:
        live = self.live.get(rec.key)
        if live is None or live.rec is not rec:
            live = Live(rec)
            self.live[rec.key] = live
        return live

    def _settings_file(self, rec: SessionRec) -> Path:
        hook_cmd = " ".join(
            shlex.quote(x) for x in (sys.executable, hook_module.__file__, str(self.cfg.socket_path), rec.key)
        )

        def hook(event: str, timeout: int) -> list[dict]:
            return [{"type": "command", "command": f"{hook_cmd} {event}", "timeout": timeout}]

        settings = {
            "enableAllProjectMcpServers": True,  # иначе при старте всплывает диалог выбора MCP
            "hooks": {
                "PermissionRequest": [{"matcher": "*", "hooks": hook("PermissionRequest", 86400)}],
                "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": hook("AskUserQuestion", 86400)}],
                "Stop": [{"hooks": hook("Stop", 60)}],
            }
        }
        path = self.cfg.state_dir / "settings" / f"{rec.chat_id}_{rec.thread_id}.json"
        path.write_text(json.dumps(settings, indent=1))
        return path

    def _command(self, rec: SessionRec, resume: bool) -> str:
        args = [self.cfg.claude_bin]
        args += ["--resume", rec.session_id] if resume else ["--session-id", rec.session_id]
        args += ["--permission-mode", self.cfg.permission_mode, "--settings", str(self._settings_file(rec))]
        system = SYSTEM_PROMPT
        if rec.project is None:
            args += ["--restricted", "--tools", CHAT_TOOLS, "--strict-mcp-config", "--disable-slash-commands"]
            system += CHAT_PROMPT
        args += ["--append-system-prompt", system, *self.cfg.extra_args]
        unset = " ".join(f"-u {v}" for v in _UNSET_VARS)
        return f"cd {shlex.quote(rec.cwd)} && exec env {unset} {shlex.join(args)}"

    async def _launch(self, live: Live, resume: bool, prompt: str | None) -> None:
        rec = live.rec
        live.starting.clear()
        # промпт не передаём аргументом claude: его съедает диалог доверия к папке при первом запуске
        try:
            await tmux.new_session(rec.tmux, rec.cwd, self._command(rec, resume))
            if not live.tail_task or live.tail_task.done():
                self._start_tail(live)
            await self._babysit_startup(live, paste=prompt)
        finally:
            live.starting.set()

    async def _babysit_startup(self, live: Live, paste: str | None) -> None:
        """Проходим стартовые диалоги TUI (доверие к папке, MCP) и ждём готовности."""
        name = live.rec.tmux
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            await asyncio.sleep(0.7)
            if not await tmux.has_session(name):
                await tg.send(self.bot, live.rec.chat_id, live.rec.thread_id, "❌ Claude не запустился. Проверь `claude` на сервере.")
                self._set_busy(live, False)
                return
            screen = await tmux.capture(name)
            if "trust this folder" in screen or "Do you trust" in screen:
                await tmux.send_keys(name, "Enter" if re.search(r"❯\s*(\d\.\s*)?Yes", screen) else "Down")
            elif "MCP servers found" in screen or "MCP server found" in screen:
                await tmux.send_keys(name, "Escape")
            elif any(m in screen for m in _READY):
                break
            elif "Enter to confirm" in screen:
                log.warning("неизвестный диалог при старте %s:\n%s", name, screen)
                await tmux.send_keys(name, "Enter")
        else:
            log.warning("сессия %s не дошла до готовности за 45с", name)
        if paste:
            await asyncio.sleep(1.0)
            await self._submit(live, paste)

    # ---------- трансляция ----------

    def _start_tail(self, live: Live) -> None:
        live.tail_task = asyncio.create_task(self._tail(live), name=f"tail-{live.rec.key}")

    def _cancel(self, live: Live) -> None:
        for task in (live.tail_task, live.typing_task, live.watchdog):
            if task:
                task.cancel()

    async def _tail(self, live: Live) -> None:
        path: Path | None = None
        while True:
            try:
                if path is None:
                    path = find_transcript(live.rec.session_id)
                if path is not None:
                    await self._drain(live, path)
                    await self._flush_activity(live)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка чтения транскрипта %s", live.rec.key)
            await asyncio.sleep(0.7)

    async def _drain(self, live: Live, path: Path) -> None:
        async with live.lock:
            rec = live.rec
            records, offset = read_new(path, rec.offset)
            if offset == rec.offset:
                return
            for r in records:
                for ev in to_events(r, rec.cwd):
                    await self._emit(live, ev)
            rec.offset = offset
            self.store.save()

    async def _emit(self, live: Live, ev) -> None:
        rec = live.rec
        if ev.kind == "text":
            await self._flush_activity(live, force=True)
            live.activity_id, live.activity = None, []
            for chunk in render(ev.text):
                await tg.send(self.bot, rec.chat_id, rec.thread_id, chunk)
        elif ev.kind == "tool":
            live.activity.append(ev.text)
            live.activity_dirty = True
        elif ev.kind == "local":
            await tg.send(self.bot, rec.chat_id, rec.thread_id, ev.text)
            self._set_busy(live, False)
            for mid in rec.pending_reactions:
                await tg.react(self.bot, rec.chat_id, mid, "👍")
            rec.pending_reactions.clear()
        elif ev.kind == "user":
            self._set_busy(live, True)
            norm = _norm(ev.text)
            if norm in live.sent_texts:
                live.sent_texts.remove(norm)
            else:  # напечатали прямо в терминале
                for chunk in render(ev.text, limit=3900):
                    await tg.send(self.bot, rec.chat_id, rec.thread_id, f"🖥 <i>из терминала:</i>\n{chunk}")
        else:
            await tg.send(self.bot, rec.chat_id, rec.thread_id, ev.text)

    async def _flush_activity(self, live: Live, force: bool = False) -> None:
        if not live.activity_dirty:
            return
        if not force and time.monotonic() - live.activity_edit_at < 2.5:
            return
        rec = live.rec
        lines = live.activity[-15:]
        hidden = len(live.activity) - len(lines)
        text = (f"<i>… ещё {hidden}</i>\n" if hidden else "") + "\n".join(lines)
        if live.activity_id is None:
            msg = await tg.send(self.bot, rec.chat_id, rec.thread_id, text)
            live.activity_id = msg.message_id if msg else None
        else:
            await tg.edit(self.bot, rec.chat_id, live.activity_id, text)
        live.activity_dirty = False
        live.activity_edit_at = time.monotonic()

    # ---------- «печатает…» ----------

    def _set_busy(self, live: Live, busy: bool) -> None:
        live.busy = busy
        if busy and (not live.typing_task or live.typing_task.done()):
            live.typing_task = asyncio.create_task(self._typing(live))

    async def _typing(self, live: Live) -> None:
        rec = live.rec
        idle_checks = 0
        while live.busy:
            if not live.waiting:
                await tg.typing(self.bot, rec.chat_id, rec.thread_id)
            await asyncio.sleep(4.5)
            if live.starting.is_set() and not await tmux.has_session(rec.tmux):
                idle_checks += 1
                if idle_checks >= 2:
                    live.busy = False
                    await tg.send(self.bot, rec.chat_id, rec.thread_id, "⚠️ <i>Сессия Claude завершилась. Следующее сообщение запустит её снова.</i>")


def _input_text(screen: str) -> str:
    """Текст в поле ввода TUI (пусто, если там только подсказка)."""
    for line in reversed(screen.splitlines()):
        if line.startswith("❯"):
            text = line[1:].strip()
            return "" if text.startswith('Try "') else text
    return ""


def _norm(text: str) -> str:
    return " ".join(text.split())[:500]
