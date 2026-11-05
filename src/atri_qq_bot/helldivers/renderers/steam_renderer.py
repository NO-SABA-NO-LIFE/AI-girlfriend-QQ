"""Pillow renderer for image-14-style paginated Steam announcement cards.

Input is deliberately integration-friendly: pass a Steam update mapping, optionally a
pre-parsed content model, translated title/content or blocks, and a local hero image.
Remote access is outside this module; callers can use :mod:`remote_image_cache` and
feed the resulting path through ``hero_image``.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .visual_assets import prune_render_outputs
from typing import Any
from urllib.parse import urlsplit

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

try:
    from ..core.steam_parser import ParsedSteamContent, SteamBlock, parse_steam_content
except ImportError:  # pragma: no cover - supports direct module loading in smoke tests
    from steam_parser import ParsedSteamContent, SteamBlock, parse_steam_content

WIDTH = 1060
MAX_HEIGHT = 1600
MIN_HEIGHT = 900
MARGIN = 28
HERO_HEIGHT = 315
HEADER_GAP = 26
CONTENT_PAD = 42
FOOTER_HEIGHT = 92

BG = (13, 18, 25)
CARD = (24, 31, 40)
CARD_ALT = (29, 38, 49)
TEXT = (238, 243, 248)
MUTED = (146, 164, 181)
STEAM_BLUE = (88, 176, 228)
STEAM_DARK_BLUE = (22, 71, 105)
DIVIDER = (55, 69, 83)
BULLET = (111, 191, 238)
_FONT_CACHE: dict[tuple[int, bool], Any] = {}


def _safe_block_level(value: Any) -> int:
    """块层级理论上应为整数；上游脏数据（如 "h2"）按 0 处理而非整卡失败。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class _RenderUnit:
    kind: str
    lines: tuple[str, ...]
    height: int
    level: int = 0
    ordered: bool = False
    index: int = 0


def _font(size: int, *, bold: bool = False):
    if ImageFont is None:
        return None
    key = (max(1, int(size)), bool(bold))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    weight = "Bold" if bold else "Regular"
    candidates = (
        f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc",
        f"/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-{weight}.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    )
    selected = None
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                selected = ImageFont.truetype(candidate, size=key[0])
                break
        except (OSError, ValueError):
            continue
    if selected is None:
        selected = ImageFont.load_default(size=key[0])
    _FONT_CACHE[key] = selected
    return selected


def _text_width(draw: Any, text: str, font: Any) -> float:
    try:
        return float(draw.textlength(str(text), font=font))
    except (AttributeError, TypeError, ValueError):
        return len(str(text)) * 8.0


def _wrap(
    draw: Any, text: str, font: Any, max_width: int, *, max_lines: int | None = None
) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text or "").replace("\r", "").split("\n"):
        cleaned = " ".join(raw_line.split())
        if not cleaned:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        current = ""
        for character in cleaned:
            candidate = current + character
            if not current or _text_width(draw, candidate, font) <= max_width:
                current = candidate
            else:
                lines.append(current.rstrip())
                current = character.lstrip()
                if max_lines is not None and len(lines) >= max_lines:
                    break
        if max_lines is not None and len(lines) >= max_lines:
            break
        if current:
            lines.append(current.rstrip())
        if max_lines is not None and len(lines) >= max_lines:
            break
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines or [""]


