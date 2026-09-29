import pytest

from tg_claude.config import Config


def _env(monkeypatch, tmp_path, **env):
    for k in ("BOT_TOKEN", "ALLOWED_USER_IDS", "MM_URL", "MM_TOKEN", "MM_ALLOWED_USERS", "MM_CALLBACK_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TGC_STATE_DIR", str(tmp_path / "st"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_incomplete_mattermost_does_not_break_telegram(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, BOT_TOKEN="1:x", ALLOWED_USER_IDS="1", MM_URL="https://mm", MM_TOKEN="t", MM_ALLOWED_USERS="me")
    cfg = Config.load(str(tmp_path))
    assert cfg.bot_token and not cfg.mm_url and "MM_CALLBACK_URL" in cfg.mm_error


def test_incomplete_mattermost_alone_is_fatal(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, MM_URL="https://mm", MM_TOKEN="t", MM_ALLOWED_USERS="me")
    with pytest.raises(SystemExit, match="MM_CALLBACK_URL"):
        Config.load(str(tmp_path))


def test_full_mattermost_config(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, MM_URL="https://mm/", MM_TOKEN="t", MM_ALLOWED_USERS="@Me", MM_CALLBACK_URL="http://h:8765")
    cfg = Config.load(str(tmp_path))
    assert cfg.mm_url == "https://mm" and cfg.mm_users == {"me"} and not cfg.mm_error
