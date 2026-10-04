import asyncio

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetUpdates, SendMessage

from tg_claude import tg


def test_get_updates_waits_retry_after(monkeypatch):
    slept, calls = [], []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(tg.asyncio, "sleep", fake_sleep)

    async def make_request(bot, method):
        calls.append(method)
        if len(calls) < 3:
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=5)
        return "ok"

    result = asyncio.run(tg.PollingFloodWait()(make_request, None, GetUpdates(timeout=30)))
    assert result == "ok"
    assert slept == [5.5, 5.5]


def test_other_methods_pass_through():
    async def make_request(bot, method):
        raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=5)

    try:
        asyncio.run(tg.PollingFloodWait()(make_request, None, SendMessage(chat_id=1, text="x")))
    except TelegramRetryAfter:
        pass
    else:
        raise AssertionError("ожидали TelegramRetryAfter")
