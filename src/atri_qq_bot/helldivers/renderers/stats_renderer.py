"""Pillow renderer for the galaxy-wide season statistics view model.

The renderer is deliberately standalone: it has no required AstrBot imports and
no required project asset package. If a sibling/top-level ``visual_assets``
module is available, common emblem-loader conventions are tried; otherwise
clean vector-style emblems are drawn with Pillow.
"""

from __future__ import annotations

import importlib
import logging
import math
import random
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # AstrBot logging is optional.
    from .._logging import logger as _logger
except Exception:  # pragma: no cover - normal outside AstrBot
    _logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except Exception:  # pragma: no cover - clean behavior when Pillow is absent
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

try:
    from ..core.i18n import format_planet
    from ..core.stats_model import build_stats_view_model
except ImportError:  # Support direct module loading during integration/smoke tests.
    from i18n import format_planet
    from stats_model import build_stats_view_model

WIDTH = 960
HEIGHT = 1320
MARGIN = 46

BG_TOP = (7, 12, 23)
BG_BOTTOM = (2, 5, 11)
PANEL = (16, 24, 38)
PANEL_ALT = (19, 29, 46)
PANEL_EDGE = (45, 68, 96)
TEXT = (235, 241, 248)
MUTED = (132, 151, 176)
LABEL = (163, 181, 205)
TRACK = (38, 50, 68)
SUPER_EARTH_BLUE = (75, 164, 235)
YELLOW = (245, 211, 64)

FACTION_COLORS: dict[str, tuple[int, int, int]] = {
    "Humans": (75, 164, 235),
    "Terminids": (230, 184, 54),
    "Automaton": (225, 76, 70),
    "Illuminate": (150, 93, 225),
}
PIE_COLORS = (
    (76, 171, 235),
    (238, 190, 52),
    (226, 82, 72),
    (156, 99, 230),
    (64, 205, 167),
    (236, 124, 62),
    (104, 116, 139),
)

_FONT_CACHE: dict[tuple[int, bool], Any] = {}
_VISUAL_ASSETS: Any | None | bool = False


def _font(size: int, *, bold: bool = False):
    if ImageFont is None:
        return None
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/lato/Lato-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
    ]
    result = None
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                result = ImageFont.truetype(candidate, size=size)
                break
        except Exception:
            continue
    if result is None:
        try:
            result = ImageFont.load_default()
        except Exception:
            result = None
    _FONT_CACHE[key] = result
    return result


def _load_visual_assets() -> Any | None:
    global _VISUAL_ASSETS
    if _VISUAL_ASSETS is not False:
        return None if _VISUAL_ASSETS is None else _VISUAL_ASSETS
    candidates = []
    if __package__:
        candidates.append(f"{__package__}.visual_assets")
    candidates.append("visual_assets")
    for module_name in candidates:
        try:
            _VISUAL_ASSETS = importlib.import_module(module_name)
            return _VISUAL_ASSETS
        except Exception:
            continue
    _VISUAL_ASSETS = None
    return None


def _coerce_image(value: Any, size: int):
    if Image is None or value is None:
        return None
    try:
        if isinstance(value, Image.Image):
            image = value.copy()
        elif isinstance(value, (str, Path)) and Path(value).exists():
            with Image.open(value) as opened:
                image = opened.convert("RGBA")
        elif isinstance(value, bytes):
            from io import BytesIO

            with Image.open(BytesIO(value)) as opened:
                image = opened.convert("RGBA")
        else:
            return None
        image = image.convert("RGBA")
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        return image
    except Exception:
        return None


