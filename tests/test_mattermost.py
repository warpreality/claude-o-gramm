import asyncio

from tg_claude.core import ChatCore
from tg_claude.mattermost import route
from tg_claude.messenger import Conv, Incoming
from tg_claude.render import html_to_md, split_md

ME, NAME = "botid", "claude"


def post(**kw):
    return {"id": "p1", "channel_id": "c1", "user_id": "u1", "root_id": "", "message": "привет", **kw}


def never(_key):
    return False


def test_route_dm_root_starts_thread():
    conv, text = route(post(), "D", [], ME, NAME, never)
    assert conv == Conv("mm", "c1", "p1") and text == "привет"
    assert conv.key == "mm:c1:p1"


def test_route_dm_reply_goes_to_root():
    conv, _ = route(post(root_id="r0"), "D", [], ME, NAME, never)
    assert conv.thread == "r0"


def test_route_channel_needs_mention():
    assert route(post(), "O", [], ME, NAME, never) is None
    conv, text = route(post(message="@claude сделай ревью"), "O", [], ME, NAME, never)
    assert text == "сделай ревью" and conv.thread == "p1"
    _, text = route(post(message="глянь"), "O", [ME], ME, NAME, never)
    assert text == "глянь"


def test_route_channel_thread_with_session_without_mention():
    conv, _ = route(post(root_id="r0", message="дальше"), "O", [], ME, NAME, lambda k: k == "mm:c1:r0")
    assert conv.thread == "r0"


def test_route_ignores_own_and_system_posts():
    assert route(post(user_id=ME), "D", [], ME, NAME, never) is None
    assert route(post(type="system_join_channel"), "D", [], ME, NAME, never) is None


def test_route_does_not_strip_other_mentions():
    _, text = route(post(message="@claude2 и @claude"), "O", [], ME, NAME, never)
    assert text == "@claude2 и"


def test_html_to_md():
    assert html_to_md("<b>Проект:</b> x <i>курсив</i> <code>a_b</code>") == "**Проект:** x *курсив* `a_b`"
    assert html_to_md("файл src/__init__.py") == r"файл src/\_\_init\_\_.py"
    assert html_to_md('<a href="https://x.io/a_b">тут</a> https://y.io/c_d') == "[тут](https://x.io/a_b) https://y.io/c_d"
    assert html_to_md('текст:\n<pre><code class="language-bash">a &lt; b</code></pre>дальше') == "текст:\n```bash\na < b\n```\nдальше"
    assert html_to_md("<blockquote>раз\nдва</blockquote>") == "> раз\n> два"


def test_split_md_keeps_code_fences():
    text = "начало\n```py\n" + "x = 1\n" * 50 + "```\nконец"
    chunks = split_md(text, 100)
    assert len(chunks) > 1 and all(len(c) <= 100 for c in chunks)
    for c in chunks:
        assert c.count("```") % 2 == 0
    assert chunks[0].startswith("начало") and chunks[-1].endswith("конец")


def test_split_md_short_text_untouched():
    assert split_md("abc\n\ndef", 100) == ["abc\n\ndef"]


# ---------- общая логика на фейковом мессенджере ----------

class FakeMessenger:
    platform, name, cmd_prefix, supports_rename = "mm", "Mattermost", "!", False

    def __init__(self):
        self.sent, self.edits, self.reactions = [], [], []

    async def send(self, conv, text, buttons=None):
        self.sent.append((text, buttons))
        return f"m{len(self.sent)}"

    async def edit(self, conv, message_id, text, buttons=None):
        self.edits.append((message_id, text, buttons))

    async def react(self, conv, message_id, emoji):
        self.reactions.append((message_id, emoji))


class FakeSessions:
    def __init__(self, projects):
        self._projects = projects

    def projects(self):
        return self._projects

    def get(self, key):
        return None


class FakeInteractions:
    def take_text_answer(self, key, text):
        return False


def test_core_first_message_shows_picker_and_pages():
    m = FakeMessenger()
    core = ChatCore(None, FakeSessions([f"proj{i}" for i in range(10)]), FakeInteractions(), {"mm": m})
    conv = Conv("mm", "c1", "p1")

    async def scenario():
        await core.on_message(Incoming(conv, "p1", "сделай задачу"))
        await core.on_message(Incoming(conv, "p2", "и ещё вот это"))
        assert core.knows(conv.key)
        answers = []

        async def answer(text=None, alert=False):
            answers.append(text)

        await core.on_button(conv, "m1", "pj:1", answer)
        return answers

    answers = asyncio.run(scenario())
    assert len(m.sent) == 1  # пикер показан один раз
    text, buttons = m.sent[0]
    assert "В каком проекте" in text
    labels = [label for row in buttons for label, _ in row]
    assert "proj0" in labels and "▶️" in labels and labels[-1].startswith("💬")
    assert [inc.text for inc in core.pending[conv.key]] == ["сделай задачу", "и ещё вот это"]
    assert m.reactions == [("p1", "👀"), ("p2", "👀")]
    # вторая страница: осталось 2 проекта и кнопка «назад»
    _, _, page2 = m.edits[-1]
    labels2 = [label for row in page2 for label, _ in row]
    assert labels2[:2] == ["proj8", "proj9"] and "◀️" in labels2
    assert answers == [None]
