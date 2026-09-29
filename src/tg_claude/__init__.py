"""tg-claude: бот для Telegram и Mattermost, который транслирует диалоги в Claude Code, запущенный в tmux."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from dotenv import load_dotenv

from .bot import BotApp
from .config import Config
from .core import ChatCore
from .hook_server import HookServer
from .mattermost import Mattermost
from .messenger import Messenger
from .questions import Interactions
from .sessions import SessionManager
from .store import Store
from .tg import TelegramMessenger

log = logging.getLogger("tg_claude")


async def run(cfg: Config) -> None:
    messengers: dict[str, Messenger] = {}
    bot = mm = None
    if cfg.bot_token:
        bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
        messengers["tg"] = TelegramMessenger(bot)
    if cfg.mm_url:
        mm = Mattermost(cfg)
        messengers["mm"] = mm
    store = Store(cfg.state_dir / "state.json")
    sessions = SessionManager(cfg, store, messengers)
    interactions = Interactions(messengers)
    hooks = HookServer(sessions, interactions, cfg.socket_path)
    core = ChatCore(cfg, sessions, interactions, messengers)

    await hooks.start()
    await sessions.restore()
    jobs = []
    try:
        if bot:
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
            log.info("Telegram: бот @%s запущен, проекты: %s", me.username, cfg.repos_dir)
            dp = BotApp(cfg, bot, core).dispatcher()
            jobs.append(asyncio.create_task(dp.start_polling(
                bot, allowed_updates=["message", "edited_message", "callback_query"], handle_signals=False,
            ), name="telegram"))
        if mm:
            mm.attach(core)
            await mm.start()
            jobs.append(asyncio.create_task(mm.run(), name="mattermost"))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([*jobs, waiter], return_when=asyncio.FIRST_COMPLETED)
        failed = [t for t in done if t is not waiter and not t.cancelled() and t.exception()]
        waiter.cancel()
    finally:
        for t in jobs:
            t.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await hooks.stop()
        if bot:
            await bot.session.close()
        if mm:
            await mm.close()
    if failed:
        raise failed[0].exception()


def main() -> None:
    parser = argparse.ArgumentParser(prog="tg-claude", description="Telegram / Mattermost ↔ Claude Code через tmux")
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
