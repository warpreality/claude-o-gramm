import asyncio
import os
import subprocess
from pathlib import Path

from tg_claude.config import Config
from tg_claude.hook_server import HookServer
from tg_claude.messenger import Conv
from tg_claude.sessions import SessionManager, claude_shell
from tg_claude.store import SessionRec, Store


class FileMessenger:
    platform, name, cmd_prefix, supports_rename, supports_delete_thread = "tg", "Telegram", "/", True, True

    def __init__(self, fail: str | None = None):
        self.files = []
        self.fail = fail

    async def send_file(self, conv, path, caption=""):
        if self.fail:
            raise ValueError(self.fail)
        self.files.append((conv, path, caption))


def _run(tmp_path, messenger, args):
    cfg = Config(bot_token="x", allowed_users={1}, repos_dir=tmp_path, state_dir=tmp_path,
                 claude_bin="claude", permission_mode="auto", extra_args=[])
    sessions = SessionManager(cfg, Store(tmp_path / "state.json"), {"tg": messenger})
    rec = SessionRec(chat_id=1, thread_id=2, session_id="s", cwd=str(tmp_path), project="p", tmux="t")
    sessions._live(rec)

    async def flush(key):
        pass

    sessions.flush = flush

    async def main():
        server = HookServer(sessions, None, cfg.socket_path)
        await server.start()
        try:
            env = {**os.environ, "TGC_SOCKET": str(cfg.socket_path), "TGC_KEY": rec.key}
            proc = await asyncio.create_subprocess_exec(
                str(tmp_path / "bin" / "send-file"), *args, cwd=tmp_path, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, err = await proc.communicate()
            return proc.returncode, out.decode() + err.decode()
        finally:
            await server.stop()

    return asyncio.run(main())


def test_send_file_reaches_messenger(tmp_path):
    (tmp_path / "report.txt").write_text("hi")
    m = FileMessenger()
    code, out = _run(tmp_path, m, ["report.txt", "Вот", "отчёт"])
    assert code == 0, out
    assert m.files == [(Conv("tg", 1, 2), tmp_path / "report.txt", "Вот отчёт")]


def test_send_file_reports_errors(tmp_path):
    code, out = _run(tmp_path, FileMessenger(), ["missing.txt"])
    assert code == 1 and "не найден" in out
    (tmp_path / "big.bin").write_text("x")
    code, out = _run(tmp_path, FileMessenger(fail="больше 50 МБ"), ["big.bin"])
    assert code == 1 and "больше 50 МБ" in out


def test_claude_shell_passes_env_and_path(tmp_path):
    cmd = claude_shell("/w", ["claude"], env={"TGC_KEY": "1:2"}, path_prepend=tmp_path / "bin")
    assert "TGC_KEY=1:2 " in cmd and f'PATH={tmp_path}/bin:"$PATH" ' in cmd