def _asset_emblem(faction: str, size: int):
    """Best-effort compatibility with future/local visual asset modules."""

    module = _load_visual_assets()
    if module is None:
        return None
    keys = {
        "Humans": ("super_earth", "humans", "human"),
        "Terminids": ("terminids", "terminid", "bugs"),
        "Automaton": ("automaton", "automatons", "bots"),
        "Illuminate": ("illuminate", "illuminates", "squids"),
    }.get(faction, (faction.lower(),))
    for function_name in (
        "get_faction_emblem",
        "load_faction_emblem",
        "get_emblem",
        "load_emblem",
        "get_asset",
        "load_asset",
    ):
        loader = getattr(module, function_name, None)
        if not callable(loader):
            continue
        for key in keys:
            for args in ((key, size), (key,)):
                try:
                    image = _coerce_image(loader(*args), size)
                except Exception:
                    image = None
                if image is not None:
                    return image
    for mapping_name in ("FACTION_EMBLEMS", "EMBLEMS", "ASSETS"):
        mapping = getattr(module, mapping_name, None)
        if not isinstance(mapping, Mapping):
            continue
        for key in keys:
            image = _coerce_image(mapping.get(key), size)
            if image is not None:
                return image
    return None


def _text_width(draw: Any, text: str, font: Any) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        return float(len(text) * 8)


def _fit_text(draw: Any, text: str, font: Any, max_width: int) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    suffix = "…"
    output = text
    while output and _text_width(draw, output + suffix, font) > max_width:
        output = output[:-1]
    return output + suffix if output else suffix


def _fmt_int(value: Any) -> str:
    try:
        return f"{max(0, int(float(value))):,}"
    except (TypeError, ValueError, OverflowError):
        return "0"


def _fmt_pct(value: Any, digits: int = 1) -> str:
    try:
        number = max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        number = 0.0
    return f"{number:.{digits}f}%"


def _rounded_panel(
    draw: Any, box: tuple[int, int, int, int], *, alt: bool = False
) -> None:
    draw.rounded_rectangle(
        box,
        radius=18,
        fill=PANEL_ALT if alt else PANEL,
        outline=PANEL_EDGE,
        width=1,
    )
    x1, y1, x2, _ = box
    draw.line((x1 + 18, y1 + 1, x2 - 18, y1 + 1), fill=(68, 100, 137), width=1)


def _star_points(
    cx: float, cy: float, outer: float, inner: float
) -> list[tuple[float, float]]:
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        radius = outer if index % 2 == 0 else inner
        points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return points


def _draw_vector_emblem(
    draw: Any,
    center: tuple[int, int],
    size: int,
    faction: str,
    color: tuple[int, int, int],
) -> None:
    cx, cy = center
    radius = size // 2
    width = max(2, size // 14)
    if faction == "Humans":
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=color,
            width=width,
        )
        draw.arc(
            (cx - radius + 6, cy - radius // 2, cx + radius - 6, cy + radius // 2),
            190,
            350,
            fill=color,
            width=width,
        )
        draw.polygon(_star_points(cx, cy, radius * 0.5, radius * 0.2), fill=color)
    elif faction == "Terminids":
        draw.ellipse(
            (
                cx - radius * 0.32,
                cy - radius * 0.45,
                cx + radius * 0.32,
                cy + radius * 0.48,
            ),
            outline=color,
            width=width,
        )
        for side in (-1, 1):
            draw.arc(
                (
                    cx - radius * 0.92 if side < 0 else cx - radius * 0.1,
                    cy - radius * 0.72,
                    cx + radius * 0.1 if side < 0 else cx + radius * 0.92,
                    cy + radius * 0.4,
                ),
                280 if side < 0 else 80,
                80 if side < 0 else 260,
                fill=color,
                width=width,
            )
            draw.line(
                (
                    cx + side * radius * 0.24,
                    cy + radius * 0.15,
                    cx + side * radius * 0.75,
                    cy + radius * 0.68,
                ),
                fill=color,
                width=width,
            )
        draw.ellipse((cx - 4, cy - 10, cx + 4, cy - 2), fill=color)
    elif faction == "Automaton":
        points = [
            (cx - radius * 0.72, cy - radius * 0.55),
            (cx + radius * 0.72, cy - radius * 0.55),
            (cx + radius * 0.9, cy + radius * 0.25),
            (cx + radius * 0.4, cy + radius * 0.72),
            (cx - radius * 0.4, cy + radius * 0.72),
            (cx - radius * 0.9, cy + radius * 0.25),
        ]
        draw.polygon(points, outline=color, width=width)
        draw.rectangle(
            (
                cx - radius * 0.5,
                cy - radius * 0.2,
                cx + radius * 0.5,
                cy + radius * 0.2,
            ),
            outline=color,
            width=width,
        )
        draw.ellipse(
            (cx - radius * 0.36, cy - 4, cx - radius * 0.16, cy + 4), fill=color
        )
        draw.ellipse(
            (cx + radius * 0.16, cy - 4, cx + radius * 0.36, cy + 4), fill=color
        )
        draw.line(
            (cx, cy + radius * 0.2, cx, cy + radius * 0.62), fill=color, width=width
        )
    else:  # Illuminate
        draw.polygon(
            [
                (cx, cy - radius),
                (cx + radius * 0.88, cy + radius * 0.62),
                (cx - radius * 0.88, cy + radius * 0.62),
            ],
            outline=color,
            width=width,
        )
        draw.ellipse(
            (
                cx - radius * 0.45,
                cy - radius * 0.26,
                cx + radius * 0.45,
                cy + radius * 0.28,
            ),
            outline=color,
            width=width,
        )
        draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=color)


