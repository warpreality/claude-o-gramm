"""Сообщения с форматированием из новых клиентов Telegram (content_type rich_message).

Текста в message.text нет — он разложен по блокам: абзацы, списки и т.д.
Собираем из блоков обычный Markdown для Claude.
"""

from __future__ import annotations

from aiogram.types import Message


def rich_of(message: Message) -> dict | None:
    rich = (message.model_extra or {}).get("rich_message")
    if rich is None:
        rich = message.model_dump(exclude_none=True).get("rich_message")
    return rich if isinstance(rich, dict) else None


def rich_to_text(rich: dict) -> str:
    return "\n\n".join(p for p in (_block(b, 0) for b in rich.get("blocks") or []) if p).strip()


def _block(block: dict, depth: int) -> str:
    kind = block.get("type", "")
    if kind == "list":
        return _list(block, depth)
    text = str(block.get("text") or "")
    if kind in ("code", "pre", "preformatted"):
        return f"```{block.get('language') or ''}\n{text}\n```"
    if kind in ("quote", "blockquote"):
        inner = text or "\n\n".join(_block(b, depth) for b in block.get("blocks") or [])
        return "\n".join(f"> {ln}" for ln in inner.splitlines())
    if kind.startswith("heading") or kind in ("header", "title"):
        return f"**{text}**"
    parts = [text] if text else []
    parts += [p for p in (_block(b, depth) for b in block.get("blocks") or []) if p]
    return "\n".join(parts)


def _list(block: dict, depth: int) -> str:
    indent = "   " * depth
    lines = []
    for item in block.get("items") or []:
        label = item.get("label") or "•"
        if label in ("•", "-", "*", "◦") or item.get("type") in ("bullet", "disc"):
            label = "-"
        inner = [_block(b, depth + 1) for b in item.get("blocks") or []]
        body = "\n".join(p for p in inner if p) or str(item.get("text") or "")
        first, *rest = body.split("\n") or [""]
        lines.append(f"{indent}{label} {first}")
        lines += [ln if ln.startswith(indent + "   ") else f"{indent}   {ln}" for ln in rest]
    return "\n".join(lines)
