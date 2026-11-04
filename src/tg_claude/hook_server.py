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
        app.router.add_post("/send-file", self.send_file)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.socket_path.unlink(missing_ok=True)
        await web.UnixSite(self.runner, str(self.socket_path)).start()
        self.socket_path.chmod(0o600)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def send_file(self, request: web.Request) -> web.Response:
        """Команда send-file из сессии Claude: отправить файл в тред этой сессии."""
        key = request.query.get("key", "")
        data = await request.json()
        if not self.sessions.get(key):
            return web.json_response({"error": "сессия не найдена — файл не отправлен"})
        path = Path(data.get("path") or "")
        if not path.is_absolute() or not path.is_file():
            return web.json_response({"error": f"файл не найден: {path}"})
        log.info("send-file [%s] %s", key, path)
        try:
            await self.sessions.send_file(key, path, data.get("caption") or "")
        except Exception as e:
            log.warning("send-file [%s] не удалось: %s", key, e)
            return web.json_response({"error": str(e) or type(e).__name__})
        return web.json_response({"ok": True})

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
                plan = (data.get("tool_input") or {}).get("plan")
                if event == "PermissionRequest" and data.get("tool_name") == "ExitPlanMode" and plan:
                    await self.sessions.show_plan(key, plan)
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
