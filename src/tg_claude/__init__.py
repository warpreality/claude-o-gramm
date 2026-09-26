"""tg-claude: Telegram-бот, который транслирует диалоги в Claude Code, запущенный в tmux."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from dotenv import load_dotenv

from .bot import BotApp
from .config import Config
from .hook_server import HookServer
from .questions import Interactions
from .sessions import SessionManager
from .store import Store

log = logging.getLogger("tg_claude")


async def run(cfg: Config) -> None:
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    store = Store(cfg.state_dir / "state.json")
    sessions = SessionManager(cfg, store, bot)
    interactions = Interactions(bot)
    hooks = HookServer(sessions, interactions, cfg.socket_path)
    app = BotApp(cfg, bot, sessions, interactions)

    await hooks.start()
    await sessions.restore()
    await bot.set_my_commands([
        BotCommand(command="new", description="Новый тред / сессия"),
        BotCommand(command="project", description="Выбрать / сменить проект"),
        BotCommand(command="status", description="Статус сессии в этом треде"),
        BotCommand(command="limits", description="Лимиты подписки Claude"),
        BotCommand(command="esc", description="Прервать текущий ответ"),
        BotCommand(command="stop", description="Закрыть сессию"),
        BotCommand(command="help", description="Помощь"),
    ])
    me = await bot.get_me()
    log.info("бот @%s запущен, проекты: %s", me.username, cfg.repos_dir)
    try:
        await app.dispatcher().start_polling(bot, allowed_updates=["message", "edited_message", "callback_query"], handle_signals=True)
    finally:
        await hooks.stop()
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="tg-claude", description="Telegram ↔ Claude Code через tmux")
    parser.add_argument("--repos", help="папка с проектами (или REPOS_DIR в .env)")
    parser.add_argument("--env", default=".env", help="путь к .env (по умолчанию ./.env)")
    args = parser.parse_args()

    load_dotenv(args.env)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    for binary in ("tmux",):
        if not shutil.which(binary):
            raise SystemExit(f"Не найден {binary} — установи его")
    cfg = Config.load(args.repos)
    if not shutil.which(cfg.claude_bin):
        raise SystemExit(f"Не найден claude ({cfg.claude_bin}) — укажи путь в CLAUDE_BIN")
    asyncio.run(run(cfg))
