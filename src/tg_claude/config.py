from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
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

    @property
    def socket_path(self) -> Path:
        return self.state_dir / "hook.sock"

    @classmethod
    def load(cls, repos_dir: str | None) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
        users = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
        if not users:
            raise SystemExit("ALLOWED_USER_IDS не задан — без него бот ответил бы кому угодно")
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
        )
