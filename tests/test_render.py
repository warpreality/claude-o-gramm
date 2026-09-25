import re

from tg_claude.render import md_to_html, render, split_html


def balanced(s: str) -> bool:
    stack = []
    for closing, name in re.findall(r"<(/?)([a-z-]+)", s):
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def test_inline():
    out = md_to_html("**жирный** и *курсив*, ~~нет~~, `a<b>` и [ссылка](https://x.io?a=1&b=2)")
    assert out == (
        "<b>жирный</b> и <i>курсив</i>, <s>нет</s>, <code>a&lt;b&gt;</code> и "
        '<a href="https://x.io?a=1&amp;b=2">ссылка</a>'
    )


def test_escape_and_unsafe_link():
    assert md_to_html("1 < 2 & 3 > 2") == "1 &lt; 2 &amp; 3 &gt; 2"
    assert "<a" not in md_to_html("[x](javascript:alert(1))")
    assert md_to_html("[x](./rel/path)") == "x"
    assert md_to_html("<div>hi</div>") == "&lt;div&gt;hi&lt;/div&gt;"


def test_code_block():
    out = md_to_html("```python\nif a < b:\n    pass\n```")
    assert out == '<pre><code class="language-python">if a &lt; b:\n    pass</code></pre>'
    assert md_to_html("```\nplain\n```") == "<pre>plain</pre>"


def test_heading_lists_quote():
    out = md_to_html("# Заголовок\n\n- один\n- два\n  - вложенный\n\n1. a\n2. b\n\n> цитата\n> > вложенная")
    assert "<b>Заголовок</b>" in out
    assert "• один\n• два\n   • вложенный" in out
    assert "1. a\n2. b" in out
    assert out.endswith("<blockquote>цитата\nвложенная</blockquote>")
    assert balanced(out)


def test_table():
    out = md_to_html("| a | bb |\n|---|---|\n| **1** | 2 |")
    assert out.startswith("<pre>") and out.endswith("</pre>")
    assert "a │ bb" in out and "1 │ 2" in out and "─┼─" in out


def test_split_long_code_keeps_tags_balanced():
    code = "\n".join(f"line {i} <x>" for i in range(2000))
    parts = render(f"Вступление\n\n```bash\n{code}\n```\n\nКонец")
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 4000
        assert balanced(p), p[:200]
    assert all(p.count("<pre>") or "Вступление" in p or "Конец" in p for p in parts)
    joined = "".join(parts)
    assert "line 0 &lt;x&gt;" in joined and "line 1999 &lt;x&gt;" in joined


def test_split_long_paragraph():
    text = ("слово **жирно** " * 800).strip()
    parts = render(text)
    assert len(parts) > 1
    assert all(len(p) <= 4000 and balanced(p) for p in parts)


def test_split_does_not_break_entities():
    parts = split_html("&amp;" * 2000, 100)
    assert all(re.fullmatch(r"(&amp;)+", p) for p in parts)


def test_many_small_blocks_packed():
    parts = render("\n\n".join(f"абзац {i}" for i in range(1000)))
    assert all(len(p) <= 4000 for p in parts)
    assert "абзац 999" in parts[-1]
