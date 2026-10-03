import asyncio
from types import SimpleNamespace

from tg_claude import core as core_module
from tg_claude.core import ChatCore
from tg_claude.mattermost import is_audio
from tg_claude.messenger import Conv, FileRef, Incoming


class Messenger:
    platform, name, cmd_prefix, supports_rename, supports_delete_thread = "tg", "Telegram", "/", True, True

    def __init__(self):
        self.sent = []

    async def send(self, conv, text, buttons=None):
        self.sent.append(text)
        return len(self.sent)

    async def react(self, *a):
        pass

    async def typing(self, conv):
        pass


class Sessions:
    def projects(self):
        return ["proj"]

    def get(self, key):
        return None


class Interactions:
    def take_text_answer(self, key, text):
        return False


async def _fetch(path):
    path.write_bytes(b"ogg")


def _run(tmp_path, monkeypatch, inc, transcribe):
    monkeypatch.setattr(core_module.voice, "transcribe", transcribe)
    m = Messenger()
    core = ChatCore(SimpleNamespace(state_dir=tmp_path), Sessions(), Interactions(), {"tg": m})
    asyncio.run(core.on_message(inc))
    return core, m


def test_voice_becomes_text(tmp_path, monkeypatch):
    async def transcribe(path):
        assert path.read_bytes() == b"ogg"
        return "сделай задачу"

    conv = Conv("tg", 1, 2)
    inc = Incoming(conv, 5, "", [FileRef("voice_5.ogg", _fetch, voice=True)])
    core, m = _run(tmp_path, monkeypatch, inc, transcribe)
    assert [i.text for i in core.pending[conv.key]] == ["сделай задачу"]
    assert core.pending[conv.key][0].files == []
    assert m.sent[0] == "🎙 <i>сделай задачу</i>"
    assert not list((tmp_path / "voice").iterdir())  # временный файл удалён


def test_voice_with_caption_and_file(tmp_path, monkeypatch):
    async def transcribe(path):
        return "голос"

    conv = Conv("tg", 1, 2)
    photo = FileRef("a.jpg", _fetch)
    inc = Incoming(conv, 5, "подпись", [photo, FileRef("v.ogg", _fetch, voice=True)])
    core, _ = _run(tmp_path, monkeypatch, inc, transcribe)
    assert core.pending[conv.key][0].text == "подпись\n\nголос"
    assert core.pending[conv.key][0].files == [photo]


def test_failed_transcription_sends_nothing_to_claude(tmp_path, monkeypatch):
    async def transcribe(path):
        raise RuntimeError("модель сломалась")

    conv = Conv("tg", 1, 2)
    inc = Incoming(conv, 5, "", [FileRef("v.ogg", _fetch, voice=True)])
    core, m = _run(tmp_path, monkeypatch, inc, transcribe)
    assert conv.key not in core.pending
    assert m.sent == ["Не смог расшифровать голосовое: модель сломалась"]


def test_mattermost_audio_detection():
    assert is_audio("rec.webm", "")
    assert is_audio("x", "audio/mpeg")
    assert not is_audio("photo.png", "image/png")
