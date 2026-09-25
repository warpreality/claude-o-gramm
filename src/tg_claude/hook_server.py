"""HTTP-сервер на unix-сокете: сюда стучатся хуки Claude (см. hook.py)."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from .questions import Interactions
from .sessions import SessionManager

log = logging.getLogger(__name__)


class HookServer:
    def __init__(self, sessions: SessionManager, interactions: Interactions, socket_path: Path):
        self.sessions = sessions
        self.interactions = interactions
        self.socket_path = socket_path
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/hook/{event}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.socket_path.unlink(missing_ok=True)
        await web.UnixSite(self.runner, str(self.socket_path)).start()
        self.socket_path.chmod(0o600)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def handle(self, request: web.Request) -> web.Response:
        event = request.match_info["event"]
        key = request.query.get("key", "")
        data = await request.json()
        live = self.sessions.get(key)
        if not live:
            log.warning("хук %s для неизвестной сессии %s", event, key)
            return web.json_response({})
        log.info("хук %s [%s] %s", event, key, data.get("tool_name", ""))
        try:
            if event == "Stop":
                self.sessions.on_turn_end(key)
                return web.json_response({})
            self.sessions.set_waiting(key, True)
            try:
                await self.sessions.flush(key)
                if event == "PermissionRequest":
                    result = await self.interactions.ask_permission(live.rec, data)
                elif event == "AskUserQuestion":
                    result = await self.interactions.ask_questions(live.rec, data)
                else:
                    result = {}
            finally:
                self.sessions.set_waiting(key, False)
            return web.json_response(result)
        except Exception:
            log.exception("ошибка обработки хука %s", event)
            return web.json_response({})
