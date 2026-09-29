"""Общий интерфейс мессенджера: Telegram и Mattermost реализуют его одинаково.

Служебные сообщения бот пишет в HTML-подмножестве Telegram (b, i, code, pre, a, blockquote) —
Mattermost-реализация сама переводит его в Markdown. Ответы Claude идут через send_markdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

# ряды кнопок: [(текст, callback-data), …]
Buttons = list[list[tuple[str, str]]]
MsgId = int | str


@dataclass(frozen=True)
class Conv:
    """Место разговора: чат/канал + тред."""

    platform: str  # "tg" | "mm"
    chat: int | str
    thread: int | str  # tg: id треда (0 — без треда), mm: id корневого поста

    @property
    def key(self) -> str:
        base = f"{self.chat}:{self.thread}"
        return base if self.platform == "tg" else f"{self.platform}:{base}"

    @property
    def slug(self) -> str:
        """Ключ, пригодный для имён файлов и tmux-сессий."""
        return self.key.replace(":", "_")


@dataclass
class FileRef:
    name: str
    fetch: Callable[[Path], Awaitable[None]]  # скачать в указанный путь


@dataclass
class Incoming:
    conv: Conv
    message_id: MsgId
    text: str
    files: list[FileRef] = field(default_factory=list)


class Messenger(Protocol):
    platform: str
    name: str  # для пользователя и системного промпта: "Telegram" / "Mattermost"
    cmd_prefix: str  # "/" или "!"
    supports_rename: bool
    supports_delete_thread: bool

    async def send(self, conv: Conv, text: str, buttons: Buttons | None = None) -> MsgId | None: ...
    async def send_markdown(self, conv: Conv, text: str) -> None: ...
    async def edit(self, conv: Conv, message_id: MsgId, text: str, buttons: Buttons | None = None) -> None: ...
    async def react(self, conv: Conv, message_id: MsgId, emoji: str) -> None: ...
    async def typing(self, conv: Conv) -> None: ...
    async def rename_thread(self, conv: Conv, name: str) -> bool: ...
    async def new_thread(self, conv: Conv, title: str) -> Conv: ...
    async def delete_thread(self, conv: Conv) -> None: ...