def _draw_emblem(
    image: Any,
    draw: Any,
    center: tuple[int, int],
    size: int,
    faction: str,
    color: tuple[int, int, int],
) -> None:
    asset = _asset_emblem(faction, size)
    if asset is not None:
        image.alpha_composite(
            asset, (center[0] - asset.width // 2, center[1] - asset.height // 2)
        )
        return
    _draw_vector_emblem(draw, center, size, faction, color)


def _background(width: int, height: int):
    image = Image.new("RGBA", (width, height), (*BG_BOTTOM, 255))
    pixels = image.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(
            int(BG_TOP[channel] * (1.0 - ratio) + BG_BOTTOM[channel] * ratio)
            for channel in range(3)
        )
        for x in range(width):
            pixels[x, y] = (*color, 255)

    draw = ImageDraw.Draw(image)
    randomizer = random.Random(24022024)
    for _ in range(390):
        x = randomizer.randrange(width)
        y = randomizer.randrange(height)
        brightness = randomizer.randrange(45, 145)
        if randomizer.random() < 0.08:
            draw.ellipse(
                (x, y, x + 1, y + 1),
                fill=(brightness, brightness + 8, min(255, brightness + 28), 150),
            )
        else:
            draw.point(
                (x, y),
                fill=(brightness, brightness + 5, min(255, brightness + 20), 105),
            )

    # Faint command-grid lines.
    for x in range(0, width, 64):
        draw.line((x, 0, x, height), fill=(26, 42, 61, 35), width=1)
    for y in range(0, height, 64):
        draw.line((0, y, width, y), fill=(26, 42, 61, 30), width=1)
    return image


def _draw_header(image: Any, draw: Any, model: Mapping[str, Any]) -> None:
    meta = model.get("meta") if isinstance(model.get("meta"), Mapping) else {}
    _draw_emblem(image, draw, (70, 66), 48, "Humans", SUPER_EARTH_BLUE)
    title_font = _font(31, bold=True)
    sub_font = _font(14, bold=True)
    meta_font = _font(15)
    draw.text((108, 38), "银河战争 · 赛季统计", font=title_font, fill=TEXT)
    draw.text(
        (110, 79), "国防部 // 战略记录", font=sub_font, fill=MUTED
    )

    date_text = str(meta.get("display_date") or "SEST —")
    day_text = str(meta.get("war_day_label") or "战争日 —")
    date_width = _text_width(draw, date_text, meta_font)
    day_width = _text_width(draw, day_text, sub_font)
    draw.text((WIDTH - MARGIN - date_width, 43), date_text, font=meta_font, fill=LABEL)
    draw.text((WIDTH - MARGIN - day_width, 76), day_text, font=sub_font, fill=YELLOW)
    draw.line((MARGIN, 111, WIDTH - MARGIN, 111), fill=(58, 84, 116), width=2)
    draw.line((MARGIN, 115, WIDTH - MARGIN - 160, 115), fill=(30, 52, 78), width=1)


def _slice_rows(distribution: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [
        dict(item)
        for item in distribution.get("top_planets", [])
        if isinstance(item, Mapping) and float(item.get("players") or 0) > 0
    ]
    others = distribution.get("others")
    if isinstance(others, Mapping) and float(others.get("players") or 0) > 0:
        rows.append(dict(others))
    if not rows:
        rows.append(
            {
                "name": "暂无活跃部署",
                "players": 0,
                "percentage": 100.0,
                "faction": "Humans",
            }
        )
    return rows[:7]


def _draw_donut_and_labels(
    draw: Any,
    distribution: Mapping[str, Any],
    box: tuple[int, int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    rows = _slice_rows(distribution)
    chart_center = (x1 + 177, y1 + (y2 - y1) // 2)
    outer = 112
    inner = 66
    total = sum(max(0, int(float(row.get("players") or 0))) for row in rows)
    if total <= 0:
        total = 1

    start = -90.0
    mid_angles: list[float] = []
    for index, row in enumerate(rows):
        players = max(0, int(float(row.get("players") or 0)))
        span = (
            players / total * 360.0 if players else (360.0 if len(rows) == 1 else 0.0)
        )
        end = start + span
        color = PIE_COLORS[index % len(PIE_COLORS)]
        if span > 0:
            draw.pieslice(
                (
                    chart_center[0] - outer,
                    chart_center[1] - outer,
                    chart_center[0] + outer,
                    chart_center[1] + outer,
                ),
                start=start,
                end=end,
                fill=color,
                outline=(8, 14, 24),
                width=2,
            )
        mid_angles.append((start + end) / 2.0)
        start = end

    draw.ellipse(
        (
            chart_center[0] - inner,
            chart_center[1] - inner,
            chart_center[0] + inner,
            chart_center[1] + inner,
        ),
        fill=PANEL,
        outline=(54, 78, 105),
        width=2,
    )
    center_small = _font(13, bold=True)
    center_big = _font(23, bold=True)
    draw.text(
        (chart_center[0], chart_center[1] - 20),
        "已部署",
        font=center_small,
        fill=MUTED,
        anchor="mm",
    )
    draw.text(
        (chart_center[0], chart_center[1] + 14),
        _fmt_int(distribution.get("total_players")),
        font=center_big,
        fill=TEXT,
        anchor="mm",
    )

    # Labels occupy fixed rows and cannot overlap. Leader lines connect each
    # wedge to its row while preserving the donut's spatial relationship.
    label_x = x1 + 375
    label_top = y1 + 30
    available_height = y2 - label_top - 24
    step = min(43, max(34, available_height // max(1, len(rows))))
    label_font = _font(15, bold=True)
    value_font = _font(13)
    for index, (row, angle) in enumerate(zip(rows, mid_angles, strict=False)):
        line_y = label_top + index * step
        color = PIE_COLORS[index % len(PIE_COLORS)]
        radians = math.radians(angle)
        start_x = chart_center[0] + math.cos(radians) * (outer + 4)
        start_y = chart_center[1] + math.sin(radians) * (outer + 4)
        elbow_x = x1 + 353
        draw.line((start_x, start_y, elbow_x, line_y + 8), fill=(*color, 190), width=1)
        draw.line(
            (elbow_x, line_y + 8, label_x - 10, line_y + 8), fill=(*color, 190), width=1
        )
        draw.ellipse((label_x - 2, line_y + 2, label_x + 10, line_y + 14), fill=color)
        name = _fit_text(
            draw,
            format_planet(row.get("name"), "未知星球"),
            label_font,
            x2 - label_x - 122,
        )
        draw.text((label_x + 19, line_y - 2), name, font=label_font, fill=TEXT)
        detail = f"{_fmt_int(row.get('players'))}  ·  {_fmt_pct(row.get('percentage'))}"
        detail_width = _text_width(draw, detail, value_font)
        draw.text(
            (x2 - 18 - detail_width, line_y + 18), detail, font=value_font, fill=LABEL
        )


def _draw_hero(image: Any, draw: Any, model: Mapping[str, Any]) -> None:
    panel = (MARGIN, 137, WIDTH - MARGIN, 530)
    _rounded_panel(draw, panel)
    total = model.get("total_online", 0)
    eyebrow_font = _font(14, bold=True)
    count_font = _font(58, bold=True)
    unit_font = _font(18, bold=True)
    draw.text((MARGIN + 28, 160), "活跃绝地潜兵", font=eyebrow_font, fill=YELLOW)
    draw.text((MARGIN + 27, 184), _fmt_int(total), font=count_font, fill=TEXT)
    count_width = _text_width(draw, _fmt_int(total), count_font)
    draw.text(
        (MARGIN + 36 + count_width, 221),
        "在线",
        font=unit_font,
        fill=SUPER_EARTH_BLUE,
    )
    draw.line((MARGIN + 28, 259, WIDTH - MARGIN - 28, 259), fill=(48, 70, 96), width=1)
    distribution = model.get("distribution")
    if not isinstance(distribution, Mapping):
        distribution = {}
    _draw_donut_and_labels(
        draw, distribution, (MARGIN + 8, 252, WIDTH - MARGIN - 8, 516)
    )


def _draw_fronts(image: Any, draw: Any, model: Mapping[str, Any]) -> None:
    panel = (MARGIN, 552, WIDTH - MARGIN, 805)
    _rounded_panel(draw, panel, alt=True)
    heading_font = _font(17, bold=True)
    label_font = _font(15, bold=True)
    value_font = _font(14)
    draw.text((MARGIN + 24, 574), "战线部署", font=heading_font, fill=TEXT)
    draw.text(
        (WIDTH - MARGIN - 208, 577), "活跃兵力占比", font=value_font, fill=MUTED
    )

    distribution = model.get("distribution")
    fronts: Sequence[Any] = []
    if isinstance(distribution, Mapping):
        fronts = distribution.get("fronts", [])
    rows = [item for item in fronts if isinstance(item, Mapping)][:4]
    if not rows:
        rows = [
            {
                "faction": faction,
                "label": faction.upper(),
                "players": 0,
                "percentage": 0.0,
            }
            for faction in FACTION_COLORS
        ]

    row_top = 615
    for index, row in enumerate(rows):
        faction = str(row.get("faction") or "Humans")
        color = FACTION_COLORS.get(faction, SUPER_EARTH_BLUE)
        y = row_top + index * 45
        _draw_emblem(image, draw, (MARGIN + 43, y + 14), 31, faction, color)
        label = str(row.get("label") or faction).upper()
        draw.text((MARGIN + 70, y + 1), label, font=label_font, fill=TEXT)
        players_text = _fmt_int(row.get("players"))
        pct_text = _fmt_pct(row.get("percentage"))
        right = f"{players_text}  ·  {pct_text}"
        right_width = _text_width(draw, right, value_font)
        draw.text(
            (WIDTH - MARGIN - 24 - right_width, y + 2),
            right,
            font=value_font,
            fill=LABEL,
        )
        bar_x1 = MARGIN + 70
        bar_x2 = WIDTH - MARGIN - 24
        bar_y = y + 25
        draw.rounded_rectangle((bar_x1, bar_y, bar_x2, bar_y + 8), radius=4, fill=TRACK)
        try:
            percentage = max(0.0, min(100.0, float(row.get("percentage") or 0.0)))
        except (TypeError, ValueError, OverflowError):
            percentage = 0.0
        fill_width = int((bar_x2 - bar_x1) * percentage / 100.0)
        if fill_width > 0:
            draw.rounded_rectangle(
                (bar_x1, bar_y, bar_x1 + max(8, fill_width), bar_y + 8),
                radius=4,
                fill=color,
            )


def _metric_rows(stats: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    return [
        ("胜利任务", _fmt_int(stats.get("missions_won")), "任务胜利数"),
        ("失败任务", _fmt_int(stats.get("missions_lost")), "任务失败数"),
        ("胜负比", str(stats.get("win_loss_display") or "0.00"), "胜利比率"),
        (
            "任务时长",
            str(stats.get("mission_time_display") or "0 小时"),
            "赛季总计",
        ),
        (
            "任务成功率",
            _fmt_pct(stats.get("mission_success_rate")),
            "API 任务比率",
        ),
        ("API 命中率", _fmt_pct(stats.get("api_accuracy")), "API 返回值"),
        ("发射子弹", _fmt_int(stats.get("bullets_fired")), "弹药消耗"),
        ("命中子弹", _fmt_int(stats.get("bullets_hit")), "记录命中"),
        ("实际命中率", _fmt_pct(stats.get("calculated_accuracy")), "命中 / 发射"),
        ("终结族击杀", _fmt_int(stats.get("terminid_kills")), "终结族消灭数"),
        ("机器人击杀", _fmt_int(stats.get("automaton_kills")), "机器人摧毁数"),
        (
            "光能者击杀",
            _fmt_int(stats.get("illuminate_kills")),
            "光能者消灭数",
        ),
        ("总击杀", _fmt_int(stats.get("total_kills")), "敌人消灭总数"),
        ("绝地潜兵阵亡", _fmt_int(stats.get("deaths")), "为英雄致敬"),
        ("击杀 / 阵亡", str(stats.get("kill_death_display") or "0.00"), "敌我交换比"),
        ("友军伤亡", _fmt_int(stats.get("friendlies")), "误伤损失"),
    ]


def _draw_metrics(draw: Any, model: Mapping[str, Any]) -> None:
    panel = (MARGIN, 827, WIDTH - MARGIN, 1260)
    _rounded_panel(draw, panel)
    heading_font = _font(17, bold=True)
    label_font = _font(12, bold=True)
    value_font = _font(18, bold=True)
    note_font = _font(11)
    draw.text((MARGIN + 24, 849), "赛季战斗记录", font=heading_font, fill=TEXT)
    draw.text(
        (WIDTH - MARGIN - 215, 852), "全银河总计", font=_font(13), fill=MUTED
    )

    stats = model.get("stats")
    if not isinstance(stats, Mapping):
        stats = {}
    metrics = _metric_rows(stats)
    columns = 3
    gap = 12
    grid_left = MARGIN + 22
    grid_right = WIDTH - MARGIN - 22
    card_width = (grid_right - grid_left - gap * (columns - 1)) // columns
    grid_top = 884
    row_height = 54
    row_gap = 7
    for index, (label, value, note) in enumerate(metrics):
        column = index % columns
        row = index // columns
        x = grid_left + column * (card_width + gap)
        y = grid_top + row * (row_height + row_gap)
        draw.rounded_rectangle(
            (x, y, x + card_width, y + row_height),
            radius=10,
            fill=(20, 30, 47),
            outline=(40, 59, 80),
            width=1,
        )
        accent = (
            FACTION_COLORS["Terminids"]
            if "终结族" in label
            else FACTION_COLORS["Automaton"]
            if "机器人" in label
            else FACTION_COLORS["Illuminate"]
            if "光能者" in label
            else SUPER_EARTH_BLUE
        )
        draw.rectangle((x, y + 9, x + 3, y + row_height - 9), fill=accent)
        draw.text((x + 13, y + 5), label, font=label_font, fill=LABEL)
        fitted = _fit_text(draw, value, value_font, card_width - 25)
        draw.text((x + 13, y + 21), fitted, font=value_font, fill=TEXT)
        note_width = _text_width(draw, note, note_font)
        if note_width < card_width - 24:
            draw.text(
                (x + card_width - 10 - note_width, y + 39),
                note,
                font=note_font,
                fill=MUTED,
            )


def _draw_footer(draw: Any) -> None:
    footer_font = _font(12, bold=True)
    small_font = _font(11)
    draw.line((MARGIN, 1280, WIDTH - MARGIN, 1280), fill=(46, 68, 94), width=1)
    draw.text(
        (MARGIN, 1291),
        "超级地球武装部队 · 管理式民主",
        font=footer_font,
        fill=LABEL,
    )
    source = "数据：绝地潜兵 2 社区 API"
    width = _text_width(draw, source, small_font)
    draw.text((WIDTH - MARGIN - width, 1293), source, font=small_font, fill=MUTED)


class StatsRenderer:
    """Render :class:`stats_model.StatsViewModel` dictionaries to PNG files."""

    def __init__(self, output_dir: str | Path, *, width: int = WIDTH) -> None:
        self.output_dir = Path(output_dir)
        self.width = max(880, int(width))

    @property
    def available(self) -> bool:
        return Image is not None and ImageDraw is not None and ImageFont is not None

    def _cleanup(self, max_age: int = 3600) -> None:
        cutoff = time.time() - max(60, int(max_age))
        try:
            for path in self.output_dir.glob("galaxy_stats_*.png"):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except Exception:
            pass

    def render(
        self,
        view_model: Mapping[str, Any],
        *,
        output_path: str | Path | None = None,
    ) -> Path | None:
        """Render a normalized view model and return the final output path.

        The PNG is written to a uniquely named temporary file in the destination
        directory and atomically replaced into place. ``None`` is returned when
        Pillow is unavailable or rendering fails.
        """

        if not self.available:
            _logger.warning("[HD2] Pillow is unavailable; cannot render statistics")
            return None
        try:
            base = _background(WIDTH, HEIGHT)
            draw = ImageDraw.Draw(base, "RGBA")
            _draw_header(base, draw, view_model)
            _draw_hero(base, draw, view_model)
            _draw_fronts(base, draw, view_model)
            _draw_metrics(draw, view_model)
            _draw_footer(draw)

            rendered = base.convert("RGB")
            if self.width != WIDTH:
                ratio = self.width / WIDTH
                target_height = max(1, round(HEIGHT * ratio))
                rendered = rendered.resize(
                    (self.width, target_height),
                    resample=Image.Resampling.LANCZOS,
                )

            if output_path is None:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                final_path = self.output_dir / f"galaxy_stats_{uuid.uuid4().hex}.png"
            else:
                final_path = Path(output_path)
                if final_path.suffix.lower() != ".png":
                    final_path = final_path.with_suffix(".png")
                final_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = final_path.with_name(
                f".{final_path.stem}.{uuid.uuid4().hex}.tmp.png"
            )
            try:
                rendered.save(temporary, format="PNG", optimize=True)
                temporary.replace(final_path)
            finally:
                temporary.unlink(missing_ok=True)
            if output_path is None:
                self._cleanup()
            return final_path
        except Exception as exc:
            try:
                _logger.exception("[HD2] galaxy statistics render failed: %s", exc)
            except TypeError:  # Some logger shims only accept one string argument.
                _logger.exception(f"[HD2] galaxy statistics render failed: {exc}")
            return None


def render_stats_image(
    data: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    output_dir: str | Path,
    *,
    planets: Sequence[Mapping[str, Any]] | None = None,
    war: Mapping[str, Any] | None = None,
    status: Mapping[str, Any] | None = None,
    output_path: str | Path | None = None,
    width: int = WIDTH,
) -> Path | None:
    """Normalize raw data when needed, render it, and return the PNG path."""

    if isinstance(data, Mapping) and all(
        key in data for key in ("meta", "distribution", "stats", "total_online")
    ):
        model: Mapping[str, Any] = data
    else:
        model = build_stats_view_model(data, planets=planets, war=war, status=status)
    return StatsRenderer(output_dir, width=width).render(model, output_path=output_path)


__all__ = [
    "FACTION_COLORS",
    "HEIGHT",
    "StatsRenderer",
    "WIDTH",
    "render_stats_image",
]