def _ellipsize(draw: Any, text: str, font: Any, max_width: int) -> str:
    value = " ".join(str(text or "").split())
    if _text_width(draw, value, font) <= max_width:
        return value
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if _text_width(draw, value[:middle].rstrip() + "…", font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip() + "…"


def _published(value: Any) -> str:
    if value is None or value == "":
        return "发布日期未知"
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return (
                datetime.fromtimestamp(number, timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
        except (OSError, OverflowError, ValueError):
            return str(value)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return (
        parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


def _load_image(value: Any):
    if Image is None or value is None:
        return None
    if hasattr(value, "convert") and hasattr(value, "size"):
        try:
            return value.convert("RGBA")
        except (OSError, ValueError):
            return None
    try:
        path = Path(value).expanduser()
    except TypeError:
        return None
    if not path.is_file():
        return None
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            image.load()
        return image
    except (OSError, ValueError):
        return None


def _visual_asset_image(visual_assets: Any | None):
    if Image is None:
        return None
    direct = _load_image(visual_assets)
    if direct is not None:
        return direct
    if isinstance(visual_assets, Mapping):
        for key in (
            "steam_header",
            "header_image",
            "hero",
            "fallback",
            "super_earth_flag",
        ):
            direct = _load_image(visual_assets.get(key))
            if direct is not None:
                return direct
    modules: list[Any] = []
    if visual_assets is not None and not isinstance(visual_assets, Mapping):
        modules.append(visual_assets)
    package = __package__
    for module_name in ([f"{package}.visual_assets"] if package else []) + [
        "visual_assets"
    ]:
        try:
            modules.append(importlib.import_module(module_name))
        except (ImportError, ValueError):
            continue
    for module in modules:
        for function_name in ("asset_image", "get_asset"):
            function = getattr(module, function_name, None)
            if not callable(function):
                continue
            for asset_name in (
                "user/steam_hero.png",
                "global_events/super_earth_flag.png",
                "global_events/super_earth_flag",
                "emblems/super_earth.png",
                "emblems/super_earth",
            ):
                try:
                    candidate = function(asset_name)
                except (OSError, TypeError, ValueError):
                    continue
                direct = _load_image(candidate)
                if direct is not None:
                    return direct
    return None


def _fallback_hero(size: tuple[int, int]):
    width, height = size
    image = Image.new("RGBA", size, (14, 38, 56, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    for y in range(height):
        amount = y / max(1, height - 1)
        draw.line(
            (0, y, width, y),
            fill=(
                int(18 + 14 * amount),
                int(66 - 25 * amount),
                int(96 - 26 * amount),
                255,
            ),
        )
    for x in range(-height, width, 96):
        draw.polygon(
            ((x, 0), (x + 58, 0), (x + height + 58, height), (x + height, height)),
            fill=(117, 195, 235, 18),
        )
    cx, cy = width - 180, height // 2
    draw.ellipse(
        (cx - 82, cy - 82, cx + 82, cy + 82), outline=(180, 226, 248, 180), width=5
    )
    draw.line((cx - 58, cy, cx + 58, cy), fill=(180, 226, 248, 180), width=4)
    draw.line((cx, cy - 58, cx, cy + 58), fill=(180, 226, 248, 180), width=4)
    font = _font(48, bold=True)
    draw.text(
        (56, height // 2 - 42), "HELLDIVERS 2", font=font, fill=(234, 245, 251, 240)
    )
    return image


def _hero(source: Any | None, size: tuple[int, int]):
    source = source if source is not None else _fallback_hero(size)
    fitted = ImageOps.fit(
        source.convert("RGBA"),
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    for y in range(size[1]):
        alpha = int(170 * (y / max(1, size[1] - 1)) ** 2)
        draw.line((0, y, size[0], y), fill=(5, 11, 17, alpha))
    return Image.alpha_composite(fitted, overlay)


def _coerce_blocks(value: Any) -> tuple[SteamBlock, ...]:
    if isinstance(value, ParsedSteamContent):
        return value.blocks
    if isinstance(value, Mapping):
        value = value.get("blocks") or []
    blocks: list[SteamBlock] = []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, SteamBlock):
                blocks.append(item)
            elif isinstance(item, Mapping):
                kind = str(item.get("kind") or item.get("type") or "paragraph").lower()
                if kind not in {"heading", "paragraph", "list"}:
                    continue
                blocks.append(
                    SteamBlock(
                        kind,  # type: ignore[arg-type]
                        text=str(item.get("text") or ""),
                        level=max(
                            0,
                            _safe_block_level(item.get("level")),
                        ),
                        items=tuple(
                            str(part)
                            for part in (item.get("items") or [])
                            if str(part).strip()
                        ),
                        ordered=bool(item.get("ordered")),
                    )
                )
    return tuple(blocks)


def _render_units(
    blocks: Iterable[SteamBlock], draw: Any, content_width: int
) -> list[_RenderUnit]:
    heading_fonts = {
        1: _font(31, bold=True),
        2: _font(27, bold=True),
        3: _font(23, bold=True),
    }
    paragraph_font = _font(21)
    units: list[_RenderUnit] = []
    for block in blocks:
        if block.kind == "heading":
            level = max(1, min(3, int(block.level or 2)))
            lines = tuple(
                _wrap(
                    draw, block.text, heading_fonts[level], content_width, max_lines=4
                )
            )
            units.append(
                _RenderUnit(
                    "heading",
                    lines,
                    22 + len(lines) * (43 if level == 1 else 38),
                    level=level,
                )
            )
        elif block.kind == "paragraph" and block.text:
            lines = _wrap(draw, block.text, paragraph_font, content_width)
            for start in range(0, len(lines), 10):
                chunk = tuple(lines[start : start + 10])
                units.append(_RenderUnit("paragraph", chunk, len(chunk) * 32 + 18))
        elif block.kind == "list":
            for index, item in enumerate(block.items, 1):
                marker_width = 38 + max(0, block.level) * 24
                lines = tuple(
                    _wrap(draw, item, paragraph_font, content_width - marker_width)
                )
                units.append(
                    _RenderUnit(
                        "list_item",
                        lines,
                        len(lines) * 31 + 12,
                        level=max(0, block.level),
                        ordered=block.ordered,
                        index=index,
                    )
                )
    if not units:
        units.append(_RenderUnit("paragraph", ("（公告正文为空）",), 50))
    return units


def _paginate(units: list[_RenderUnit], capacity: int) -> list[list[_RenderUnit]]:
    expanded: list[_RenderUnit] = []
    for unit in units:
        if unit.height <= capacity or unit.kind == "heading":
            expanded.append(unit)
            continue
        line_height = 32 if unit.kind == "paragraph" else 31
        padding = 18 if unit.kind == "paragraph" else 12
        lines_per_chunk = max(1, (capacity - padding) // line_height)
        for start in range(0, len(unit.lines), lines_per_chunk):
            lines = unit.lines[start : start + lines_per_chunk]
            expanded.append(
                _RenderUnit(
                    unit.kind,
                    lines,
                    len(lines) * line_height + padding,
                    level=unit.level,
                    ordered=unit.ordered,
                    index=unit.index if start == 0 else 0,
                )
            )

    pages: list[list[_RenderUnit]] = []
    current: list[_RenderUnit] = []
    used = 0
    for unit in expanded:
        if current and used + unit.height > capacity:
            pages.append(current)
            current = []
            used = 0
        current.append(unit)
        used += unit.height
    if current:
        pages.append(current)
    return pages or [[]]


def _write_png_atomic(image: Any, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=".tmp.png",
            delete=False,
        ) as handle:
            temporary = handle.name
        image.save(temporary, format="PNG", optimize=True)
        with open(temporary, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        prune_render_outputs(Path(destination).parent)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return destination


def render_steam_news(
    update: Mapping[str, Any],
    output_dir: str | Path,
    *,
    parsed: ParsedSteamContent | Mapping[str, Any] | Sequence[Any] | None = None,
    title: str | None = None,
    content: str | None = None,
    translated_blocks: ParsedSteamContent
    | Mapping[str, Any]
    | Sequence[Any]
    | None = None,
    hero_image: Any | None = None,
    visual_assets: Any | None = None,
    max_height: int = MAX_HEIGHT,
    width: int = WIDTH,
    filename_prefix: str | None = None,
) -> list[Path]:
    """Render a Steam update into one or more bounded-height PNG cards."""

    if Image is None or ImageDraw is None or ImageOps is None:
        raise RuntimeError("Pillow is required to render Steam news")
    if not isinstance(update, Mapping):
        raise TypeError("update must be a mapping")
    width = max(820, int(width))
    max_height = max(MIN_HEIGHT, int(max_height))
    raw_content = str(
        content
        if content is not None
        else update.get("content") or update.get("contents") or ""
    )
    parsed_model = parsed if parsed is not None else parse_steam_content(raw_content)
    blocks = _coerce_blocks(translated_blocks) or _coerce_blocks(parsed_model)
    if not blocks and raw_content:
        blocks = parse_steam_content(raw_content).blocks

    headline = " ".join(
        str(
            title if title is not None else update.get("title") or "Steam Update"
        ).split()
    )
    author = " ".join(str(update.get("author") or "Arrowhead Game Studios").split())
    published = _published(
        update.get("publishedAt") or update.get("date") or update.get("published")
    )
    tags_value = update.get("tags") or []
    tags = (
        [str(tag).strip() for tag in tags_value if str(tag).strip()]
        if isinstance(tags_value, list)
        else []
    )
    source_url = str(update.get("url") or update.get("source_url") or "").strip()

    probe = Image.new("RGB", (10, 10))
    probe_draw = ImageDraw.Draw(probe)
    content_width = width - MARGIN * 2 - CONTENT_PAD * 2
    units = _render_units(blocks, probe_draw, content_width)
    title_font = _font(39, bold=True)
    title_lines = _wrap(probe_draw, headline, title_font, content_width, max_lines=3)
    title_height = len(title_lines) * 50
    metadata_height = 75
    fixed_height = (
        MARGIN * 2
        + HERO_HEIGHT
        + HEADER_GAP
        + 50
        + title_height
        + metadata_height
        + FOOTER_HEIGHT
    )
    content_capacity = max(240, max_height - fixed_height)
    pages = _paginate(units, content_capacity)

    local_hero = _load_image(hero_image)
    if local_hero is None:
        for key in ("hero_path", "image_path", "header_image_path", "cached_image"):
            local_hero = _load_image(update.get(key))
            if local_hero is not None:
                break
    if local_hero is None:
        local_hero = _visual_asset_image(visual_assets)

    identifier = str(update.get("id") or update.get("gid") or uuid.uuid4().hex[:12])
    safe_id = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in identifier
    )
    prefix = filename_prefix or f"steam_{safe_id}"
    output_root = Path(output_dir)
    paths: list[Path] = []
    page_count = len(pages)

    try:
        for page_number, page_units in enumerate(pages, 1):
            used_content = sum(unit.height for unit in page_units)
            page_height = min(max_height, max(MIN_HEIGHT, fixed_height + used_content))
            image = Image.new("RGB", (width, page_height), BG)
            draw = ImageDraw.Draw(image)
            card_box = (MARGIN, MARGIN, width - MARGIN, page_height - MARGIN)
            draw.rounded_rectangle(
                card_box, radius=24, fill=CARD, outline=(48, 61, 75), width=2
            )

            hero = _hero(local_hero, (width - MARGIN * 2, HERO_HEIGHT))
            mask = Image.new("L", hero.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, hero.width, hero.height + 24), radius=24, fill=255
            )
            hero.putalpha(mask)
            image.paste(hero, (MARGIN, MARGIN), hero)
            hero.close()

            left = MARGIN + CONTENT_PAD
            right = width - MARGIN - CONTENT_PAD
            y = MARGIN + HERO_HEIGHT + 22
            badge_font = _font(16, bold=True)
            badge = "绝地潜兵 2  ·  STEAM 更新"
            badge_width = int(_text_width(draw, badge, badge_font)) + 28
            draw.rounded_rectangle(
                (left, y, left + badge_width, y + 34), radius=17, fill=STEAM_DARK_BLUE
            )
            draw.text((left + 14, y + 7), badge, font=badge_font, fill=(222, 242, 253))
            page_label = f"{page_number}/{page_count}"
            page_width = _text_width(draw, page_label, badge_font)
            draw.text(
                (right - page_width, y + 7), page_label, font=badge_font, fill=MUTED
            )
            y += 48

            for line in title_lines:
                draw.text((left, y), line, font=title_font, fill=TEXT)
                y += 50
            metadata_font = _font(17)
            meta = f"{author}   ·   {published}"
            draw.text(
                (left, y + 4),
                _ellipsize(draw, meta, metadata_font, content_width),
                font=metadata_font,
                fill=MUTED,
            )
            y += 34
            if tags:
                tag_text = "  ".join(f"#{tag}" for tag in tags[:6])
                draw.text(
                    (left, y),
                    _ellipsize(draw, tag_text, metadata_font, content_width),
                    font=metadata_font,
                    fill=STEAM_BLUE,
                )
                y += 31
            else:
                y += 8
            draw.line((left, y, right, y), fill=DIVIDER, width=2)
            y += 20

            paragraph_font = _font(21)
            heading_fonts = {
                1: _font(31, bold=True),
                2: _font(27, bold=True),
                3: _font(23, bold=True),
            }
            for unit in page_units:
                if unit.kind == "heading":
                    y += 8
                    font = heading_fonts[max(1, min(3, unit.level))]
                    for line in unit.lines:
                        draw.text((left, y), line, font=font, fill=STEAM_BLUE)
                        y += 43 if unit.level == 1 else 38
                    y += 10
                elif unit.kind == "paragraph":
                    for line in unit.lines:
                        draw.text((left, y), line, font=paragraph_font, fill=TEXT)
                        y += 32
                    y += 18
                elif unit.kind == "list_item":
                    indent = unit.level * 24
                    marker = (
                        f"{unit.index}."
                        if unit.ordered and unit.index > 0
                        else ("•" if unit.index > 0 else "")
                    )
                    marker_x = left + indent
                    draw.text((marker_x, y), marker, font=paragraph_font, fill=BULLET)
                    text_x = marker_x + 34
                    for line_index, line in enumerate(unit.lines):
                        draw.text((text_x, y), line, font=paragraph_font, fill=TEXT)
                        y += 31
                        if line_index == 0:
                            marker = ""
                    y += 12

            footer_top = page_height - MARGIN - FOOTER_HEIGHT + 20
            draw.line((left, footer_top, right, footer_top), fill=DIVIDER, width=1)
            footer_font = _font(15)
            source_label = source_url or "Steam Community announcement"
            if source_url:
                host = urlsplit(source_url).hostname or "Steam"
                source_label = f"原文 · {host} · {source_url}"
            footer_lines = _wrap(
                draw, source_label, footer_font, content_width, max_lines=2
            )
            fy = footer_top + 14
            for line in footer_lines:
                draw.text((left, fy), line, font=footer_font, fill=MUTED)
                fy += 22

            destination = output_root / f"{Path(prefix).name}_p{page_number:02d}.png"
            try:
                paths.append(_write_png_atomic(image, destination))
            finally:
                image.close()
    finally:
        probe.close()
        if local_hero is not None:
            local_hero.close()
    return paths


class SteamRenderer:
    """Directory-oriented wrapper for paginated Steam news rendering."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        width: int = WIDTH,
        max_height: int = MAX_HEIGHT,
        visual_assets: Any | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.width = max(820, int(width))
        self.max_height = max(MIN_HEIGHT, int(max_height))
        self.visual_assets = visual_assets

    @property
    def available(self) -> bool:
        return Image is not None

    def render(
        self,
        update: Mapping[str, Any],
        *,
        parsed: ParsedSteamContent | Mapping[str, Any] | Sequence[Any] | None = None,
        title: str | None = None,
        content: str | None = None,
        translated_blocks: ParsedSteamContent
        | Mapping[str, Any]
        | Sequence[Any]
        | None = None,
        hero_image: Any | None = None,
        filename_prefix: str | None = None,
    ) -> list[Path]:
        return render_steam_news(
            update,
            self.output_dir,
            parsed=parsed,
            title=title,
            content=content,
            translated_blocks=translated_blocks,
            hero_image=hero_image,
            visual_assets=self.visual_assets,
            max_height=self.max_height,
            width=self.width,
            filename_prefix=filename_prefix,
        )


__all__ = [
    "MAX_HEIGHT",
    "SteamRenderer",
    "WIDTH",
    "render_steam_news",
]
