"""Чтение транскрипта сессии Claude (~/.claude/projects/*/<session_id>.jsonl).

Вместо парсинга экрана TUI читаем структурированный лог, который Claude пишет сам.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .render import esc

# инструменты, которые не показываем в ленте активности
_HIDDEN_TOOLS = {"AskUserQuestion", "TodoWrite", "ToolSearch", "ExitPlanMode", "EnterPlanMode"}
_ICONS = {
    "Bash": "💻", "Read": "📖", "Edit": "✏️", "MultiEdit": "✏️", "Write": "📝", "NotebookEdit": "✏️",
    "Grep": "🔎", "Glob": "🔎", "WebSearch": "🌐", "WebFetch": "🌐", "Task": "🤖", "Agent": "🤖",
    "Skill": "🧩",
}


@dataclass
class Event:
    kind: str  # "text" | "tool" | "user" | "info"
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
    if t == "system" and rec.get("subtype") == "compact_boundary":
        return [Event("info", "🗜 <i>Контекст сжат</i>")]
    msg = rec.get("message") or {}
    content = msg.get("content")
    if t == "user":
        if rec.get("isMeta"):
            return []
        if isinstance(content, str) and not content.startswith("<"):
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
