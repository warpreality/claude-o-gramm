import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import web

from tg_claude import hook as hook_module
from tg_claude import sessions as sessions_module
from tg_claude.config import Config
from tg_claude.core import ChatCore
from tg_claude.messenger import Conv
from tg_claude.sessions import Live, SessionManager
from tg_claude.store import SessionRec, Store


def _cfg(tmp_path):
    return Config(bot_token="x", allowed_users={1}, repos_dir=tmp_path, state_dir=tmp_path,
                  claude_bin="claude", permission_mode="auto", extra_args=[])


class QuietMessenger:
    platform, name, cmd_prefix, supports_rename, supports_delete_thread = "tg", "Telegram", "/", True, True

    async def send(self, conv, text, buttons=None):
        return 1

    async def edit(self, *a, **k):
        pass

    async def typing(self, conv):
        pass


def test_parallel_messages_are_typed_one_after_another(tmp_path, monkeypatch):
    typed = []

    async def type_text(name, text):
        for ch in text:  # печатаем «порциями», отдавая управление — как настоящий tmux
            typed.append(ch)
            await asyncio.sleep(0)

    async def noop(*a, **k):
        return ""

    async def alive(name):
        return True

    monkeypatch.setattr(sessions_module.tmux, "type_text", type_text)
    monkeypatch.setattr(sessions_module.tmux, "send_keys", noop)
    monkeypatch.setattr(sessions_module.tmux, "capture", noop)
    monkeypatch.setattr(sessions_module.tmux, "has_session", alive)

    async def scenario():
        mgr = SessionManager(_cfg(tmp_path), Store(tmp_path / "state.json"), {"tg": QuietMessenger()})
        rec = SessionRec(chat_id=1, thread_id=2, session_id="s", cwd=str(tmp_path), project="p", tmux="t")
        live = Live(rec)
        live.starting.set()
        mgr.live[rec.key] = live
        await asyncio.gather(mgr.send(rec.key, "AAAA"), mgr.send(rec.key, "BBBB"))
        for task in list(mgr._bg):
            task.cancel()

    asyncio.run(scenario())
    assert "".join(typed) in ("AAAABBBB", "BBBBAAAA")


def test_double_tap_on_project_starts_session_once(tmp_path):
    starts = []

    class Sessions:
        def projects(self):
            return ["proj"]

        def get(self, key):
            return None

        async def start(self, conv, project, prompt, reaction_ids=()):
            starts.append(project)
            await asyncio.sleep(0.05)  # запуск Claude занимает время — второе нажатие приходит в это окно

    class M(QuietMessenger):
        async def rename_thread(self, conv, name):
            return True

    (tmp_path / "proj").mkdir()
    core = ChatCore(_cfg(tmp_path), Sessions(), None, {"tg": M()})
    conv = Conv("tg", 1, 2)

    async def answer(text=None, alert=False):
        pass

    async def scenario():
        await asyncio.gather(core.on_button(conv, 1, "ps:0", answer), core.on_button(conv, 1, "ps:0", answer))

    asyncio.run(scenario())
    assert starts == ["proj"]


def test_broken_state_file_does_not_block_start(tmp_path):
    (tmp_path / "state.json").write_text("{не json")
    store = Store(tmp_path / "state.json")
    assert store.sessions == {} and (tmp_path / "state.broken").exists()


def test_unknown_fields_in_state_are_ignored(tmp_path):
    rec = {"chat_id": 1, "thread_id": 2, "session_id": "s", "cwd": "/", "project": None, "tmux": "t", "from_future": 1}
    (tmp_path / "state.json").write_text(json.dumps({"sessions": {"1:2": rec, "bad": {"chat_id": 1}}}))
    store = Store(tmp_path / "state.json")
    assert list(store.sessions) == ["1:2"]  # запись без обязательных полей пропущена, лишнее поле — отброшено


def test_hook_waits_for_service_restart(tmp_path):
    """Сервис недоступен в момент хука (перезапуск) — хук ждёт и получает ответ, когда тот поднимется."""
    # путь к unix-сокету ограничен ~100 символами, а tmp_path на macOS длинный
    sock = Path(tempfile.mkdtemp(prefix="tgc", dir="/tmp")) / "h.sock"
    proc = subprocess.Popen(
        [sys.executable, hook_module.__file__, str(sock), "1:2", "PermissionRequest"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    proc.stdin.write(b"{}")
    proc.stdin.close()

    async def serve():
        await asyncio.sleep(3)  # «сервис перезапускается»

        async def handle(request):
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook/{event}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.UnixSite(runner, str(sock)).start()
        for _ in range(100):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        await runner.cleanup()

    asyncio.run(serve())
    assert json.loads(proc.stdout.read()) == {"ok": True}


def test_idle_sessions_get_closed(tmp_path, monkeypatch):
    killed = []

    async def alive(name):
        return True

    async def kill(name):
        killed.append(name)

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(sessions_module.tmux, "has_session", alive)
    monkeypatch.setattr(sessions_module.tmux, "kill_session", kill)
    monkeypatch.setattr(sessions_module.asyncio, "sleep", fast_sleep)

    async def scenario():
        mgr = SessionManager(_cfg(tmp_path), Store(tmp_path / "state.json"), {"tg": QuietMessenger()})
        for tmux_name, idle_h, busy in (("idle", 7, False), ("fresh", 1, False), ("busy", 7, True)):
            rec = SessionRec(chat_id=1, thread_id=tmux_name, session_id="s", cwd="/", project="p", tmux=tmux_name)
            live = Live(rec, busy=busy)
            live.last_active = time.monotonic() - idle_h * 3600
            mgr.live[rec.key] = live
        task = asyncio.create_task(mgr._reap_idle())
        for _ in range(5):
            await real_sleep(0)
        task.cancel()

    asyncio.run(scenario())
    assert killed and set(killed) == {"idle"}
