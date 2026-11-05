"""Safe, browser-free normalization of Steam announcement BBCode and HTML.

The parser intentionally produces a small display model: headings, paragraphs, and
lists. Inline markup is reduced to readable text, unknown tags are ignored, HTML
entities are unescaped, and only explicitly allowed Steam CDN image URLs are exposed.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

STEAM_IMAGE_HOSTS = frozenset(
    {
        "clan.steamstatic.com",
        "clan.akamai.steamstatic.com",
        "shared.akamai.steamstatic.com",
        "store.akamai.steamstatic.com",
        "cdn.akamai.steamstatic.com",
        "shared.steamstatic.com",
    }
)

BlockKind = Literal["heading", "paragraph", "list"]
_WHITESPACE_RE = re.compile(r"[\t\f\v ]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")
_BBCODE_IMAGE_RE = re.compile(
    r"\[img(?:=[^\]]*)?\](.*?)\[/img\]", re.IGNORECASE | re.DOTALL
)
_BBCODE_TAG_RE = re.compile(
    r"\[/?[a-z][a-z0-9]*(?:[=\s][^\]]*)?\]",
    re.IGNORECASE,
)
_STEAM_CLAN_TOKEN_RE = re.compile(r"^\{STEAM_CLAN_IMAGE\}", re.IGNORECASE)
_IMAGE_PATH_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)(?:$|[?#])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SteamBlock:
    """A normalized display block.

    ``text`` is populated for headings and paragraphs. ``items`` is populated for
    lists. Heading levels are clamped to 1..3; list levels start at zero.
    """

    kind: BlockKind
    text: str = ""
    level: int = 0
    items: tuple[str, ...] = ()
    ordered: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "type": self.kind,
            "text": self.text,
            "level": self.level,
            "items": list(self.items),
            "ordered": self.ordered,
        }


@dataclass(frozen=True, slots=True)
class ParsedSteamContent:
    """Normalized Steam content and validated image candidates."""

    blocks: tuple[SteamBlock, ...]
    image_candidates: tuple[str, ...]

    @property
    def plain_text(self) -> str:
        parts: list[str] = []
        for block in self.blocks:
            if block.kind == "list":
                parts.extend(f"- {item}" for item in block.items)
            elif block.text:
                parts.append(block.text)
        return "\n\n".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {
            "blocks": [block.as_dict() for block in self.blocks],
            "image_candidates": list(self.image_candidates),
            "plain_text": self.plain_text,
        }


def normalize_steam_image_url(value: str) -> str | None:
    """Return a canonical allowed HTTPS Steam image URL, otherwise ``None``."""

    candidate = html.unescape(str(value or "")).strip().strip("\"'")
    candidate = _STEAM_CLAN_TOKEN_RE.sub(
        "https://clan.akamai.steamstatic.com",
        candidate,
    )
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".").lower()
    if (
        parts.scheme.lower() != "https"
        or host not in STEAM_IMAGE_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or not parts.path.startswith("/")
        or "\x00" in candidate
    ):
        return None
    if not _IMAGE_PATH_RE.search(parts.path):
        return None
    netloc = host if parts.port is None else f"{host}:443"
    return urlunsplit(("https", netloc, parts.path, parts.query, ""))


def is_official_steam_image_url(value: str) -> bool:
    """Whether ``value`` is an explicitly allowed Steam CDN image URL."""

    return normalize_steam_image_url(value) is not None


def extract_steam_image_candidates(content: str) -> tuple[str, ...]:
    """Extract de-duplicated official image candidates without rendering HTML."""

    collector = _SteamHTMLCollector()
    collector.feed(_bbcode_to_html(str(content or ""), collector.add_image))
    collector.close()
    return tuple(collector.images)


def parse_steam_content(content: str, *, max_blocks: int = 512) -> ParsedSteamContent:
    """Parse Steam BBCode/HTML into a small normalized content model.

    The input is never executed and no network or browser is used. ``max_blocks``
    provides a deterministic upper bound for hostile or unexpectedly large input.
    """

    collector = _SteamHTMLCollector(max_blocks=max(1, int(max_blocks)))
    converted = _bbcode_to_html(str(content or ""), collector.add_image)
    collector.feed(converted)
    collector.close()
    return ParsedSteamContent(tuple(collector.blocks), tuple(collector.images))


def parse_steam_bbcode_html(
    content: str, *, max_blocks: int = 512
) -> ParsedSteamContent:
    """Compatibility alias with an explicit name for integration code."""

    return parse_steam_content(content, max_blocks=max_blocks)


def _clean_text(value: str, *, preserve_newlines: bool = False) -> str:
    text = html.unescape(html.unescape(str(value or ""))).replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if preserve_newlines:
        lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
        return "\n".join(lines).strip()
    return " ".join(text.split())


def _bbcode_to_html(content: str, image_sink) -> str:
    text = html.unescape(str(content or "")).replace("\x00", "")

    def image_replacement(match: re.Match[str]) -> str:
        raw_url = _clean_text(match.group(1))
        image_sink(raw_url)
        return f'<img src="{html.escape(raw_url, quote=True)}">'

    text = _BBCODE_IMAGE_RE.sub(image_replacement, text)
    replacements = (
        (r"\[h1\]", "<h1>"),
        (r"\[/h1\]", "</h1>"),
        (r"\[h2\]", "<h2>"),
        (r"\[/h2\]", "</h2>"),
        (r"\[h3\]", "<h3>"),
        (r"\[/h3\]", "</h3>"),
        (r"\[p\]", "<p>"),
        (r"\[/p\]", "</p>"),
        (r"\[list(?:=[^\]]+)?\]", "<ul>"),
        (r"\[/list\]", "</ul>"),
        (r"\[olist(?:=[^\]]+)?\]", "<ol>"),
        (r"\[/olist\]", "</ol>"),
        (r"\[\*\]", "</li><li>"),
        (r"\[br\s*/?\]", "<br>"),
        (r"\[(?:b|strong)\]", "<strong>"),
        (r"\[/(?:b|strong)\]", "</strong>"),
        (r"\[(?:i|em)\]", "<em>"),
        (r"\[/(?:i|em)\]", "</em>"),
        (r"\[u\]", "<span>"),
        (r"\[/u\]", "</span>"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]",
        lambda match: (
            f'<a href="{html.escape(match.group(1).strip(), quote=True)}">'
            f"{match.group(2)}</a>"
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"\[url\](.*?)\[/url\]",
        lambda match: (
            f'<a href="{html.escape(match.group(1).strip(), quote=True)}">{match.group(1)}</a>'
        ),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"(<(?:ul|ol)>)\s*</li>", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>\s*(</(?:ul|ol)>)", r"</li>\1", text, flags=re.IGNORECASE)
    return _BBCODE_TAG_RE.sub("", text)


@dataclass(slots=True)
class _ListContext:
    ordered: bool
    level: int
    items: list[str]
    current: list[str]


class _SteamHTMLCollector(HTMLParser):
    _HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 3, "h5": 3, "h6": 3}
    _BREAK_TAGS = {"div", "section", "article", "blockquote", "pre", "tr"}

    def __init__(self, *, max_blocks: int = 512) -> None:
        super().__init__(convert_charrefs=True)
        self.max_blocks = max_blocks
        self.blocks: list[SteamBlock] = []
        self.images: list[str] = []
        self._image_seen: set[str] = set()
        self._paragraph: list[str] = []
        self._heading_level: int | None = None
        self._heading: list[str] = []
        self._lists: list[_ListContext] = []
        self._suppressed_depth = 0

    def add_image(self, value: str) -> None:
        normalized = normalize_steam_image_url(value)
        if normalized and normalized not in self._image_seen:
            self._image_seen.add(normalized)
            self.images.append(normalized)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "template", "noscript"}:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth:
            return
        attributes = {str(key).lower(): value for key, value in attrs}
        if tag == "img":
            self.add_image(attributes.get("src") or "")
            return
        if tag in self._HEADINGS:
            self._flush_paragraph()
            self._heading_level = self._HEADINGS[tag]
            self._heading = []
            return
        if tag == "p":
            self._flush_paragraph()
            return
        if tag in {"ul", "ol"}:
            self._flush_paragraph()
            self._lists.append(
                _ListContext(
                    ordered=tag == "ol",
                    level=len(self._lists),
                    items=[],
                    current=[],
                )
            )
            return
        if tag == "li" and self._lists:
            self._flush_list_item(self._lists[-1])
            return
        if tag == "br":
            self._append_text("\n")
        elif tag in self._BREAK_TAGS:
            self._append_text("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "template", "noscript"}:
            self._suppressed_depth = max(0, self._suppressed_depth - 1)
            return
        if self._suppressed_depth:
            return
        if tag in self._HEADINGS and self._heading_level is not None:
            text = _clean_text("".join(self._heading))
            if text:
                self._add_block(
                    SteamBlock("heading", text=text, level=self._heading_level)
                )
            self._heading_level = None
            self._heading = []
            return
        if tag == "p":
            self._flush_paragraph()
            return
        if tag == "li" and self._lists:
            self._flush_list_item(self._lists[-1])
            return
        if tag in {"ul", "ol"} and self._lists:
            context = self._lists.pop()
            self._flush_list_item(context)
            if context.items:
                self._add_block(
                    SteamBlock(
                        "list",
                        level=context.level,
                        items=tuple(context.items),
                        ordered=context.ordered,
                    )
                )
            return
        if tag in self._BREAK_TAGS:
            self._append_text("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self._append_text(data)

    def handle_entityref(
        self, name: str
    ) -> None:  # pragma: no cover - convert_charrefs handles this
        self._append_text(html.unescape(f"&{name};"))

    def handle_charref(
        self, name: str
    ) -> None:  # pragma: no cover - convert_charrefs handles this
        self._append_text(html.unescape(f"&#{name};"))

    def close(self) -> None:
        super().close()
        if self._heading_level is not None:
            text = _clean_text("".join(self._heading))
            if text:
                self._add_block(
                    SteamBlock("heading", text=text, level=self._heading_level)
                )
            self._heading_level = None
            self._heading = []
        while self._lists:
            context = self._lists.pop()
            self._flush_list_item(context)
            if context.items:
                self._add_block(
                    SteamBlock(
                        "list",
                        level=context.level,
                        items=tuple(context.items),
                        ordered=context.ordered,
                    )
                )
        self._flush_paragraph()

    def _append_text(self, value: str) -> None:
        if not value:
            return
        if self._heading_level is not None:
            self._heading.append(value)
        elif self._lists:
            self._lists[-1].current.append(value)
        else:
            self._paragraph.append(value)

    def _flush_list_item(self, context: _ListContext) -> None:
        text = _clean_text("".join(context.current), preserve_newlines=True)
        text = " ".join(part for part in text.splitlines() if part).strip()
        if text:
            context.items.append(text)
        context.current = []

    def _flush_paragraph(self) -> None:
        raw = _clean_text("".join(self._paragraph), preserve_newlines=True)
        self._paragraph = []
        if not raw:
            return
        for paragraph in _BLANK_LINES_RE.split(raw):
            text = " ".join(
                line.strip() for line in paragraph.splitlines() if line.strip()
            )
            text = _clean_text(text)
            if text:
                self._add_block(SteamBlock("paragraph", text=text))

    def _add_block(self, block: SteamBlock) -> None:
        if len(self.blocks) >= self.max_blocks:
            return
        if block.kind == "paragraph" and self.blocks:
            previous = self.blocks[-1]
            if previous.kind == "paragraph" and previous.text == block.text:
                return
        self.blocks.append(block)


__all__ = [
    "ParsedSteamContent",
    "STEAM_IMAGE_HOSTS",
    "SteamBlock",
    "extract_steam_image_candidates",
    "is_official_steam_image_url",
    "normalize_steam_image_url",
    "parse_steam_bbcode_html",
    "parse_steam_content",
]
