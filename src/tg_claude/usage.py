"""Лимиты подписки: открываем /usage в служебном Claude и разбираем экран."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from . import tmux
from .config import Config
from .render import esc
from .sessions import claude_shell, wait_ready

NAME = "tgc-usage"
_lock = asyncio.Lock()


@dataclass
class Limit:
    title: str
    percent: int
    resets: str


async def fetch(cfg: Config) -> list[Limit]:
    async with _lock:  # два /limits подряд не должны драться за одну сессию
        cwd = cfg.state_dir / "usage"
        cwd.mkdir(exist_ok=True)
        await tmux.kill_session(NAME)
        args = [cfg.claude_bin, "--strict-mcp-config", "--tools", ""]
        await tmux.new_session(NAME, str(cwd), claude_shell(str(cwd), args))
        try:
            if await wait_ready(NAME, timeout=30) is None:
                raise RuntimeError("служебный Claude не запустился")
            await tmux.type_text(NAME, "/usage")
            await asyncio.sleep(0.5)
            await tmux.send_keys(NAME, "Enter")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                await asyncio.sleep(1)
                limits = parse(await tmux.capture(NAME))
                if limits:
                    return limits
            raise RuntimeError("не дождался экрана /usage")
        finally:
            await tmux.kill_session(NAME)


def parse(screen: str) -> list[Limit]:
    lines = [ln.strip() for ln in screen.splitlines() if ln.strip()]
    limits = []
    for i, line in enumerate(lines):
        m = re.search(r"(\d+)% used", line)
        if not m or i == 0:
            continue
        title = lines[i - 1]
        resets = ""
        if i + 1 < len(lines) and lines[i + 1].startswith("Resets"):
            resets = lines[i + 1].removeprefix("Resets").strip()
        limits.append(Limit(title, int(m.group(1)), resets))
    return limits


_TITLES = {"Current session": "Текущая сессия (5 часов)", "Current week (all models)": "Неделя, все модели"}


def format_html(limits: list[Limit]) -> str:
    out = ["📊 <b>Лимиты Claude</b>"]
    for lim in limits:
        title = _TITLES.get(lim.title) or re.sub(r"^Current week \((.+)\)$", r"Неделя, \1", lim.title)
        filled = round(lim.percent / 10)
        icon = "🟢" if lim.percent < 50 else ("🟡" if lim.percent < 80 else "🔴")
        bar = "▰" * filled + "▱" * (10 - filled)
        line = f"\n{icon} <b>{esc(title)}</b>\n<code>{bar}</code> {lim.percent}%"
        if lim.resets:
            line += f"\n<i>сброс: {esc(lim.resets)}</i>"
        out.append(line)
    return "\n".join(out)
