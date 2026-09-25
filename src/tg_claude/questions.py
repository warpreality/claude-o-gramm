"""Интерактив с кнопками: запросы разрешений и вопросы Claude (AskUserQuestion)."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup

from . import tg
from .render import esc
from .store import SessionRec

TIMEOUT = 3600


@dataclass
class Pending:
    kind: str  # "perm" | "question"
    rec: SessionRec
    message_id: int
    text: str
    future: asyncio.Future
    options: list[str] = field(default_factory=list)
    multi: bool = False
    selected: set[int] = field(default_factory=set)
    suggestions: list | None = None


class Interactions:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.pending: dict[str, Pending] = {}
        self.awaiting_text: dict[str, str] = {}  # ключ сессии -> id вопроса, ждущего свободный ответ

    # ---------- разрешения ----------

    async def ask_permission(self, rec: SessionRec, data: dict) -> dict:
        tool = data.get("tool_name", "?")
        text = "⚠️ <b>Claude просит разрешение</b>\n\n" + describe_tool(tool, data.get("tool_input") or {})
        suggestions = data.get("permission_suggestions") or None
        pid = secrets.token_hex(4)
        row = [Btn(text="✅ Разрешить", callback_data=f"p:{pid}:y")]
        if suggestions:
            row.append(Btn(text="✅ Всегда", callback_data=f"p:{pid}:a"))
        row.append(Btn(text="❌ Запретить", callback_data=f"p:{pid}:n"))
        msg = await tg.send(self.bot, rec.chat_id, rec.thread_id, text, InlineKeyboardMarkup(inline_keyboard=[row]))
        if not msg:
            return {}
        fut = asyncio.get_running_loop().create_future()
        self.pending[pid] = Pending("perm", rec, msg.message_id, text, fut, suggestions=suggestions)
        try:
            choice = await asyncio.wait_for(fut, TIMEOUT)
        except TimeoutError:
            choice = "n"
            await tg.edit(self.bot, rec.chat_id, msg.message_id, text + "\n\n⌛ <i>Нет ответа — запрещено</i>")
        finally:
            self.pending.pop(pid, None)
        if choice == "n":
            decision = {"behavior": "deny", "message": "Пользователь запретил это действие в Telegram."}
        else:
            decision = {"behavior": "allow"}
            if choice == "a" and suggestions:
                decision["updatedPermissions"] = suggestions
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}

    # ---------- вопросы ----------

    async def ask_questions(self, rec: SessionRec, data: dict) -> dict:
        questions = (data.get("tool_input") or {}).get("questions") or []
        answers = []
        for q in questions:
            answer = await self._ask_one(rec, q)
            if answer is None:
                return _deny("Пользователь не ответил на вопрос в Telegram. Не повторяй вопрос, продолжай по своему усмотрению или остановись.")
            answers.append((q.get("question", ""), answer))
        lines = "\n".join(f"- «{q}» → {a}" for q, a in answers)
        return _deny(
            "Пользователь уже ответил на твои вопросы через Telegram-кнопки (это не ошибка):\n"
            f"{lines}\nПродолжай с учётом этих ответов, не задавай эти вопросы повторно."
        )

    async def _ask_one(self, rec: SessionRec, q: dict) -> str | None:
        options = [o.get("label", "") for o in q.get("options") or []]
        multi = bool(q.get("multiSelect"))
        header = q.get("header") or "Вопрос"
        text = f"❓ <b>{esc(header)}</b>\n{esc(q.get('question', ''))}"
        described = [f"• <b>{esc(o.get('label', ''))}</b> — {esc(o['description'])}" for o in q.get("options") or [] if o.get("description")]
        if described:
            text += "\n\n" + "\n".join(described)
        if multi:
            text += "\n\n<i>Можно выбрать несколько, затем «Готово».</i>"
        qid = secrets.token_hex(4)
        p = Pending("question", rec, 0, text, None, options=options, multi=multi)  # type: ignore[arg-type]
        msg = await tg.send(self.bot, rec.chat_id, rec.thread_id, text, self._question_kb(qid, p))
        if not msg:
            return None
        p.message_id = msg.message_id
        p.future = asyncio.get_running_loop().create_future()
        self.pending[qid] = p
        try:
            answer = await asyncio.wait_for(p.future, TIMEOUT)
        except TimeoutError:
            answer = None
        finally:
            self.pending.pop(qid, None)
            if self.awaiting_text.get(rec.key) == qid:
                self.awaiting_text.pop(rec.key)
        shown = esc(answer) if answer is not None else "<i>нет ответа</i>"
        await tg.edit(self.bot, rec.chat_id, p.message_id, f"{text}\n\n<b>Ответ:</b> {shown}")
        return answer

    def _question_kb(self, qid: str, p: Pending) -> InlineKeyboardMarkup:
        rows = []
        for i, label in enumerate(p.options):
            mark = ("☑️ " if i in p.selected else "⬜ ") if p.multi else ""
            rows.append([Btn(text=f"{mark}{label}"[:64], callback_data=f"q:{qid}:{i}")])
        if p.multi:
            rows.append([Btn(text="✅ Готово", callback_data=f"q:{qid}:ok")])
        rows.append([Btn(text="✏️ Свой ответ", callback_data=f"q:{qid}:txt")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ---------- нажатия кнопок и текст ----------

    async def on_callback(self, data: str) -> str:
        """Возвращает текст всплывающего уведомления."""
        kind, pid, value = data.split(":", 2)
        p = self.pending.get(pid)
        if not p or p.future.done():
            return "Вопрос уже неактуален"
        rec = p.rec
        if kind == "p":
            label = {"y": "✅ Разрешено", "a": "✅ Разрешено навсегда", "n": "❌ Запрещено"}[value]
            await tg.edit(self.bot, rec.chat_id, p.message_id, f"{p.text}\n\n<b>{label}</b>")
            p.future.set_result(value)
            return label
        if value == "txt":
            self.awaiting_text[rec.key] = pid
            await tg.send(self.bot, rec.chat_id, rec.thread_id, "✏️ Напиши ответ следующим сообщением")
            return "Жду ответ текстом"
        if value == "ok":
            chosen = [p.options[i] for i in sorted(p.selected)]
            p.future.set_result(", ".join(chosen) if chosen else "(ничего не выбрано)")
            return "Принято"
        idx = int(value)
        if not p.multi:
            p.future.set_result(p.options[idx])
            return p.options[idx]
        p.selected ^= {idx}
        await tg.edit(self.bot, rec.chat_id, p.message_id, p.text, self._question_kb(pid, p))
        return ""

    def take_text_answer(self, key: str, text: str) -> bool:
        qid = self.awaiting_text.pop(key, None)
        p = self.pending.get(qid) if qid else None
        if not p or p.future.done():
            return False
        p.future.set_result(text)
        return True


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


def describe_tool(tool: str, inp: dict) -> str:
    if tool == "Bash":
        out = f"💻 <b>Команда</b>\n<pre><code class=\"language-bash\">{esc(inp.get('command', '')[:3000])}</code></pre>"
        if inp.get("description"):
            out += f"\n<i>{esc(inp['description'])}</i>"
        return out
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit", "Read"):
        return f"📄 <b>{tool}</b> <code>{esc(inp.get('file_path') or inp.get('notebook_path') or '')}</code>"
    if tool == "WebFetch":
        return f"🌐 <b>WebFetch</b> {esc(inp.get('url', ''))}"
    dump = json.dumps(inp, ensure_ascii=False, indent=1)
    if len(dump) > 1500:
        dump = dump[:1500] + "…"
    return f"🔧 <b>{esc(tool)}</b>\n<pre>{esc(dump)}</pre>"
