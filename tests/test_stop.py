import asyncio

from tg_claude.core import ChatCore
from tg_claude.messenger import Conv, Incoming


class FakeTg:
    platform, name, cmd_prefix, supports_rename, supports_delete_thread = "tg", "Telegram", "/", True, True

    def __init__(self):
        self.sent, self.edits, self.deleted = [], [], []

    async def send(self, conv, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    async def edit(self, conv, message_id, text, buttons=None):
        self.edits.append((message_id, text))

    async def delete_thread(self, conv):
        self.deleted.append(conv.key)


class FakeSessions:
    def __init__(self, keys):
        self.keys, self.stopped = set(keys), []

    def get(self, key):
        return object() if key in self.keys else None

    async def stop(self, key):
        self.keys.discard(key)
        self.stopped.append(key)


def _run(conv, has_session, choice):
    m, s = FakeTg(), FakeSessions([conv.key] if has_session else [])
    core = ChatCore(None, s, None, {"tg": m})

    async def scenario():
        await core.command("stop", Incoming(conv, 1, "/stop"))
        answers = []

        async def answer(text=None, alert=False):
            answers.append(text)

        if choice:
            await core.on_button(conv, 1, choice, answer)

    asyncio.run(scenario())
    return m, s


def test_stop_asks_before_deleting():
    conv = Conv("tg", 1, 42)
    m, s = _run(conv, True, None)
    labels = [b[0][1] for b in m.sent[0][1]]
    assert labels == ["st:del", "st:stop", "st:no"]
    assert not s.stopped and not m.deleted  # до нажатия ничего не трогаем


def test_stop_delete_closes_session_and_deletes_thread():
    conv = Conv("tg", 1, 42)
    m, s = _run(conv, True, "st:del")
    assert s.stopped == [conv.key] and m.deleted == [conv.key]


def test_stop_only_session_keeps_thread():
    conv = Conv("tg", 1, 42)
    m, s = _run(conv, True, "st:stop")
    assert s.stopped == [conv.key] and not m.deleted


def test_stop_cancel_changes_nothing():
    conv = Conv("tg", 1, 42)
    m, s = _run(conv, True, "st:no")
    assert not s.stopped and not m.deleted


def test_stop_in_main_chat_does_not_offer_delete():
    conv = Conv("tg", 1, 0)  # основной чат без треда — удалять нечего
    m, s = _run(conv, True, None)
    assert s.stopped == [conv.key] and m.sent[0][1] is None
