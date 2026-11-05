"""Dedicated Pillow renderer for normalized Helldivers warfront rows.

The renderer has no dependency on the plugin's API models.  Feed it rows such as::

    {
        "planet_name": "Heeth",
        "planet_index": 112,
        "players": 12345,
        "liberation": 72.4,
        "regen": 1.5,
        "recent_rate_pph": 2.3,
        "estimated_result": "Liberation in 11h 59m",
        "is_defense": False,
    }

Use :func:`render_warfront` for a one-shot render or :class:`WarfrontRenderer`
when an integration owns an output directory.  PNG writes are atomic.  If an
optional ``visual_assets`` module exists, a compatible faction emblem is used;
otherwise the renderer draws a deterministic geometric fallback.
"""

from __future__ import annotations

import importlib
import math
import os
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path

from .visual_assets import prune_render_outputs
from typing import Any

try:
    from ..core.i18n import format_planet
    from .hud_theme import (
        cockpit_backdrop,
        draw_chamfered_panel,
        draw_scanlines,
        draw_segmented_bar,
        draw_tech_header,
    )
except ImportError:  # pragma: no cover - direct module execution
    from i18n import format_planet  # type: ignore
    from hud_theme import (  # type: ignore
        cockpit_backdrop,
        draw_chamfered_panel,
        draw_scanlines,
        draw_segmented_bar,
        draw_tech_header,
    )

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - availability is exposed to callers
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

WIDTH = 1180
MARGIN_X = 30
TITLE_H = 100
TABLE_TITLE_H = 42
COLUMN_HEADER_H = 40
ROW_H = 39
SECTION_GAP = 24
BOTTOM_PAD = 28

BG = (12, 15, 22)
PANEL = (20, 24, 34)
PANEL_ALT = (24, 29, 40)
HEADER_BG = (28, 34, 47)
TEXT = (235, 239, 247)
TEXT_MUTED = (151, 163, 185)
GRID = (55, 64, 82)
SUCCESS = (94, 208, 135)
DANGER = (255, 112, 116)

FACTION_THEMES: dict[str, tuple[int, int, int]] = {
    "Humans": (95, 174, 245),
    "Super Earth": (95, 174, 245),
    "Terminids": (238, 184, 55),
    "Automaton": (238, 82, 80),
    "Illuminate": (166, 105, 232),
    "Unknown": (145, 153, 171),
}

