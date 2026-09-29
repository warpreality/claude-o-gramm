"""Состояние сессий в state.json: переживает перезапуск сервиса."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .messenger import Conv


@dataclass
class SessionRec:
    chat_id: int | str  # tg: id чата, mm: id канала
    thread_id: int | str  # tg: id треда (0 — основной чат), mm: id корневого поста
    session_id: str
    cwd: str
    project: str | None  # None — режим «без проекта»
    tmux: str
    offset: int = 0  # сколько байт транскрипта уже отправили в Telegram
    pending_reactions: list[int | str] = field(default_factory=list)  # сообщения, ждущие 👍
    topic_title: str | None = None  # заголовок Claude (как он его написал), под которым уже назван тред
    custom_title: bool = False  # заголовок задан через /rename — ai-title его не перетирает
    platform: str = "tg"  # "tg" | "mm"

    @property
    def conv(self) -> Conv:
        return Conv(self.platform, self.chat_id, self.thread_id)

    @property
    def key(self) -> str:
        return self.conv.key

    @property
    def title(self) -> str:
        return self.project or "💬 без проекта"


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.sessions: dict[str, SessionRec] = {}
        if path.exists():
            data = json.loads(path.read_text())
            for key, rec in data.get("sessions", {}).items():
                self.sessions[key] = SessionRec(**rec)

    def get(self, key: str) -> SessionRec | None:
        return self.sessions.get(key)

    def put(self, rec: SessionRec) -> None:
        self.sessions[rec.key] = rec
        self.save()

    def drop(self, key: str) -> None:
        self.sessions.pop(key, None)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": {k: asdict(v) for k, v in self.sessions.items()}}, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)
