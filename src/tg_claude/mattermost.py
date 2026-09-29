"""Mattermost: WebSocket для входящих, REST для исходящих, HTTP-сервер для нажатий кнопок.

Тред = корневой пост. В личке каждый новый пост (не в треде) начинает новую сессию,
в каналах бота зовут упоминанием, а в треде с сессией можно писать уже без него.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Callable

import aiohttp
from aiohttp import web

from .config import Config
from .core import ALIASES, COMMANDS
from .messenger import Buttons, Conv, FileRef, Incoming
from .render import html_to_md, split_md

log = logging.getLogger(__name__)
LIMIT = 15000  # у Mattermost по умолчанию 16383 символа на пост
_EMOJI = {"👀": "eyes", "👍": "+1"}


class MMError(RuntimeError):
    pass


def route(post: dict, channel_type: str, mentions: list[str], me_id: str, me_name: str,
          known: Callable[[str], bool]) -> tuple[Conv, str] | None:
    """К какому треду относится пост и какой в нём текст для бота. None — пост не для нас."""
    if post.get("user_id") == me_id or post.get("type"):  # свои и системные сообщения
        return None
    text = post.get("message") or ""
    conv = Conv("mm", post.get("channel_id", ""), post.get("root_id") or post.get("id", ""))
    mention = re.compile(rf"(?i)(?<![\w.-])@{re.escape(me_name)}(?![\w-])") if me_name else None
    mentioned = me_id in mentions or bool(mention and mention.search(text))
    if channel_type != "D" and not mentioned and not known(conv.key):
        return None
    if mention:
        text = mention.sub("", text)
    return conv, text.strip()


class Mattermost:
    platform = "mm"
    name = "Mattermost"
    cmd_prefix = "!"
    supports_rename = False
    supports_delete_thread = False  # корневой пост пользователя бот удалить не может

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.mm_url + "/api/v4"
        self.http: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.seq = 0
        self.me_id = ""
        self.me_name = ""
        self.allowed: set[str] = set()  # id пользователей
        # секрет в кнопках: чужой не подделает нажатие; стабилен между перезапусками
        self.secret = hashlib.sha256(f"tgc-mm:{cfg.mm_token}".encode()).hexdigest()[:32]
        self.core = None  # ChatCore, задаётся в attach()
        self.runner: web.AppRunner | None = None
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()

    def attach(self, core) -> None:
        self.core = core

    # ---------- REST ----------

    async def _api(self, method: str, path: str, raw: bool = False, **kw):
        """raw=True — вернуть тело как есть (файлы: .json-вложение не должно парситься)."""
        for attempt in range(5):
            async with self.http.request(method, self.base + path, **kw) as r:
                if r.status == 429 and attempt < 4:
                    await asyncio.sleep(1 + attempt)
                    continue
                if r.status >= 400:
                    raise MMError(f"{method} {path}: {r.status} {(await r.text())[:300]}")
                if not raw and r.content_type == "application/json":
                    return await r.json()
                return await r.read()

    async def start(self) -> None:
        self.http = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self.cfg.mm_token}"})
        me = await self._api("GET", "/users/me")
        self.me_id, self.me_name = me["id"], me["username"]
        users = await self._api("POST", "/users/usernames", json=sorted(self.cfg.mm_users))
        self.allowed = {u["id"] for u in users}
        missing = self.cfg.mm_users - {u["username"].lower() for u in users}
        if missing:
            log.warning("Mattermost: не нашёл пользователей %s", ", ".join(sorted(missing)))
        await self._start_callbacks()
        log.info("Mattermost: бот @%s на %s, кнопки: %s", self.me_name, self.cfg.mm_url, self.cfg.mm_callback_url)

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
        if self.http:
            await self.http.close()

    # ---------- Messenger ----------

    def _props(self, conv: Conv, buttons: Buttons | None) -> dict:
        if not buttons:
            return {"attachments": []}
        actions = []
        for row in buttons:
            for label, data in row:
                actions.append({
                    "id": f"b{len(actions)}",
                    "name": label,
                    "type": "button",
                    "integration": {
                        "url": self.cfg.mm_callback_url,
                        "context": {"data": data, "channel": conv.chat, "thread": conv.thread, "secret": self.secret},
                    },
                })
        return {"attachments": [{"text": "", "actions": actions}]}

    async def _post(self, conv: Conv, message: str, props: dict | None = None) -> str | None:
        body = {"channel_id": conv.chat, "root_id": conv.thread, "message": message}
        if props:
            body["props"] = props
        try:
            post = await self._api("POST", "/posts", json=body)
        except (MMError, aiohttp.ClientError) as e:
            log.error("Mattermost: не удалось отправить сообщение: %s", e)
            return None
        return post["id"]

    async def send(self, conv: Conv, text: str, buttons: Buttons | None = None) -> str | None:
        chunks = split_md(html_to_md(text), LIMIT) or ["…"]
        for chunk in chunks[:-1]:
            await self._post(conv, chunk)
        return await self._post(conv, chunks[-1], self._props(conv, buttons) if buttons else None)

    async def send_markdown(self, conv: Conv, text: str) -> None:
        for chunk in split_md(text, LIMIT):
            await self._post(conv, chunk)

    async def edit(self, conv: Conv, message_id: str, text: str, buttons: Buttons | None = None) -> None:
        body = {"message": html_to_md(text)[:LIMIT], "props": self._props(conv, buttons)}
        try:
            await self._api("PUT", f"/posts/{message_id}/patch", json=body)
        except (MMError, aiohttp.ClientError) as e:
            log.warning("Mattermost: не удалось отредактировать сообщение: %s", e)

    async def react(self, conv: Conv, message_id: str, emoji: str) -> None:
        name = _EMOJI.get(emoji, emoji)
        try:
            await self._api("POST", "/reactions", json={"user_id": self.me_id, "post_id": message_id, "emoji_name": name})
        except (MMError, aiohttp.ClientError) as e:
            log.debug("реакция не поставилась: %s", e)
            return
        # как в Telegram: новая реакция бота заменяет прежнюю
        for other in _EMOJI.values():
            if other != name:
                try:
                    await self._api("DELETE", f"/users/{self.me_id}/posts/{message_id}/reactions/{other}")
                except (MMError, aiohttp.ClientError):
                    pass

    async def typing(self, conv: Conv) -> None:
        if self.ws is None or self.ws.closed:
            return
        self.seq += 1
        try:
            await self.ws.send_json({
                "seq": self.seq, "action": "user_typing",
                "data": {"channel_id": conv.chat, "parent_id": conv.thread},
            })
        except Exception:
            pass

    async def rename_thread(self, conv: Conv, name: str) -> bool:
        return False  # у тредов Mattermost нет названий

    async def new_thread(self, conv: Conv, title: str) -> Conv:
        raise RuntimeError("в Mattermost новая сессия — просто новое сообщение вне треда")

    async def delete_thread(self, conv: Conv) -> None:
        raise RuntimeError("в Mattermost бот не может удалить тред")

    # ---------- входящие ----------

    async def run(self) -> None:
        url = re.sub(r"^http", "ws", self.base) + "/websocket"
        delay = 1
        while True:
            try:
                async with self.http.ws_connect(url, heartbeat=30) as ws:
                    self.ws, delay = ws, 1
                    log.info("Mattermost: WebSocket подключён")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._dispatch(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Mattermost: WebSocket упал: %s", e)
            finally:
                self.ws = None
            log.info("Mattermost: переподключаюсь через %dс", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _dispatch(self, ev: dict) -> None:
        event = ev.get("event")
        if event not in ("posted", "post_edited"):
            return
        data = ev.get("data") or {}
        try:
            post = json.loads(data.get("post") or "{}")
            mentions = json.loads(data.get("mentions") or "[]")
        except ValueError:
            return
        self._spawn(self._handle(event, post, data.get("channel_type", ""), mentions))

    async def _handle(self, event: str, post: dict, channel_type: str, mentions: list[str]) -> None:
        routed = route(post, channel_type, mentions, self.me_id, self.me_name, self.core.knows)
        if not routed:
            return
        conv, text = routed
        user = post.get("user_id")
        log.info("входящее mm %s [%s] от %s: %d симв. %r", event, conv.key, user, len(text), text[:60])
        if user not in self.allowed:
            log.info("Mattermost: пользователь %s не в MM_ALLOWED_USERS — игнорирую", user)
            return
        # сообщения одного треда обрабатываем строго по очереди
        async with self.locks.setdefault(conv.key, asyncio.Lock()):
            try:
                if event == "post_edited":
                    await self.core.on_edited(conv)
                    return
                inc = Incoming(conv, post["id"], text, self._files(post))
                if text.startswith("!"):
                    word = text[1:].split(maxsplit=1)[0].lower() if text[1:].strip() else ""
                    if word in COMMANDS or word in ALIASES:
                        await self.core.command(word, inc)
                        return
                    inc.text = "/" + text[1:]  # !compact -> /compact для Claude
                await self.core.on_message(inc)
            except Exception:
                log.exception("Mattermost: ошибка обработки сообщения")

    def _files(self, post: dict) -> list[FileRef]:
        infos = {f["id"]: f.get("name") or f["id"] for f in (post.get("metadata") or {}).get("files") or []}
        files = []
        for fid in post.get("file_ids") or []:
            async def fetch(path: Path, fid=fid) -> None:
                path.write_bytes(await self._api("GET", f"/files/{fid}", raw=True))
            files.append(FileRef(infos.get(fid, fid), fetch))
        return files

    # ---------- нажатия кнопок ----------

    async def _start_callbacks(self) -> None:
        app = web.Application()
        app.router.add_post("/{tail:.*}", self._on_action)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        host, _, port = self.cfg.mm_listen.rpartition(":")
        await web.TCPSite(self.runner, host or "0.0.0.0", int(port)).start()

    async def _on_action(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({}, status=400)
        ctx = body.get("context") or {}
        if ctx.get("secret") != self.secret:
            return web.json_response({}, status=403)
        user = body.get("user_id")
        log.info("кнопка mm %r от %s", ctx.get("data"), user)
        if user not in self.allowed:
            return web.json_response({"ephemeral_text": "Нет доступа"})
        conv = Conv("mm", ctx.get("channel", ""), ctx.get("thread", ""))
        fut = asyncio.get_running_loop().create_future()

        async def answer(text: str | None = None, alert: bool = False) -> None:
            if not fut.done():
                fut.set_result(text if alert else None)

        async def press() -> None:
            try:
                await self.core.on_button(conv, body.get("post_id", ""), str(ctx.get("data", "")), answer)
            except Exception:
                log.exception("Mattermost: ошибка обработки кнопки")
            finally:
                await answer()

        self._spawn(press())
        try:
            text = await asyncio.wait_for(asyncio.shield(fut), 10)
        except TimeoutError:
            text = None
        return web.json_response({"ephemeral_text": text} if text else {})
