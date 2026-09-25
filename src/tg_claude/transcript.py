"""Чтение транскрипта сессии Claude (~/.claude/projects/*/<session_id>.jsonl).

Вместо парсинга экрана TUI читаем структурированный лог, который Claude пишет сам.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .render import esc

# инструменты, которые не показываем в ленте активности
_HIDDEN_TOOLS = {"AskUserQuestion", "TodoWrite", "ToolSearch", "ExitPlanMode", "EnterPlanMode"}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ICONS = {
    "Bash": "💻", "Read": "📖", "Edit": "✏️", "MultiEdit": "✏️", "Write": "📝", "NotebookEdit": "✏️",
    "Grep": "🔎", "Glob": "🔎", "WebSearch": "🌐", "WebFetch": "🌐", "Task": "🤖", "Agent": "🤖",
    "Skill": "🧩",
}


@dataclass
class Event:
    kind: str  # "text" | "tool" | "user" | "info" | "local" (вывод локальной команды вроде /model)
    text: str  # для text — markdown, для остальных — готовый HTML


def find_transcript(session_id: str) -> Path | None:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"
    matches = list(base.glob(f"*/{session_id}.jsonl"))
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def read_new(path: Path, offset: int) -> tuple[list[dict], int]:
    """Читает целые строки после offset. Недописанная последняя строка остаётся на следующий раз."""
    size = path.stat().st_size
    if size < offset:  # файл пересоздан
        offset = 0
    if size == offset:
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(size - offset)
    end = data.rfind(b"\n")
    if end == -1:
        return [], offset
    records = []
    for line in data[: end + 1].splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records, offset + end + 1


def to_events(rec: dict, cwd: str) -> list[Event]:
    if rec.get("isSidechain"):
        return []
    t = rec.get("type")
    if t == "system":
        return _system_events(rec)
    msg = rec.get("message") or {}
    content = msg.get("content")
    if t == "user":
        if rec.get("isMeta"):
            return []
        if not isinstance(content, str):
            return []
        local = _local_output(content)
        if local:
            return [local]
        if not content.startswith("<"):
            return [Event("user", content)]
        return []
    if t != "assistant" or not isinstance(content, list):
        return []
    events = []
    for block in content:
        bt = block.get("type")
        if bt == "text" and block.get("text", "").strip():
            events.append(Event("text", block["text"]))
        elif bt == "tool_use" and block.get("name") not in _HIDDEN_TOOLS:
            events.append(Event("tool", tool_line(block.get("name", "?"), block.get("input") or {}, cwd)))
    return events


def _system_events(rec: dict) -> list[Event]:
    sub = rec.get("subtype")
    if sub == "compact_boundary":
        return [Event("info", "🗜 <i>Контекст сжат</i>")]
    if sub == "local_command":
        local = _local_output(rec.get("content") or "")
        return [local] if local else []
    if sub == "informational" and rec.get("content"):
        # например «Unknown command: /project» — Stop-хука после такого не будет
        return [Event("local", f"ℹ️ {esc(_ANSI.sub('', rec['content']))}")]
    if sub == "api_error":
        err = rec.get("error") or {}
        attempt, total = rec.get("retryAttempt") or 0, rec.get("maxRetries") or 0
        if attempt in (1, total):
            text = err.get("formatted") or err.get("message") or "неизвестная ошибка"
            retry = f" — повтор {attempt}/{total}" if total and attempt < total else ""
            return [Event("info", f"⚠️ <i>Ошибка API: {esc(str(text)[:500])}{retry}</i>")]
    return []


def _local_output(content: str) -> Event | None:
    """Вывод локальной команды (/model, /cost …): <local-command-stdout>…</local-command-stdout>."""
    m = re.search(r"<local-command-(?:stdout|stderr)>(.*?)</local-command-(?:stdout|stderr)>", content, re.S)
    if not m:
        return None
    out = _ANSI.sub("", m.group(1)).strip()
    return Event("local", f"<pre>{esc(out[:3500])}</pre>" if out else "✅")


def tool_line(name: str, inp: dict, cwd: str) -> str:
    icon = _ICONS.get(name, "🔧")
    detail = ""
    if name == "Bash":
        detail = inp.get("description") or inp.get("command", "")
    elif name in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit"):
        detail = _rel(inp.get("file_path") or inp.get("notebook_path") or "", cwd)
    elif name in ("Grep", "Glob"):
        detail = inp.get("pattern", "")
    elif name == "WebSearch":
        detail = inp.get("query", "")
    elif name == "WebFetch":
        detail = inp.get("url", "")
    elif name in ("Task", "Agent"):
        detail = inp.get("description", "")
    elif name == "Skill":
        detail = inp.get("skill", "")
    elif name.startswith("mcp__"):
        name = name.removeprefix("mcp__").replace("__", " · ")
    detail = " ".join(str(detail).split())
    if len(detail) > 120:
        detail = detail[:117] + "…"
    return f"{icon} <b>{esc(name)}</b> {esc(detail)}".rstrip()


def _rel(path: str, cwd: str) -> str:
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return path
