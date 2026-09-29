"""Интерактив с кнопками: запросы разрешений и вопросы Claude (AskUserQuestion)."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field

from .messenger import Buttons, Messenger, MsgId
from .render import esc
from .store import SessionRec

TIMEOUT = 3600


@dataclass
class Pending:
    kind: str  # "perm" | "question"
    rec: SessionRec
    message_id: MsgId
    text: str
    future: asyncio.Future
    options: list[str] = field(default_factory=list)
    multi: bool = False
    selected: set[int] = field(default_factory=set)
    suggestions: list | None = None
    labels: dict | None = None  # подписи результата для кнопок разрешения


class Interactions:
    def __init__(self, messengers: dict[str, Messenger]):
        self.messengers = messengers
        self.pending: dict[str, Pending] = {}
        self.awaiting_text: dict[str, str] = {}  # ключ сессии -> id вопроса, ждущего свободный ответ

    def _where(self, rec: SessionRec) -> str:
        m = self.messengers.get(rec.platform)
        return m.name if m else "мессенджере"

    async def _send(self, rec: SessionRec, text: str, buttons: Buttons | None = None) -> MsgId | None:
        return await self.messengers[rec.platform].send(rec.conv, text, buttons)

    async def _edit(self, rec: SessionRec, message_id: MsgId, text: str, buttons: Buttons | None = None) -> None:
        await self.messengers[rec.platform].edit(rec.conv, message_id, text, buttons)

    # ---------- разрешения ----------

    async def ask_permission(self, rec: SessionRec, data: dict) -> dict:
        tool = data.get("tool_name", "?")
        suggestions = data.get("permission_suggestions") or None
        pid = secrets.token_hex(4)
        is_plan = tool == "ExitPlanMode"
        if is_plan:  # сам план уже отправлен сообщением выше
            text = "📋 <b>План готов</b> — он в сообщении выше. Выполнять?"
            row = [("✅ Выполнять", f"p:{pid}:y"), ("✏️ Доработать", f"p:{pid}:n")]
            labels = {"y": "✅ План утверждён", "n": "✏️ План не утверждён — напиши, что поменять"}
            suggestions = None
        else:
            text = "⚠️ <b>Claude просит разрешение</b>\n\n" + describe_tool(tool, data.get("tool_input") or {})
            row = [("✅ Разрешить", f"p:{pid}:y")]
            if suggestions:
                row.append(("✅ Всегда", f"p:{pid}:a"))
            row.append(("❌ Запретить", f"p:{pid}:n"))
            labels = None
        mid = await self._send(rec, text, [row])
        if mid is None:
            return {}
        fut = asyncio.get_running_loop().create_future()
        self.pending[pid] = Pending("perm", rec, mid, text, fut, suggestions=suggestions, labels=labels)
        try:
            choice = await asyncio.wait_for(fut, TIMEOUT)
        except TimeoutError:
            choice = "n"
            await self._edit(rec, mid, text + "\n\n⌛ <i>Нет ответа — запрещено</i>")
        finally:
            self.pending.pop(pid, None)
        if choice == "n" and is_plan:
            decision = {"behavior": "deny", "message": (
                "Пользователь не утвердил план. Не начинай выполнение, оставайся в режиме планирования "
                "и дождись его следующего сообщения с правками."
            )}
        elif choice == "n":
            decision = {"behavior": "deny", "message": f"Пользователь запретил это действие в {self._where(rec)}."}
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
                return _deny(f"Пользователь не ответил на вопрос в {self._where(rec)}. Не повторяй вопрос, продолжай по своему усмотрению или остановись.")
            answers.append((q.get("question", ""), answer))
        lines = "\n".join(f"- «{q}» → {a}" for q, a in answers)
        return _deny(
            f"Пользователь уже ответил на твои вопросы кнопками в {self._where(rec)} (это не ошибка):\n"
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
        mid = await self._send(rec, text, self._question_kb(qid, p))
        if mid is None:
            return None
        p.message_id = mid
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
        await self._edit(rec, p.message_id, f"{text}\n\n<b>Ответ:</b> {shown}")
        return answer

    def _question_kb(self, qid: str, p: Pending) -> Buttons:
        rows = []
        for i, label in enumerate(p.options):
            mark = ("☑️ " if i in p.selected else "⬜ ") if p.multi else ""
            rows.append([(f"{mark}{label}"[:64], f"q:{qid}:{i}")])
        if p.multi:
            rows.append([("✅ Готово", f"q:{qid}:ok")])
        rows.append([("✏️ Свой ответ", f"q:{qid}:txt")])
        return rows

    # ---------- нажатия кнопок и текст ----------

    async def on_callback(self, data: str) -> tuple[str, bool]:
        """Возвращает текст всплывающего уведомления и признак «важное» (показать как предупреждение)."""
        kind, pid, value = data.split(":", 2)
        p = self.pending.get(pid)
        if not p or p.future.done():
            return "Вопрос уже неактуален", True
        rec = p.rec
        if kind == "p":
            label = (p.labels or {"y": "✅ Разрешено", "a": "✅ Разрешено навсегда", "n": "❌ Запрещено"})[value]
            await self._edit(rec, p.message_id, f"{p.text}\n\n<b>{label}</b>")
            p.future.set_result(value)
            return label, False
        if value == "txt":
            self.awaiting_text[rec.key] = pid
            await self._send(rec, "✏️ Напиши ответ следующим сообщением")
            return "Жду ответ текстом", False
        if value == "ok":
            chosen = [p.options[i] for i in sorted(p.selected)]
            p.future.set_result(", ".join(chosen) if chosen else "(ничего не выбрано)")
            return "Принято", False
        idx = int(value)
        if not p.multi:
            p.future.set_result(p.options[idx])
            return p.options[idx], False
        p.selected ^= {idx}
        await self._edit(rec, p.message_id, p.text, self._question_kb(pid, p))
        return "", False

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
