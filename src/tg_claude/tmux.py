"""Тонкая обёртка над tmux."""

from __future__ import annotations

import asyncio
import os


# отдельный tmux-сервер (tmux -L tg-claude), чтобы не мешать личным сессиям пользователя
SOCKET = os.environ.get("TGC_TMUX_SOCKET", "tg-claude")


class TmuxError(RuntimeError):
    pass


def attach_cmd(name: str) -> str:
    return f"tmux -L {SOCKET} attach -t {name}"


async def _run(*args: str, stdin: bytes | None = None, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "-L", SOCKET, *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), 15)
    except TimeoutError:
        proc.kill()
        raise TmuxError(f"tmux {args[0]}: нет ответа от tmux за 15с")
    if check and proc.returncode:
        raise TmuxError(f"tmux {args[0]}: {err.decode().strip()}")
    return out.decode()


async def has_session(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "-L", SOCKET, "has-session", "-t", f"={name}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(proc.wait(), 15) == 0
    except TimeoutError:
        proc.kill()
        raise TmuxError("tmux has-session: нет ответа от tmux за 15с")


async def new_session(name: str, cwd: str, command: str) -> None:
    await _run("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", cwd, command)


async def kill_session(name: str) -> None:
    await _run("kill-session", "-t", f"={name}", check=False)


async def type_text(name: str, text: str) -> None:
    """Набирает текст как с клавиатуры (без Enter в конце).

    Не используем paste-buffer: вставку Claude помечает как <pasted_content> и относится к ней
    как к недоверенному тексту. Перенос строки в TUI Claude — это «\\» + Enter.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    for n, line in enumerate(lines):
        last = n == len(lines) - 1
        piece = line if last else line + "\\"
        for i in range(0, len(piece), 1000):
            await _run("send-keys", "-t", f"={name}:", "-l", piece[i : i + 1000])
        if not last:
            await _run("send-keys", "-t", f"={name}:", "Enter")


async def send_keys(name: str, *keys: str) -> None:
    await _run("send-keys", "-t", f"={name}:", *keys)


async def capture(name: str) -> str:
    return await _run("capture-pane", "-p", "-t", f"={name}:", check=False)