# x positions are relative to the table content width (1120 px).
_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("排名", 64, "center"),
    ("星球", 244, "left"),
    ("玩家", 110, "right"),
    ("解放进度", 128, "right"),
    ("恢复率", 105, "right"),
    ("近期速率", 151, "right"),
    ("预计结果", 318, "right"),
)
_FONT_CACHE: dict[tuple[int, bool], Any] = {}


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _nested(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = row.get(key)
    return value if isinstance(value, Mapping) else {}


def _row_name(row: Mapping[str, Any]) -> str:
    planet = _nested(row, "planet")
    value = _first(row, "planet_name", "name")
    if value is None:
        value = _first(planet, "name", "planetName")
    return format_planet(value, "未知星球")


def _row_players(row: Mapping[str, Any]) -> int:
    value = _first(row, "players", "player_count", "playerCount")
    if value is None:
        stats = _nested(_nested(row, "planet"), "statistics")
        value = stats.get("playerCount")
    return max(0, int(_finite_float(value)))


def _row_liberation(row: Mapping[str, Any]) -> float:
    value = _first(
        row,
        "liberation",
        "liberation_pct",
        "progress",
        "progress_pct",
        "percentage",
    )
    if value is not None:
        return max(0.0, min(100.0, _finite_float(value)))
    planet = _nested(row, "planet")
    candidates: list[float] = []
    # API 改版后星球主战场恒满血，真实进度在 planet.regions 数组
    if planet:
        regions = planet.get("regions")
        if isinstance(regions, list) and regions:
            total_max = 0.0
            total_health = 0.0
            for region in regions:
                if not isinstance(region, dict):
                    continue
                maximum = _optional_float(region.get("maxHealth"))
                health = _optional_float(region.get("health"))
                if maximum is None or health is None or maximum <= 0:
                    continue
                total_max += maximum
                total_health += max(0.0, min(maximum, health))
            if total_max > 0:
                candidates.append(
                    max(0.0, min(100.0, (1.0 - total_health / total_max) * 100.0))
                )
    source = _nested(row, "event") or _nested(row, "planet")
    health = _optional_float(source.get("health"))
    maximum = _optional_float(source.get("maxHealth") or source.get("max_health"))
    if health is not None and maximum is not None and maximum > 0:
        candidates.append(
            max(0.0, min(100.0, (1.0 - health / maximum) * 100.0))
        )
    if candidates:
        # 过渡期 regions 与旧主战场血量并存，取最大值避免进度倒退
        return max(candidates)
    return 0.0


def _row_regen(row: Mapping[str, Any]) -> Any:
    return _first(
        row,
        "regen",
        "regen_rate",
        "regen_per_hour",
        "regen_pct_per_hour",
        "regeneration",
    )


def _row_rate(row: Mapping[str, Any]) -> float | None:
    return _optional_float(
        _first(row, "recent_rate_pph", "recent_rate", "net_rate_pph", "rate_pph")
    )


def _row_estimate(row: Mapping[str, Any]) -> str:
    value = _first(
        row,
        "estimated_result",
        "estimate",
        "result_estimate",
        "estimated_outcome",
    )
    return str(value or "等待趋势").strip() or "等待趋势"


def _row_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    planet = _nested(row, "planet")
    values = (
        _first(row, "planet_index", "planet_id", "index"),
        _first(planet, "index", "planetIndex", "id"),
        _first(row, "campaign_id", "campaignId"),
        _first(row, "event_id", "eventId"),
        _row_regen(row),
        _row_rate(row),
        _row_estimate(row),
    )
    return tuple(str(value or "").casefold() for value in values)


def _font(size: int, *, bold: bool = False):
    if ImageFont is None:
        return None
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    weight = "Bold" if bold else "Regular"
    candidates = [
        f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc",
        f"/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-{weight}.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    selected = None
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                selected = ImageFont.truetype(candidate, size=size)
                break
        except (OSError, ValueError):
            continue
    if selected is None:
        try:
            selected = ImageFont.load_default()
        except OSError:
            selected = None
    _FONT_CACHE[key] = selected
    return selected


def _text_width(draw: Any, text: str, font: Any) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except (AttributeError, TypeError, ValueError):
        return float(len(text) * 8)


def truncate_text(draw: Any, text: Any, font: Any, max_width: int) -> str:
    """Pixel-width truncation that treats CJK and Latin characters correctly."""

    cleaned = " ".join(str(text or "").replace("\x00", "").split())
    if max_width <= 0:
        return ""
    if _text_width(draw, cleaned, font) <= max_width:
        return cleaned
    ellipsis = "…"
    ellipsis_width = _text_width(draw, ellipsis, font)
    if ellipsis_width > max_width:
        return ""
    low, high = 0, len(cleaned)
    while low < high:
        middle = (low + high + 1) // 2
        if _text_width(draw, cleaned[:middle], font) + ellipsis_width <= max_width:
            low = middle
        else:
            high = middle - 1
    return cleaned[:low].rstrip() + ellipsis


def _normalize_faction(faction: str | None) -> str:
    key = str(faction or "").strip().casefold()
    aliases = {
        "terminid": "Terminids",
        "terminids": "Terminids",
        "bugs": "Terminids",
        "虫族": "Terminids",
        "automaton": "Automaton",
        "automatons": "Automaton",
        "bots": "Automaton",
        "机器人": "Automaton",
        "illuminate": "Illuminate",
        "squids": "Illuminate",
        "光能者": "Illuminate",
        "humans": "Humans",
        "human": "Humans",
        "super earth": "Super Earth",
        "超级地球": "Super Earth",
    }
    return aliases.get(key, str(faction or "Unknown").strip() or "Unknown")


def sort_by_players(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Deterministically sort normalized rows by players, then progress and name."""

    return sorted(
        rows,
        key=lambda row: (
            -_row_players(row),
            -_row_liberation(row),
            _row_name(row).casefold(),
            _row_identity(row),
        ),
    )


def sort_by_liberation(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Deterministically sort normalized rows by liberation, then players and name."""

    return sorted(
        rows,
        key=lambda row: (
            -_row_liberation(row),
            -_row_players(row),
            _row_name(row).casefold(),
            _row_identity(row),
        ),
    )


def _format_regen(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        stripped = value.strip()
        parsed = _optional_float(stripped.rstrip("%"))
        if parsed is None:
            return stripped or "—"
        value = parsed
    number = _optional_float(value)
    return f"{number:.2f}%/小时" if number is not None else "—"


def _format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f} 百分点/小时"


def _row_values(row: Mapping[str, Any], rank: int) -> tuple[str, ...]:
    return (
        str(rank),
        _row_name(row),
        f"{_row_players(row):,}",
        f"{_row_liberation(row):.1f}%",
        _format_regen(_row_regen(row)),
        _format_rate(_row_rate(row)),
        _row_estimate(row),
    )


def _blend(
    a: tuple[int, int, int], b: tuple[int, int, int], amount: float
) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(a[i] * (1.0 - amount) + b[i] * amount) for i in range(3))


def _load_visual_asset(faction: str, size: int):
    if Image is None:
        return None
    modules = []
    package = __package__
    for name in ([f"{package}.visual_assets"] if package else []) + ["visual_assets"]:
        try:
            modules.append(importlib.import_module(name))
        except (ImportError, ValueError):
            continue
    function_names = (
        "get_faction_emblem",
        "get_faction_icon",
        "load_faction_emblem",
        "load_faction_icon",
        "faction_emblem",
        "faction_icon",
    )
    for module in modules:
        for function_name in function_names:
            function = getattr(module, function_name, None)
            if not callable(function):
                continue
            for args in ((faction, size), (faction,)):
                try:
                    asset = function(*args)
                except (OSError, TypeError, ValueError):
                    continue
                try:
                    if isinstance(asset, (str, Path)):
                        # with 块确保关闭文件句柄，convert 结果已在内存
                        with Image.open(asset) as opened:
                            return opened.convert("RGBA").resize((size, size))
                    if hasattr(asset, "convert") and hasattr(asset, "resize"):
                        return asset.convert("RGBA").resize((size, size))
                except (OSError, TypeError, ValueError):
                    continue
    return None


def _draw_fallback_emblem(
    draw: Any, box: tuple[int, int, int, int], accent: tuple[int, int, int]
) -> None:
    left, top, right, bottom = box
    cx, cy = (left + right) // 2, (top + bottom) // 2
    radius = min(right - left, bottom - top) // 2
    draw.polygon(
        ((cx, cy - radius), (cx + radius, cy), (cx, cy + radius), (cx - radius, cy)),
        outline=accent,
        width=3,
    )
    inner = max(3, radius // 3)
    draw.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=accent)


def _draw_cell_text(
    draw: Any,
    value: str,
    font: Any,
    left: int,
    top: int,
    width: int,
    height: int,
    alignment: str,
    fill: tuple[int, int, int],
) -> None:
    padding = 10
    display = truncate_text(draw, value, font, width - padding * 2)
    text_width = _text_width(draw, display, font)
    if alignment == "right":
        x = left + width - padding - text_width
    elif alignment == "center":
        x = left + (width - text_width) / 2
    else:
        x = left + padding
    try:
        bbox = draw.textbbox((0, 0), display, font=font)
        text_height = bbox[3] - bbox[1]
        y = top + (height - text_height) / 2 - bbox[1]
    except (AttributeError, TypeError, ValueError):
        y = top + 9
    draw.text((x, y), display, font=font, fill=fill)


def _estimate_color(row: Mapping[str, Any]) -> tuple[int, int, int]:
    status = str(row.get("estimated_result_status") or "").casefold()
    estimate = _row_estimate(row).casefold()
    if status in {"success", "advancing"} or any(
        word in estimate for word in ("liberation in", "holds", "secured", "liberated")
    ):
        return SUCCESS
    if status in {"failure", "losing"} or any(
        word in estimate for word in ("fails", "failed", "losing")
    ):
        return DANGER
    return TEXT_MUTED


def _draw_table(
    draw: Any,
    rows: list[Mapping[str, Any]],
    *,
    y: int,
    heading: str,
    accent: tuple[int, int, int],
) -> int:
    table_left = MARGIN_X
    table_width = WIDTH - MARGIN_X * 2
    title_font = _font(21, bold=True)
    header_font = _font(15, bold=True)
    body_font = _font(16)

    table_bottom = y + TABLE_TITLE_H + COLUMN_HEADER_H + ROW_H * 10
    draw_chamfered_panel(
        draw,
        (table_left, y, table_left + table_width, table_bottom),
        fill=(11, 19, 27, 226),
        outline=(*accent, 225),
        accent=(*accent, 245),
        cut=12,
    )
    tab_points = [
        (table_left + 8, y + 2),
        (table_left + 208, y + 2),
        (table_left + 228, y + TABLE_TITLE_H // 2),
        (table_left + 208, y + TABLE_TITLE_H - 2),
        (table_left + 8, y + TABLE_TITLE_H - 2),
    ]
    draw.polygon(tab_points, fill=(*accent, 220))
    draw.text((table_left + 18, y + 8), heading, font=title_font, fill=TEXT)
    y += TABLE_TITLE_H

    draw.rectangle(
        (table_left, y, table_left + table_width, y + COLUMN_HEADER_H), fill=HEADER_BG
    )
    x = table_left
    for label, width, alignment in _COLUMNS:
        _draw_cell_text(
            draw,
            label,
            header_font,
            x,
            y,
            width,
            COLUMN_HEADER_H,
            alignment,
            TEXT_MUTED,
        )
        x += width
        if x < table_left + table_width:
            draw.line((x, y, x, y + COLUMN_HEADER_H), fill=GRID, width=1)
    y += COLUMN_HEADER_H

    display_rows = rows[:10]
    for rank in range(1, 11):
        row = display_rows[rank - 1] if rank <= len(display_rows) else None
        row_fill = (22, 29, 36, 220) if rank % 2 else (31, 38, 45, 220)
        draw.rectangle(
            (table_left, y, table_left + table_width, y + ROW_H), fill=row_fill
        )
        x = table_left
        values = (
            _row_values(row, rank)
            if row is not None
            else (str(rank), "—", "—", "—", "—", "—", "—")
        )
        for column_index, ((_, width, alignment), value) in enumerate(
            zip(_COLUMNS, values, strict=True)
        ):
            fill = (
                _estimate_color(row) if row is not None and column_index == 6 else TEXT
            )
            if row is None:
                fill = _blend(TEXT_MUTED, BG, 0.35)
            if row is not None and column_index == 3:
                draw_segmented_bar(
                    draw,
                    (x + 8, y + 11, x + width - 8, y + 29),
                    _row_liberation(row) / 100,
                    color=(*accent, 255),
                    segments=16,
                    gap=2,
                )
                _draw_cell_text(draw, value, _font(13, bold=True), x, y, width, ROW_H, "right", TEXT)
            elif row is not None and column_index == 4:
                regen = abs(_finite_float(_row_regen(row)))
                draw_segmented_bar(
                    draw,
                    (x + 8, y + 13, x + width - 8, y + 27),
                    min(1.0, regen / 7.0),
                    color=(214, 165, 54, 255),
                    segments=12,
                    gap=2,
                )
                _draw_cell_text(draw, value, _font(12, bold=True), x, y, width, ROW_H, "right", TEXT)
            elif row is not None and column_index == 6 and fill in {SUCCESS, DANGER}:
                draw.rounded_rectangle(
                    (x + 8, y + 6, x + width - 8, y + ROW_H - 6),
                    radius=7,
                    fill=(*_blend(PANEL, fill, 0.12), 220),
                    outline=(*fill, 235),
                    width=2,
                )
                _draw_cell_text(draw, value, _font(14, bold=True), x, y, width, ROW_H, alignment, fill)
            else:
                _draw_cell_text(draw, value, body_font, x, y, width, ROW_H, alignment, fill)
            x += width
            if x < table_left + table_width:
                draw.line((x, y, x, y + ROW_H), fill=GRID, width=1)
        draw.line(
            (table_left, y + ROW_H - 1, table_left + table_width, y + ROW_H - 1),
            fill=GRID,
            width=1,
        )
        y += ROW_H
    return y


def _defense_summary_text(
    rows: list[Mapping[str, Any]],
    defense_summary: str | Mapping[str, Any] | None,
) -> str:
    if isinstance(defense_summary, str):
        return " ".join(defense_summary.split())
    if isinstance(defense_summary, Mapping):
        explicit = _first(defense_summary, "text", "summary", "label")
        if explicit:
            return " ".join(str(explicit).split())
        active = int(
            _finite_float(_first(defense_summary, "active", "count", "defenses"))
        )
        holding = int(
            _finite_float(_first(defense_summary, "holding", "success", "winning"))
        )
        failing = int(
            _finite_float(_first(defense_summary, "failing", "failure", "losing"))
        )
        deadline = _first(defense_summary, "nearest_deadline", "deadline", "ends")
        parts = [
            f"防御战线：{active}",
            f"可守住：{holding}",
            f"有风险：{failing}",
        ]
        if deadline:
            parts.append(f"最近截止：{deadline}")
        return "   ·   ".join(parts)

    defense_rows = [row for row in rows if bool(row.get("is_defense"))]
    if not defense_rows:
        return ""
    holding = 0
    failing = 0
    for row in defense_rows:
        status = str(row.get("estimated_result_status") or "").casefold()
        if status in {"success", "advancing"}:
            holding += 1
        elif status in {"failure", "losing"}:
            failing += 1
    return f"防御战线：{len(defense_rows)}   ·   可守住：{holding}   ·   有风险：{failing}"


def render_warfront(
    rows: Iterable[Mapping[str, Any]],
    output_path: str | Path,
    *,
    faction: str = "Unknown",
    title: str | None = None,
    subtitle: str = "",
    defense_summary: str | Mapping[str, Any] | None = None,
) -> Path:
    """Render two deterministic Top-10 tables and atomically return the PNG path.

    ``output_path`` is a file path, not a directory.  The parent directory is created
    when needed.  ``RuntimeError`` is raised when Pillow is unavailable.
    """

    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is required to render a warfront table")
    normalized_rows = [row for row in rows if isinstance(row, Mapping)]
    by_players = sort_by_players(normalized_rows)[:10]
    by_liberation = sort_by_liberation(normalized_rows)[:10]
    selected_faction = _normalize_faction(faction)
    accent = FACTION_THEMES.get(selected_faction, FACTION_THEMES["Unknown"])
    defense_text = _defense_summary_text(normalized_rows, defense_summary)
    defense_h = 58 if defense_text else 0
    table_h = TABLE_TITLE_H + COLUMN_HEADER_H + ROW_H * 10
    height = TITLE_H + table_h * 2 + SECTION_GAP + defense_h + BOTTOM_PAD

    image = cockpit_backdrop(
        "user/warfront_template.png",
        (WIDTH, height),
        blur=5.5,
        darkness=150,
        tint=(5, 10, 14),
    )
    draw_scanlines(image, step=5, alpha=10)
    draw = ImageDraw.Draw(image, "RGBA")
    draw_tech_header(image, (8, 8, WIDTH - 8, TITLE_H - 8), accent=accent)
    draw = ImageDraw.Draw(image, "RGBA")

    emblem_size = 58
    emblem_x = MARGIN_X
    emblem_y = 21
    asset = _load_visual_asset(selected_faction, emblem_size)
    if asset is not None:
        image.paste(asset, (emblem_x, emblem_y), asset)
    else:
        _draw_fallback_emblem(
            draw,
            (emblem_x, emblem_y, emblem_x + emblem_size, emblem_y + emblem_size),
            accent,
        )

    title_font = _font(30, bold=True)
    subtitle_font = _font(15)
    faction_label = {
        "Humans": "超级地球",
        "Terminids": "终结族",
        "Automaton": "机器人",
        "Illuminate": "光能者",
        "Unknown": "未知阵营",
    }.get(selected_faction, selected_faction)
    heading = title or f"{faction_label}战线"
    title_x = emblem_x + emblem_size + 20
    draw.text((title_x, 22), heading, font=title_font, fill=TEXT)
    detail = subtitle.strip() or f"共 {len(normalized_rows)} 条活跃战役"
    draw.text((title_x, 62), detail, font=subtitle_font, fill=TEXT_MUTED)
    draw.line(
        (MARGIN_X, TITLE_H - 1, WIDTH - MARGIN_X, TITLE_H - 1), fill=GRID, width=1
    )

    y = TITLE_H
    y = _draw_table(draw, by_players, y=y, heading="玩家数 Top 10", accent=accent)
    y += SECTION_GAP
    y = _draw_table(
        draw, by_liberation, y=y, heading="解放进度 Top 10", accent=accent
    )

    if defense_text:
        summary_top = y + 12
        summary_bottom = summary_top + 42
        draw.rounded_rectangle(
            (MARGIN_X, summary_top, WIDTH - MARGIN_X, summary_bottom),
            radius=7,
            fill=_blend(PANEL, accent, 0.10),
            outline=_blend(GRID, accent, 0.25),
            width=1,
        )
        label_font = _font(15, bold=True)
        summary_font = _font(15)
        label = "防御"
        draw.text(
            (MARGIN_X + 14, summary_top + 11), label, font=label_font, fill=accent
        )
        label_width = _text_width(draw, label, label_font)
        summary_x = int(MARGIN_X + 28 + label_width)
        display = truncate_text(
            draw, defense_text, summary_font, WIDTH - MARGIN_X - summary_x - 14
        )
        draw.text(
            (summary_x, summary_top + 11), display, font=summary_font, fill=TEXT_MUTED
        )

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=".tmp.png",
            delete=False,
        ) as handle:
            temporary_name = handle.name
        image.convert("RGB").save(temporary_name, format="PNG", optimize=True)
        with open(temporary_name, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        prune_render_outputs(destination.parent)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        image.close()
    return destination


class WarfrontRenderer:
    """Directory-oriented wrapper around :func:`render_warfront`."""

    def __init__(self, output_dir: str | Path, *, width: int = WIDTH) -> None:
        self.output_dir = Path(output_dir)
        # Width is intentionally fixed so column alignment and truncation stay stable.
        self.width = WIDTH if int(width) <= 0 else int(width)

    @property
    def available(self) -> bool:
        return Image is not None

    def render(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        faction: str = "Unknown",
        title: str | None = None,
        subtitle: str = "",
        defense_summary: str | Mapping[str, Any] | None = None,
        filename: str | None = None,
    ) -> Path:
        """Render a uniquely named PNG unless ``filename`` is supplied."""

        name = filename or f"warfront_{uuid.uuid4().hex}.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        return render_warfront(
            rows,
            self.output_dir / Path(name).name,
            faction=faction,
            title=title,
            subtitle=subtitle,
            defense_summary=defense_summary,
        )


__all__ = [
    "FACTION_THEMES",
    "WIDTH",
    "WarfrontRenderer",
    "render_warfront",
    "sort_by_liberation",
    "sort_by_players",
    "truncate_text",
]
