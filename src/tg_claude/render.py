"""Markdown (как пишет Claude) -> HTML-подмножество Telegram + нарезка на сообщения.

Telegram понимает только: b, i, u, s, code, pre, a, blockquote (+expandable), tg-spoiler.
Всё остальное превращаем в текст, таблицы — в моноширинный блок.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
import re
import unicodedata

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

LIMIT = 4000  # у Telegram 4096 символов, оставляем запас

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])
_SAFE_SCHEMES = ("http://", "https://", "tg://", "mailto:")


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def md_to_html(text: str) -> str:
    root = SyntaxTreeNode(_md.parse(text))
    blocks = [_block(child, quote_depth=0) for child in root.children]
    return "\n\n".join(b for b in blocks if b.strip())


def render(text: str, limit: int = LIMIT) -> list[str]:
    """Markdown -> список HTML-сообщений, каждое не длиннее limit и со сбалансированными тегами."""
    root = SyntaxTreeNode(_md.parse(text))
    blocks = [b for b in (_block(c, quote_depth=0) for c in root.children) if b.strip()]
    return pack(blocks, limit)


def pack(blocks: list[str], limit: int = LIMIT, sep: str = "\n\n") -> list[str]:
    out: list[str] = []
    cur = ""
    for block in blocks:
        pieces = split_html(block, limit) if len(block) > limit else [block]
        for piece in pieces:
            if not cur:
                cur = piece
            elif len(cur) + len(sep) + len(piece) <= limit:
                cur += sep + piece
            else:
                out.append(cur)
                cur = piece
    if cur:
        out.append(cur)
    return out


# ---------- блоки ----------

def _block(node: SyntaxTreeNode, quote_depth: int, list_depth: int = 0) -> str:
    t = node.type
    if t == "paragraph":
        return _inline_children(node)
    if t == "heading":
        return f"<b>{_inline_children(node)}</b>"
    if t in ("bullet_list", "ordered_list"):
        return _list(node, quote_depth, list_depth)
    if t == "blockquote":
        inner = "\n".join(_block(c, quote_depth + 1, list_depth) for c in node.children)
        # вложенные цитаты Telegram не поддерживает — оставляем одну обёртку
        return inner if quote_depth else f"<blockquote>{inner}</blockquote>"
    if t in ("fence", "code_block"):
        return code_block(node.content, (node.info or "").split(" ")[0] if t == "fence" else "")
    if t == "hr":
        return "———"
    if t == "table":
        return _table(node)
    if t == "html_block":
        return esc(node.content.rstrip("\n"))
    if t == "inline":
        return _inline(node)
    return "\n".join(_block(c, quote_depth, list_depth) for c in node.children)


def code_block(code: str, lang: str = "") -> str:
    code = esc(code.rstrip("\n"))
    lang = re.sub(r"[^\w+#-]", "", lang or "")
    if lang:
        return f'<pre><code class="language-{lang}">{code}</code></pre>'
    return f"<pre>{code}</pre>"


def _list(node: SyntaxTreeNode, quote_depth: int, list_depth: int) -> str:
    ordered = node.type == "ordered_list"
    start = int(node.attrs.get("start", 1)) if ordered else 1
    indent = "   " * list_depth
    lines = []
    for i, item in enumerate(node.children):
        marker = f"{start + i}." if ordered else "•"
        parts = [_block(c, quote_depth, list_depth + 1) for c in item.children]
        body = "\n".join(p for p in parts if p)
        # продолжение пункта выравниваем под текст, вложенные списки уже с отступом
        body_lines = body.split("\n")
        first, rest = body_lines[0], body_lines[1:]
        rest = [ln if ln.startswith(indent + "   ") else indent + "   " + ln for ln in rest]
        lines.append("\n".join([f"{indent}{marker} {first}", *rest]))
    return "\n".join(lines)


def _table(node: SyntaxTreeNode) -> str:
    rows: list[list[str]] = []
    header_rows = 0
    for section in node.children:
        for tr in section.children:
            rows.append([_plain(cell) for cell in tr.children])
            if section.type == "thead":
                header_rows += 1
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(_width(r[c]) for r in rows) for c in range(ncols)]

    def fmt(r: list[str]) -> str:
        return " │ ".join(cell + " " * (widths[c] - _width(cell)) for c, cell in enumerate(r)).rstrip()

    lines = [fmt(r) for r in rows]
    if header_rows:
        lines.insert(header_rows, "─┼─".join("─" * w for w in widths))
    return f"<pre>{esc(chr(10).join(lines))}</pre>"


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _plain(node: SyntaxTreeNode) -> str:
    """Текст узла без разметки (для таблиц)."""
    if node.type in ("text", "code_inline", "html_inline"):
        return node.content
    if node.type in ("softbreak", "hardbreak"):
        return " "
    return "".join(_plain(c) for c in node.children)


# ---------- инлайн ----------

def _inline_children(node: SyntaxTreeNode) -> str:
    return "".join(_inline(c) for c in node.children)


def _inline(node: SyntaxTreeNode) -> str:
    t = node.type
    if t == "inline":
        return _inline_children(node)
    if t == "text":
        return esc(node.content)
    if t in ("softbreak", "hardbreak"):
        return "\n"
    if t == "code_inline":
        return f"<code>{esc(node.content)}</code>"
    if t == "strong":
        return f"<b>{_inline_children(node)}</b>"
    if t == "em":
        return f"<i>{_inline_children(node)}</i>"
    if t == "s":
        return f"<s>{_inline_children(node)}</s>"
    if t == "link":
        href = str(node.attrs.get("href", ""))
        inner = _inline_children(node)
        if href.startswith(_SAFE_SCHEMES):
            return f'<a href="{html.escape(href, quote=True)}">{inner}</a>'
        return inner
    if t == "image":
        src = str(node.attrs.get("src", ""))
        alt = _plain(node) or "image"
        if src.startswith(_SAFE_SCHEMES):
            return f'<a href="{html.escape(src, quote=True)}">🖼 {esc(alt)}</a>'
        return esc(alt)
    if t == "html_inline":
        return esc(node.content)
    return _inline_children(node)


# ---------- нарезка длинного HTML с сохранением баланса тегов ----------

_TOKEN = re.compile(r"<[^>]+>|[^<]+")
_TAG = re.compile(r"<(/?)([a-z-]+)")


def split_html(text: str, limit: int = LIMIT) -> list[str]:
    chunks: list[str] = []
    stack: list[tuple[str, str]] = []  # (имя тега, открывающий тег целиком)
    cur = ""

    def closing() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(stack))

    def flush() -> None:
        nonlocal cur
        chunks.append(cur + closing())
        cur = "".join(tag for _, tag in stack)

    for tok in _TOKEN.findall(text):
        if tok.startswith("<"):
            m = _TAG.match(tok)
            if not m:
                continue
            if len(cur) + len(tok) + len(closing()) + 16 > limit and cur.strip():
                flush()
            if m.group(1):
                if stack and stack[-1][0] == m.group(2):
                    stack.pop()
            else:
                stack.append((m.group(2), tok))
            cur += tok
            continue
        while tok:
            room = limit - len(cur) - len(closing())
            if len(tok) <= room:
                cur += tok
                break
            cut = _cut_point(tok, max(room, 0))
            if cut == 0:
                if cur.strip("\n") != "".join(t for _, t in stack):
                    flush()
                    continue
                cut = max(room, 1)  # пустой чанк не помещает даже кусок — режем как есть
                cut = _not_in_entity(tok, cut)
            cur += tok[:cut]
            tok = tok[cut:].lstrip("\n") if tok[cut - 1 : cut] == "\n" else tok[cut:]
            flush()
    if cur.strip() and cur != "".join(t for _, t in stack):
        chunks.append(cur + closing())
    return [c for c in chunks if _visible(c)]


def _cut_point(tok: str, room: int) -> int:
    if room <= 0:
        return 0
    window = tok[:room]
    for sep in ("\n\n", "\n", " "):
        i = window.rfind(sep)
        if i > room // 3:
            return i + len(sep)
    return _not_in_entity(tok, room) if room > 40 else 0


def _not_in_entity(tok: str, cut: int) -> int:
    amp = tok.rfind("&", max(0, cut - 8), cut)
    if amp != -1 and ";" not in tok[amp:cut]:
        return amp or cut
    return cut


def _visible(chunk: str) -> bool:
    return bool(re.sub(r"<[^>]+>", "", chunk).strip())


# ---------- для Mattermost: наш HTML -> Markdown, нарезка Markdown ----------

_MD_SPECIAL = re.compile(r"([\\`*_~\[\]])")
_URL = re.compile(r"(https?://\S+)")
_INLINE = {"b": "**", "strong": "**", "i": "*", "em": "*", "s": "~~", "strike": "~~", "del": "~~"}


class _HtmlToMd(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bufs: list[list[str]] = [[]]  # blockquote собирает содержимое в отдельный буфер
        self.pre = False
        self.fence_at = -1  # индекс открывающего ``` в текущем буфере — туда допишем язык
        self.code = False
        self.hrefs: list[str] = []
        self.need_nl = False

    def _out(self, s: str) -> None:
        if self.need_nl and s and not s.startswith("\n"):
            s = "\n" + s
        self.need_nl = False
        self.bufs[-1].append(s)

    def _ends_with_nl(self) -> bool:
        text = "".join(self.bufs[-1])
        return not text or text.endswith("\n")

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if self.pre:
            lang = (a.get("class") or "").removeprefix("language-")
            if tag == "code" and lang and self.fence_at >= 0:
                self.bufs[-1][self.fence_at] = "```" + lang
            return
        if tag in _INLINE:
            self._out(_INLINE[tag])
        elif tag == "code":
            self.code = True
            self._out("`")
        elif tag == "pre":
            if not self._ends_with_nl():
                self._out("\n")
            self._out("```")
            self.fence_at = len(self.bufs[-1]) - 1
            self._out("\n")
            self.pre = True
        elif tag == "a":
            self.hrefs.append(a.get("href") or "")
            self._out("[")
        elif tag == "blockquote":
            self.bufs.append([])

    def handle_endtag(self, tag: str) -> None:
        if self.pre:
            if tag == "pre":
                if not self._ends_with_nl():
                    self._out("\n")
                self._out("```")
                self.pre, self.fence_at, self.need_nl = False, -1, True
            return
        if tag in _INLINE:
            self._out(_INLINE[tag])
        elif tag == "code":
            self.code = False
            self._out("`")
        elif tag == "a":
            self._out(f"]({self.hrefs.pop() if self.hrefs else ''})")
        elif tag == "blockquote" and len(self.bufs) > 1:
            inner = "".join(self.bufs.pop()).strip("\n")
            if not self._ends_with_nl():
                self._out("\n")
            self._out("\n".join(f"> {ln}" for ln in inner.split("\n")))
            self.need_nl = True

    def handle_data(self, data: str) -> None:
        if self.pre or self.code:
            self._out(data)
            return
        # спецсимволы Markdown в обычном тексте экранируем (кроме ссылок — иначе сломаются)
        parts = _URL.split(data)
        self._out("".join(p if i % 2 else _MD_SPECIAL.sub(r"\\\1", p) for i, p in enumerate(parts)))


def html_to_md(text: str) -> str:
    """HTML-подмножество Telegram (как пишет бот) -> Markdown для Mattermost."""
    parser = _HtmlToMd()
    parser.feed(text)
    parser.close()
    while len(parser.bufs) > 1:  # незакрытая цитата
        inner = "".join(parser.bufs.pop())
        parser.bufs[-1].append(inner)
    return "".join(parser.bufs[0]).strip("\n")


def split_md(text: str, limit: int) -> list[str]:
    """Режет Markdown по строкам под лимит; блок кода на границе закрывается и открывается заново."""
    out: list[str] = []
    cur: list[str] = []
    size = 0
    fence: str | None = None
    lines: list[str] = []
    for line in text.split("\n"):  # слишком длинные строки режем как есть
        while len(line) > limit - 10:
            lines.append(line[: limit - 10])
            line = line[limit - 10 :]
        lines.append(line)
    for line in lines:
        if cur and size + len(line) + 1 + 4 > limit:
            if fence is not None:
                cur.append("```")
            out.append("\n".join(cur).strip("\n"))
            cur = [fence] if fence is not None else []
            size = len(fence) + 1 if fence is not None else 0
        cur.append(line)
        size += len(line) + 1
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.strip()
    if cur:
        out.append("\n".join(cur).strip("\n"))
    return [c for c in out if c.strip()]
