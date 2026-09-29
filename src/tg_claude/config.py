from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    bot_token: str
    allowed_users: set[int]
    repos_dir: Path
    state_dir: Path
    claude_bin: str
    permission_mode: str
    extra_args: list[str]
    # Mattermost (пусто — выключен)
    mm_url: str = ""
    mm_token: str = ""
    mm_users: set[str] = field(default_factory=set)  # логины без @
    mm_listen: str = "0.0.0.0:8765"  # где слушать нажатия кнопок
    mm_callback_url: str = ""  # как этот адрес видит сервер Mattermost
    mm_error: str = ""  # почему Mattermost выключен при неполных настройках

    @property
    def socket_path(self) -> Path:
        return self.state_dir / "hook.sock"

    @classmethod
    def load(cls, repos_dir: str | None) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        users = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
        if token and not users:
            raise SystemExit("ALLOWED_USER_IDS не задан — без него бот ответил бы кому угодно")
        mm_url = os.environ.get("MM_URL", "").strip().rstrip("/")
        mm_token = os.environ.get("MM_TOKEN", "").strip()
        mm_users = {x.lstrip("@").lower() for x in os.environ.get("MM_ALLOWED_USERS", "").replace(" ", "").split(",") if x}
        mm_callback = os.environ.get("MM_CALLBACK_URL", "").strip()
        mm_error = ""
        if mm_url or mm_token:
            if not (mm_url and mm_token):
                mm_error = "для Mattermost нужны оба: MM_URL и MM_TOKEN"
            elif not mm_users:
                mm_error = "MM_ALLOWED_USERS не задан — без него бот ответил бы кому угодно"
            elif not mm_callback:
                mm_error = "MM_CALLBACK_URL не задан — без него в Mattermost не работают кнопки (выбор проекта, разрешения)"
        if mm_error:
            # неполные настройки Mattermost не должны ронять Telegram: выключаем только Mattermost
            if not token:
                raise SystemExit(f"Mattermost: {mm_error}")
            mm_url = mm_token = ""
        if not token and not mm_url:
            raise SystemExit("Не настроен ни Telegram (BOT_TOKEN), ни Mattermost (MM_URL, MM_TOKEN) — см. .env.example")
        repos = Path(repos_dir or os.environ.get("REPOS_DIR", "")).expanduser()
        if not repos_dir and not os.environ.get("REPOS_DIR"):
            raise SystemExit("Укажи папку с проектами: tg-claude --repos /path/to/projects")
        if not repos.is_dir():
            raise SystemExit(f"Папка с проектами не найдена: {repos}")
        state = Path(os.environ.get("TGC_STATE_DIR", "~/.tg-claude")).expanduser()
        state.mkdir(parents=True, exist_ok=True)
        return cls(
            bot_token=token,
            allowed_users=users,
            repos_dir=repos.resolve(),
            state_dir=state,
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            permission_mode=os.environ.get("CLAUDE_PERMISSION_MODE", "auto"),
            extra_args=shlex.split(os.environ.get("CLAUDE_EXTRA_ARGS", "")),
            mm_url=mm_url,
            mm_token=mm_token,
            mm_users=mm_users,
            mm_listen=os.environ.get("MM_CALLBACK_LISTEN", "0.0.0.0:8765").strip(),
            mm_callback_url=mm_callback,
            mm_error=mm_error,
        )
