"""Standalone Pillow renderer for Democracy Space Station tactical reports.

The renderer accepts a normalized station dictionary and deliberately has no hard
AstrBot dependency.  It produces a deterministic, minute-granularity cached PNG:
identical content rendered in the same UTC minute resolves to the same path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from pathlib import Path

from .visual_assets import prune_render_outputs
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # AstrBot is optional; its logger is used when available.
    from .._logging import logger as logger
except Exception:  # pragma: no cover - standalone use is the normal fallback
    logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
except ImportError:  # pragma: no cover - exposed through ``available``
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

try:
    from . import visual_assets as _visual_assets
    from ..core.i18n import format_faction, format_planet
except Exception:  # pragma: no cover - direct module execution
    try:
        import visual_assets as _visual_assets  # type: ignore[no-redef]
    except Exception:
        _visual_assets = None

    def format_faction(value: object, default: str | None = None) -> str:
        return default or str(value)

    def format_planet(value: object, default: str | None = None) -> str:
        return default or str(value)


WIDTH = 1200
HEIGHT = 960
VERSION = "4"

_BG = (6, 14, 23, 255)
_PANEL = (10, 25, 38, 242)
_PANEL_ALT = (13, 31, 46, 246)
_LINE = (48, 91, 112, 180)
_GRID = (31, 73, 93, 92)
_TEXT = (229, 242, 248, 255)
_MUTED = (130, 166, 181, 255)
_DIM = (82, 119, 136, 255)
_CYAN = (86, 218, 244, 255)
_BLUE = (62, 145, 222, 255)
_YELLOW = (238, 194, 72, 255)
_GREEN = (74, 211, 137, 255)
_COOLDOWN = (132, 146, 230, 255)
_RED = (232, 91, 98, 255)

_STATUS: dict[int, tuple[str, tuple[int, int, int, int]]] = {
    0: ("未激活", (103, 119, 130, 255)),
    1: ("筹备中", _YELLOW),
    2: ("进行中", _GREEN),
    3: ("冷却中", _COOLDOWN),
}

_RESOURCE_NAMES: dict[str, str] = {
    "0": "征用点",
    "1": "普通样本",
    "2": "稀有样本",
    "3": "超级样本",
    "4": "奖章",
    "3992382197": "普通样本",
    "2985106497": "稀有样本",
    "3608481516": "征用点",
}

_FONT_REGULAR = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/arial.ttf",
)
_FONT_BOLD = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
)


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> Any:
    """Load a CJK-capable font without relying on plugin helpers."""
    size = max(8, int(size))
    if _visual_assets is not None:
        getter = getattr(_visual_assets, "get_font", None) or getattr(
            _visual_assets, "font", None
        )
        if callable(getter):
            try:
                return getter(size, bold=bold)
            except (OSError, TypeError, ValueError):
                pass
    if ImageFont is None:
        return None
    for candidate in _FONT_BOLD if bold else _FONT_REGULAR:
        try:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _safe_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    return re.sub(r"\s+", " ", text).strip() or default


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _safe_int(value: Any, default: int = 0) -> int:
    number = _safe_float(value)
    return int(number) if number is not None else default


def _first(mapping: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = list(value.values())
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _text_width(draw: Any, text: str, font: Any) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except (AttributeError, TypeError):
        box = draw.textbbox((0, 0), text, font=font)
        return float(box[2] - box[0])


def _ellipsize(draw: Any, text: str, font: Any, max_width: float) -> str:
    text = _safe_text(text)
    if _text_width(draw, text, font) <= max_width:
        return text
    suffix = "…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _text_width(draw, text[:middle].rstrip() + suffix, font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + suffix if low else ""


def _wrap(
    draw: Any,
    text: str,
    font: Any,
    max_width: float,
    *,
    max_lines: int,
) -> list[str]:
    """Greedy mixed CJK/Latin wrapping with deterministic truncation."""
    text = _safe_text(text)
    if not text or max_lines <= 0:
        return []
    tokens = re.findall(r"\s+|[A-Za-z0-9][A-Za-z0-9_./:%+\-]*|.", text)
    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = current + token
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current.rstrip())
            current = token.lstrip()
            while current and _text_width(draw, current, font) > max_width:
                split_at = 1
                while (
                    split_at < len(current)
                    and _text_width(draw, current[: split_at + 1], font) <= max_width
                ):
                    split_at += 1
                lines.append(current[:split_at])
                current = current[split_at:]
        else:
            current = candidate
    if current:
        lines.append(current.rstrip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _ellipsize(draw, lines[-1] + "…", font, max_width)
    return lines


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _asset_root() -> Path | None:
    if _visual_assets is not None:
        root = getattr(_visual_assets, "ASSET_ROOT", None)
        if root:
            return Path(root)
    candidate = Path(__file__).resolve().parent.parent / "assets"
    return candidate if candidate.is_dir() else None


def _load_asset(names: Iterable[str | Path]) -> Any | None:
    """Load the first local asset, supporting current and legacy helpers."""
    loader = None
    if _visual_assets is not None:
        # asset_image resolves administrator/user overrides and applies the same
        # transparent normalization used by the card and dashboard renderers.
        loader = getattr(_visual_assets, "asset_image", None) or getattr(
            _visual_assets, "load_asset", None
        )
    root = _asset_root()
    for name in names:
        if not name:
            continue
        path = Path(name)
        if path.is_absolute():
            candidates = [path]
        elif root is not None:
            candidates = [root / path]
            if not path.suffix:
                candidates.extend(
                    root / path.with_suffix(ext) for ext in (".png", ".webp")
                )
        else:
            candidates = []
        if callable(loader) and not path.is_absolute():
            try:
                image = loader(path)
                if image is not None:
                    return image.convert("RGBA")
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
        if Image is None:
            continue
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if root is not None and not resolved.is_relative_to(root.resolve()):
                    continue
                if resolved.is_file():
                    with Image.open(resolved) as source:
                        image = source.convert("RGBA")
                        image.load()
                    return image
            except (FileNotFoundError, OSError, ValueError):
                continue
    return None


def _find_asset(*tokens: str) -> Path | None:
    root = _asset_root()
    wanted = {slug for token in tokens if token if (slug := _slug(token))}
    if root is None or not root.is_dir() or not wanted:
        return None
    best: tuple[int, int, Path] | None = None
    try:
        paths = root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in {
                ".png",
                ".webp",
                ".jpg",
                ".jpeg",
            }:
                continue
            haystack = _slug(str(path.relative_to(root)))
            score = sum(token in haystack for token in wanted)
            if score == 0:
                continue
            rank = (score, -len(haystack), path)
            if best is None or rank[:2] > best[:2]:
                best = rank
    except OSError:
        return None
    return best[2] if best else None


def _contain(image: Any, size: tuple[int, int]) -> Any:
    if ImageOps is not None:
        return ImageOps.contain(image.convert("RGBA"), size, Image.Resampling.LANCZOS)
    return image.convert("RGBA").resize(size)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda x: str(x[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _safe_text(value, f"<{type(value).__name__}>")


def _parse_time(value: Any, now_utc: datetime) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
        return (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )
    numeric = _safe_float(value)
    if numeric is not None and not isinstance(value, str):
        if abs(numeric) >= 100_000_000:
            if abs(numeric) >= 100_000_000_000:
                numeric /= 1000.0
            try:
                return datetime.fromtimestamp(numeric, UTC)
            except (OSError, OverflowError, ValueError):
                return None
        if numeric > 0:
            return now_utc + timedelta(seconds=numeric)
        return None
    text = _safe_text(value)
    if not text:
        return None
    numeric = _safe_float(text)
    if numeric is not None:
        return _parse_time(numeric, now_utc)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _duration(seconds: float, *, expired: str = "已结束") -> str:
    if not math.isfinite(seconds):
        return "—"
    if seconds <= 0:
        return expired
    total = int(seconds)
    days, total = divmod(total, 86_400)
    hours, total = divmod(total, 3_600)
    minutes, secs = divmod(total, 60)
    if days:
        return f"{days}天 {hours:02d}小时 {minutes:02d}分钟"
    if hours:
        return f"{hours}小时 {minutes:02d}分钟"
    if minutes:
        return f"{minutes}分钟 {secs:02d}秒"
    return f"{secs}秒"


def _number(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ""
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{sign}{value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{sign}{value / 1_000_000:.2f}M"
    if absolute >= 10_000:
        return f"{sign}{value / 1_000:.1f}K"
    if value.is_integer():
        return f"{sign}{int(value):,}"
    return f"{sign}{value:,.2f}".rstrip("0").rstrip(".")


def _system_timezone() -> tzinfo:
    """Resolve the system IANA zone when possible so future DST labels stay correct."""
    candidates: list[str] = []
    environment_zone = os.environ.get("TZ", "").lstrip(":")
    if environment_zone:
        candidates.append(environment_zone)
    try:
        localtime = Path("/etc/localtime").resolve()
        marker = "zoneinfo/"
        if marker in str(localtime):
            candidates.append(str(localtime).split(marker, 1)[1])
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
    return datetime.now().astimezone().tzinfo or UTC


def _timezone(value: str | tzinfo | None) -> tzinfo:
    if isinstance(value, str) and value:
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Unknown DSS renderer timezone %r; using system local timezone", value
            )
    if isinstance(value, tzinfo):
        return value
    return _system_timezone()


def _tz_key(value: tzinfo) -> str:
    return getattr(value, "key", None) or _safe_text(value, "local")


def _exact_time(value: datetime | None, zone: tzinfo) -> tuple[str, str]:
    if value is None:
        return "未知", "未知"
    local = value.astimezone(zone)
    local_name = local.tzname() or "本地"
    return (
        f"{local:%Y-%m-%d %H:%M} {local_name}",
        f"{value.astimezone(UTC):%Y-%m-%d %H:%M} UTC",
    )


def _palette(seed: str) -> tuple[tuple[int, int, int], ...]:
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).digest()
    base = (25 + digest[0] // 4, 62 + digest[1] // 3, 80 + digest[2] // 3)
    land = (50 + digest[3] // 3, 85 + digest[4] // 3, 65 + digest[5] // 3)
    cap = (145 + digest[6] // 3, 155 + digest[7] // 3, 160 + digest[8] // 3)
    return base, land, cap


def _procedural_planet(size: int, seed: str) -> Any:
    if _visual_assets is not None:
        maker = getattr(_visual_assets, "procedural_planet", None)
        if callable(maker):
            try:
                return maker(size, seed, palette=_palette(seed), rings=False)
            except (OSError, TypeError, ValueError):
                pass
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    surface = Image.new("RGBA", (size, size), (*_palette(seed)[0], 255))
    sdraw = ImageDraw.Draw(surface, "RGBA")
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).digest()
    state = int.from_bytes(digest[:8], "big")

    def unit() -> float:
        nonlocal state
        state = (6364136223846793005 * state + 1442695040888963407) & ((1 << 64) - 1)
        return state / ((1 << 64) - 1)

    palette = _palette(seed)
    for _ in range(max(22, size // 5)):
        cx, cy = int(unit() * size), int(unit() * size)
        rx = int(size * (0.025 + unit() * 0.14))
        ry = int(size * (0.02 + unit() * 0.08))
        color = palette[int(unit() * len(palette)) % len(palette)]
        sdraw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(*color, 105))
    surface = surface.filter(ImageFilter.GaussianBlur(max(1, size // 100)))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((4, 4, size - 5, size - 5), fill=255)
    shade = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pixels = shade.load()
    for y in range(size):
        for x in range(size):
            dx = (x - size * 0.28) / size
            dy = (y - size * 0.25) / size
            alpha = int(min(225, max(0, math.hypot(dx, dy) ** 1.8 * 255)))
            pixels[x, y] = (0, 5, 16, alpha)
    surface = Image.alpha_composite(surface, shade)
    surface.putalpha(mask)
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((2, 2, size - 3, size - 3), outline=_CYAN, width=3)
    glow = glow.filter(ImageFilter.GaussianBlur(7))
    canvas = Image.alpha_composite(canvas, glow)
    canvas.alpha_composite(surface)
    return canvas


def _planet_asset(planet: Mapping[str, Any], name: str) -> Any | None:
    explicit = _first(
        planet,
        ("texture_asset", "textureAsset", "image_asset", "imageAsset", "asset"),
    )
    candidates: list[str | Path] = []
    if isinstance(explicit, (str, Path)) and not str(explicit).startswith(
        ("http://", "https://")
    ):
        candidates.append(explicit)
    slug = _slug(name)
    candidates.extend(
        (
            f"planets/{slug}.png",
            f"planet/{slug}.png",
            f"textures/planets/{slug}.png",
            "planets/default.png",
            "planet/default.png",
        )
    )
    found = _load_asset(candidates)
    if found is not None:
        return found
    fuzzy = _find_asset(name, "planet")
    return _load_asset((fuzzy,)) if fuzzy else None


def _dss_asset(station: Mapping[str, Any]) -> Any | None:
    explicit = _first(
        station,
        ("wireframe_asset", "wireframeAsset", "station_asset", "stationAsset"),
    )
    candidates: list[str | Path] = []
    if isinstance(explicit, (str, Path)) and not str(explicit).startswith(
        ("http://", "https://")
    ):
        candidates.append(explicit)
    candidates.extend(
        (
            "dss/dss_wireframe.png",
            "stations/dss_wireframe.png",
            "dss_wireframe.png",
            "dss/wireframe.png",
            "stations/dss.png",
            "emblems/dss.png",
        )
    )
    found = _load_asset(candidates)
    if found is not None:
        return found
    fuzzy = _find_asset("dss", "wireframe") or _find_asset("dss", "station")
    return _load_asset((fuzzy,)) if fuzzy else None


def _action_icon_asset(action: Mapping[str, Any], raw_name: str) -> Any | None:
    explicit = _first(action, ("icon_asset", "iconAsset", "icon", "asset"))
    candidates: list[str | Path] = []
    if isinstance(explicit, (str, Path)) and not str(explicit).startswith(
        ("http://", "https://")
    ):
        candidates.append(explicit)
    canonical_slugs = {
        "eagle_storm": "eagle_storm",
        "orbital_blockade": "orbital_blockade",
        "heavy_ordnance_distribution": "heavy_ordnance",
        "飞鹰风暴": "eagle_storm",
        "轨道封锁": "orbital_blockade",
        "重型军械配给": "heavy_ordnance",
        "重型军械分发": "heavy_ordnance",
    }
    slug = canonical_slugs.get(raw_name.strip().casefold(), _slug(raw_name))
    candidates.extend(
        (
            f"dss/actions/{slug}.png",
            f"tactical/{slug}.png",
            f"actions/{slug}.png",
            f"icons/dss/{slug}.png",
            f"icons/{slug}.png",
        )
    )
    found = _load_asset(candidates)
    if found is not None:
        return found
    fuzzy = _find_asset(raw_name, "action") or _find_asset(raw_name, "icon")
    return _load_asset((fuzzy,)) if fuzzy else None


def _draw_panel(
    draw: Any, box: tuple[int, int, int, int], *, fill: Any = _PANEL
) -> None:
    draw.rounded_rectangle(box, radius=12, fill=fill, outline=_LINE, width=1)
    x0, y0, x1, _ = box
    draw.line((x0 + 14, y0, x1 - 14, y0), fill=_CYAN, width=2)


def _draw_grid(draw: Any, box: tuple[int, int, int, int], step: int = 32) -> None:
    x0, y0, x1, y1 = box
    for x in range(x0, x1 + 1, step):
        draw.line((x, y0, x, y1), fill=_GRID, width=1)
    for y in range(y0, y1 + 1, step):
        draw.line((x0, y, x1, y), fill=_GRID, width=1)
    for x in range(x0, x1 + 1, step * 4):
        draw.line((x, y0, x, y1), fill=(40, 93, 116, 115), width=1)
    for y in range(y0, y1 + 1, step * 4):
        draw.line((x0, y, x1, y), fill=(40, 93, 116, 115), width=1)


def _draw_dss_wireframe(draw: Any, center: tuple[int, int], scale: float) -> None:
    """Procedural fallback wireframe with a recognizable station silhouette."""
    cx, cy = center
    color = (91, 224, 247, 215)
    faint = (71, 173, 199, 145)
    core = int(46 * scale)
    draw.ellipse((cx - core, cy - core, cx + core, cy + core), outline=color, width=3)
    draw.ellipse(
        (
            cx - int(25 * scale),
            cy - int(25 * scale),
            cx + int(25 * scale),
            cy + int(25 * scale),
        ),
        outline=faint,
        width=2,
    )
    draw.line((cx - core, cy, cx + core, cy), fill=color, width=2)
    draw.line((cx, cy - core, cx, cy + core), fill=color, width=2)
    arm = int(140 * scale)
    for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        ux, uy = math.cos(angle), math.sin(angle)
        px, py = -uy, ux
        start = (cx + int(ux * core), cy + int(uy * core))
        end = (cx + int(ux * arm), cy + int(uy * arm))
        draw.line((*start, *end), fill=color, width=3)
        half = int(24 * scale)
        draw.line(
            (
                end[0] - int(px * half),
                end[1] - int(py * half),
                end[0] + int(px * half),
                end[1] + int(py * half),
            ),
            fill=color,
            width=3,
        )
        for offset in (72, 102, 132):
            ox, oy = cx + int(ux * offset * scale), cy + int(uy * offset * scale)
            wing = int((16 + (offset - 72) / 4) * scale)
            draw.polygon(
                [
                    (ox + int(px * wing), oy + int(py * wing)),
                    (ox + int(ux * 13 * scale), oy + int(uy * 13 * scale)),
                    (ox - int(px * wing), oy - int(py * wing)),
                    (ox - int(ux * 10 * scale), oy - int(uy * 10 * scale)),
                ],
                outline=faint,
            )
    ring_w, ring_h = int(205 * scale), int(72 * scale)
    draw.ellipse(
        (cx - ring_w, cy - ring_h, cx + ring_w, cy + ring_h), outline=faint, width=2
    )
    draw.arc(
        (cx - ring_w - 12, cy - ring_h - 12, cx + ring_w + 12, cy + ring_h + 12),
        185,
        350,
        fill=color,
        width=2,
    )
    for radius in (175, 205):
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=(55, 133, 157, 70),
            width=1,
        )


def _draw_blueprint(
    image: Any,
    station: Mapping[str, Any],
    planet_name: str,
    box: tuple[int, int, int, int],
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_panel(draw, box)
    x0, y0, x1, y1 = box
    _draw_grid(draw, (x0 + 1, y0 + 1, x1 - 1, y1 - 1), 32)
    draw.text(
        (x0 + 24, y0 + 20), "DSS // 结构扫描", font=_font(17, True), fill=_CYAN
    )
    draw.text(
        (x0 + 24, y0 + 48),
        _ellipsize(draw, planet_name.upper(), _font(13), x1 - x0 - 48),
        font=_font(13),
        fill=_MUTED,
    )
    center = ((x0 + x1) // 2, y0 + 376)
    asset = _dss_asset(station)
    if asset is not None:
        fitted = _contain(asset, (x1 - x0 - 72, 510))
        alpha = fitted.getchannel("A")
        px = center[0] - fitted.width // 2
        py = center[1] - fitted.height // 2
        # Transparent line art gets a soft glow. Opaque uploaded artwork keeps its
        # original deep-space background without producing a rectangular halo.
        if alpha.getextrema()[0] < 255:
            glow_alpha = alpha.filter(ImageFilter.GaussianBlur(18)).point(
                lambda v: min(210, v)
            )
            glow = Image.new("RGBA", fitted.size, _CYAN)
            glow.putalpha(glow_alpha)
            image.alpha_composite(glow, (px, py))
        image.alpha_composite(fitted, (px, py))
    else:
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        _draw_dss_wireframe(ImageDraw.Draw(glow, "RGBA"), center, 1.18)
        blurred = glow.filter(ImageFilter.GaussianBlur(14))
        image.alpha_composite(blurred)
        image.alpha_composite(glow)
        draw = ImageDraw.Draw(image, "RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle(
        (x0 + 20, y1 - 142, x1 - 20, y1 - 20), fill=(7, 19, 30, 210), outline=_LINE
    )
    sid = _safe_text(_first(station, ("id32", "id", "stationId")), "749875195")
    flags = _safe_text(station.get("flags"), "—")
    rows = (
        ("平台", "民主空间站"),
        ("空间站 ID", sid),
        ("标记", flags),
        ("链路", "正常"),
    )
    for index, (label, value) in enumerate(rows):
        yy = y1 - 128 + index * 25
        draw.text((x0 + 34, yy), label, font=_font(11, True), fill=_DIM)
        draw.text(
            (x0 + 142, yy),
            _ellipsize(draw, value, _font(12), x1 - x0 - 178),
            font=_font(12),
            fill=_TEXT if index < 3 else _GREEN,
        )


def _draw_planet(
    image: Any,
    station: Mapping[str, Any],
    planet: Mapping[str, Any],
    box: tuple[int, int, int, int],
    now_utc: datetime,
    local_zone: tzinfo,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_panel(draw, box, fill=_PANEL_ALT)
    x0, y0, x1, y1 = box
    name = format_planet(_safe_text(
        _first(planet, ("name", "planetName")),
        _safe_text(station.get("planetName"), "未知"),
    ), "未知")
    owner_raw = _safe_text(
        _first(planet, ("currentOwner", "owner", "initialOwner")), "未知"
    )
    owner = format_faction(owner_raw, owner_raw)
    sector = _safe_text(planet.get("sector"), "未知扇区")
    event = _mapping(planet.get("event")) or _mapping(station.get("event"))
    event_text = _safe_text(
        _first(event, ("title", "name", "eventType", "type", "faction")),
        "无活跃事件",
    )
    statistics = _mapping(planet.get("statistics"))
    players = _safe_int(
        _first(
            statistics,
            ("playerCount", "players"),
            _first(planet, ("players", "playerCount"), station.get("playerCount")),
        )
    )
    jump_raw = _first(
        station,
        ("electionEnd", "nextElectionEnd", "jumpTime", "nextJump", "jumpEnd"),
    )
    jump = _parse_time(jump_raw, now_utc)
    local_exact, utc_exact = _exact_time(jump, local_zone)
    countdown = _duration((jump - now_utc).total_seconds()) if jump else "未知"

    pcx, pcy = x0 + 126, y0 + 126
    draw.ellipse(
        (pcx - 103, pcy - 38, pcx + 103, pcy + 38), outline=(65, 149, 178, 125), width=2
    )
    draw.arc((pcx - 112, pcy - 48, pcx + 112, pcy + 48), 188, 350, fill=_CYAN, width=2)
    sprite = _planet_asset(planet, name)
    if sprite is None:
        sprite = _procedural_planet(164, f"{name}|{owner}|{sector}")
    else:
        sprite = _contain(sprite, (164, 164))
    image.alpha_composite(sprite, (pcx - sprite.width // 2, pcy - sprite.height // 2))

    info_x = x0 + 246
    max_w = x1 - info_x - 20
    draw.text((info_x, y0 + 20), "当前轨道", font=_font(11, True), fill=_DIM)
    draw.text(
        (info_x, y0 + 38),
        _ellipsize(draw, name.upper(), _font(24, True), max_w),
        font=_font(24, True),
        fill=_TEXT,
    )
    draw.text(
        (info_x, y0 + 70),
        _ellipsize(draw, sector.upper(), _font(12), max_w),
        font=_font(12),
        fill=_MUTED,
    )
    meta = (
        ("控制方", owner, _BLUE),
        ("事件", event_text or "无", _YELLOW if event else _DIM),
        ("绝地潜兵", f"{players:,}", _GREEN),
    )
    for index, (label, value, color) in enumerate(meta):
        yy = y0 + 100 + index * 27
        draw.text((info_x, yy), label, font=_font(10, True), fill=_DIM)
        draw.text(
            (info_x + 92, yy),
            _ellipsize(draw, value, _font(12, True), max_w - 92),
            font=_font(12, True),
            fill=color,
        )
    draw.line((x0 + 22, y1 - 65, x1 - 22, y1 - 65), fill=_LINE, width=1)
    draw.text((x0 + 24, y1 - 54), "下次跃迁", font=_font(10, True), fill=_DIM)
    draw.text((x0 + 108, y1 - 58), countdown, font=_font(17, True), fill=_CYAN)
    draw.text((x0 + 246, y1 - 56), f"本地  {local_exact}", font=_font(10), fill=_MUTED)
    draw.text((x0 + 246, y1 - 35), f"UTC     {utc_exact}", font=_font(10), fill=_MUTED)


def _status(action: Mapping[str, Any]) -> tuple[int, str, tuple[int, int, int, int]]:
    raw = _first(action, ("status", "state"), 0)
    status = _safe_int(raw, -1)
    label, color = _STATUS.get(status, (f"STATUS {raw}", _RED))
    return status, label, color


def _resource_label(cost: Mapping[str, Any], index: int) -> str:
    resource = _mapping(cost.get("resource"))
    label = _safe_text(
        _first(
            cost,
            (
                "display_name",
                "displayName",
                "resourceName",
                "itemName",
                "name",
                "label",
                "resourceType",
            ),
        )
        or _first(resource, ("display_name", "displayName", "name", "label"))
    )
    if label:
        return label.upper()
    identifier = _first(cost, ("resourceId", "itemId", "id", "itemMixId"))
    if identifier is not None:
        key = _safe_text(identifier)
        return _RESOURCE_NAMES.get(key, f"RESOURCE #{key}")
    return f"RESOURCE {index + 1}"


def _cost_rate(cost: Mapping[str, Any]) -> tuple[float | None, str]:
    rate_fields = (
        ("deltaPerSecond", 1.0, "/s"),
        ("currentValueDeltaPerSecond", 1.0, "/s"),
        ("ratePerSecond", 1.0, "/s"),
        ("deltaPerMinute", 60.0, "/m"),
        ("ratePerMinute", 60.0, "/m"),
        ("deltaPerHour", 3600.0, "/小时"),
        ("ratePerHour", 3600.0, "/小时"),
        ("delta", 1.0, "/s"),
        ("rate", 1.0, "/s"),
    )
    for key, divisor, suffix in rate_fields:
        if key not in cost:
            continue
        raw = _safe_float(cost.get(key))
        if raw is not None:
            display = raw if suffix == "/s" else raw
            return raw / divisor, f"{_number(display, signed=True)}{suffix}"
    return None, "Δ —"


def _draw_action_icon(
    image: Any,
    action: Mapping[str, Any],
    raw_name: str,
    box: tuple[int, int, int, int],
    color: Any,
) -> None:
    x0, y0, x1, y1 = box
    asset = _action_icon_asset(action, raw_name)
    if asset is not None:
        icon = _contain(asset, (x1 - x0, y1 - y0))
        image.alpha_composite(
            icon, (x0 + (x1 - x0 - icon.width) // 2, y0 + (y1 - y0 - icon.height) // 2)
        )
        return
    draw = ImageDraw.Draw(image, "RGBA")
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    radius = max(10, min(x1 - x0, y1 - y0) // 2 - 3)
    draw.polygon(
        [
            (cx, cy - radius),
            (cx + int(radius * 0.86), cy - radius // 2),
            (cx + int(radius * 0.86), cy + radius // 2),
            (cx, cy + radius),
            (cx - int(radius * 0.86), cy + radius // 2),
            (cx - int(radius * 0.86), cy - radius // 2),
        ],
        outline=color,
        width=2,
    )
    lowered = raw_name.lower()
    if "eagle" in lowered:
        draw.line(
            (cx - radius + 5, cy, cx, cy - radius // 2, cx + radius - 5, cy),
            fill=color,
            width=3,
        )
        draw.line(
            (cx - radius // 2, cy + 6, cx, cy, cx + radius // 2, cy + 6),
            fill=color,
            width=2,
        )
    elif "block" in lowered or "shield" in lowered:
        draw.polygon(
            [
                (cx, cy - radius // 2),
                (cx + radius // 2, cy - 4),
                (cx + radius // 3, cy + radius // 2),
                (cx, cy + radius * 2 // 3),
                (cx - radius // 3, cy + radius // 2),
                (cx - radius // 2, cy - 4),
            ],
            outline=color,
            width=2,
        )
    else:
        draw.ellipse(
            (cx - radius // 2, cy - radius // 2, cx + radius // 2, cy + radius // 2),
            outline=color,
            width=2,
        )
        draw.line((cx - radius + 7, cy, cx + radius - 7, cy), fill=color, width=2)
        draw.line((cx, cy - radius + 7, cx, cy + radius - 7), fill=color, width=2)


def _draw_cost(
    draw: Any,
    cost: Mapping[str, Any],
    index: int,
    box: tuple[int, int, int, int],
    accent: Any,
) -> None:
    x0, y0, x1, y1 = box
    current = _safe_float(
        _first(cost, ("currentValue", "current", "value", "progress"))
    )
    target = _safe_float(
        _first(cost, ("targetValue", "target", "required", "maxValue"))
    )
    rate_per_second, rate_label = _cost_rate(cost)
    progress = 0.0
    if current is not None and target is not None and target > 0:
        progress = max(0.0, min(1.0, current / target))
    remaining = max(0.0, (target or 0.0) - (current or 0.0))
    if target is not None and current is not None and current >= target:
        eta = "已筹满"
    elif (
        rate_per_second is not None
        and rate_per_second > 0
        and target is not None
        and current is not None
    ):
        eta = f"预计 {_duration(remaining / rate_per_second, expired='0秒')}"
    else:
        eta = "预计 —"
    label = _resource_label(cost, index)
    values = f"{_number(current)} / {_number(target)}"
    row_height = y1 - y0
    if row_height < 26:
        # Dense payloads still show every cost's label, values, progress, delta, and ETA.
        font_size = max(6, min(8, row_height - 5))
        compact_font = _font(font_size)
        compact_bold = _font(font_size, True)
        width = x1 - x0
        label_width = width * 0.31
        value_width = width * 0.30
        draw.text(
            (x0, y0),
            _ellipsize(draw, label, compact_bold, label_width),
            font=compact_bold,
            fill=_MUTED,
        )
        draw.text(
            (x0 + label_width + 3, y0),
            _ellipsize(draw, values, compact_bold, value_width),
            font=compact_bold,
            fill=_TEXT,
        )
        delta_eta = f"{rate_label} · {eta}"
        draw.text(
            (x0 + label_width + value_width + 6, y0),
            _ellipsize(
                draw, delta_eta, compact_font, width - label_width - value_width - 6
            ),
            font=compact_font,
            fill=accent if eta == "已筹满" else _DIM,
        )
        bar_y = min(y1 - 2, y0 + max(6, row_height - 3))
        draw.rectangle((x0, bar_y, x1, bar_y + 1), fill=(35, 57, 69, 255))
        if progress > 0:
            draw.rectangle(
                (x0, bar_y, x0 + max(2, int(width * progress)), bar_y + 1), fill=accent
            )
        return

    draw.text(
        (x0, y0),
        _ellipsize(draw, label, _font(9, True), (x1 - x0) * 0.38),
        font=_font(9, True),
        fill=_MUTED,
    )
    value_w = _text_width(draw, values, _font(9, True))
    draw.text((x1 - value_w, y0), values, font=_font(9, True), fill=_TEXT)
    bar_y = y0 + 15
    draw.rounded_rectangle((x0, bar_y, x1, bar_y + 6), radius=3, fill=(35, 57, 69, 255))
    if progress > 0:
        draw.rounded_rectangle(
            (x0, bar_y, x0 + max(5, int((x1 - x0) * progress)), bar_y + 6),
            radius=3,
            fill=accent,
        )
    draw.text((x0, bar_y + 8), rate_label, font=_font(8), fill=_DIM)
    eta_w = _text_width(draw, eta, _font(8, True))
    draw.text(
        (x1 - eta_w, bar_y + 8),
        eta,
        font=_font(8, True),
        fill=accent if eta == "已筹满" else _DIM,
    )


def _draw_action_card(
    image: Any,
    action: Mapping[str, Any],
    box: tuple[int, int, int, int],
    now_utc: datetime,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    x0, y0, x1, y1 = box
    status, status_label, accent = _status(action)
    draw.rounded_rectangle(
        box, radius=10, fill=(11, 28, 42, 248), outline=(54, 91, 108, 220), width=1
    )
    draw.rectangle((x0, y0 + 10, x0 + 4, y1 - 10), fill=accent)

    raw_name = _safe_text(
        _first(action, ("name", "title", "raw_name", "rawName")), "战术行动"
    )
    display_name = _safe_text(_first(action, ("display_name", "displayName")), raw_name)
    description = _safe_text(
        _first(
            action,
            (
                "display_description",
                "displayDescription",
                "display_strategic_description",
                "displayStrategicDescription",
                "strategicDescription",
                "description",
                "desc",
            ),
        ),
        "No tactical briefing available.",
    )
    icon_size = min(48, max(34, y1 - y0 - 20))
    _draw_action_icon(
        image,
        action,
        raw_name,
        (x0 + 14, y0 + 13, x0 + 14 + icon_size, y0 + 13 + icon_size),
        accent,
    )

    title_x = x0 + 24 + icon_size
    badge_font = _font(9, True)
    badge_w = int(_text_width(draw, status_label, badge_font)) + 18
    badge_box = (x1 - badge_w - 12, y0 + 12, x1 - 12, y0 + 32)
    draw.rounded_rectangle(
        badge_box,
        radius=6,
        fill=(*accent[:3], 42),
        outline=accent,
    )
    text_box = draw.textbbox((0, 0), status_label, font=badge_font)
    text_x = badge_box[0] + (badge_w - (text_box[2] - text_box[0])) / 2 - text_box[0]
    text_y = badge_box[1] + (20 - (text_box[3] - text_box[1])) / 2 - text_box[1]
    draw.text((text_x, text_y), status_label, font=badge_font, fill=accent)
    title_width = max(30, x1 - badge_w - 24 - title_x)
    draw.text(
        (title_x, y0 + 10),
        _ellipsize(draw, display_name, _font(15, True), title_width),
        font=_font(15, True),
        fill=_TEXT,
    )
    raw_label = f"原文  {raw_name}"
    draw.text(
        (title_x, y0 + 33),
        _ellipsize(draw, raw_label, _font(8), x1 - title_x - 14),
        font=_font(8),
        fill=_DIM,
    )

    costs = _dict_list(_first(action, ("costs", "resourceCosts", "requirements"), []))
    available_height = y1 - y0
    cost_height = 34
    fixed = 76 + len(costs) * cost_height
    desc_lines = max(0, min(2, (available_height - fixed) // 15))
    desc_y = y0 + 60
    for line in _wrap(draw, description, _font(10), x1 - x0 - 28, max_lines=desc_lines):
        draw.text((x0 + 14, desc_y), line, font=_font(10), fill=_MUTED)
        desc_y += 15

    expires = _parse_time(
        _first(
            action, ("statusExpire", "expireTime", "endTime", "expiresAt", "expiration")
        ),
        now_utc,
    )
    timer = _duration((expires - now_utc).total_seconds()) if expires else "—"
    timer_label = {
        0: "可用",
        1: "筹资窗口",
        2: "生效中",
        3: "冷却中",
    }.get(status, "计时")
    cost_y = max(y0 + 75, desc_y + 3)
    timer_text = f"{timer_label}  {timer}"
    draw.text(
        (x0 + 14, cost_y),
        _ellipsize(draw, timer_text, _font(9, True), x1 - x0 - 28),
        font=_font(9, True),
        fill=accent,
    )
    cost_y += 19

    if not costs:
        draw.text(
            (x0 + 14, cost_y + 2), "暂无资源消耗数据", font=_font(8), fill=_DIM
        )
        return
    row_height = max(10, min(cost_height, (y1 - cost_y - 7) // len(costs)))
    for index, cost in enumerate(costs):
        row_top = cost_y + index * row_height
        row_bottom = min(y1 - 6, row_top + row_height)
        _draw_cost(draw, cost, index, (x0 + 14, row_top, x1 - 14, row_bottom), accent)


def _draw_actions(
    image: Any,
    station: Mapping[str, Any],
    box: tuple[int, int, int, int],
    now_utc: datetime,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_panel(draw, box)
    x0, y0, x1, y1 = box
    actions = _dict_list(
        _first(station, ("tacticalActions", "tactical_actions", "actions"), [])
    )
    actions.sort(
        key=lambda item: {2: 0, 1: 1, 3: 2, 0: 3}.get(
            _safe_int(item.get("status"), -1), 9
        )
    )
    draw.text(
        (x0 + 20, y0 + 15), "战术行动控制", font=_font(16, True), fill=_CYAN
    )
    draw.text(
        (x1 - 108, y0 + 18),
        f"{len(actions):02d} 项行动",
        font=_font(9, True),
        fill=_DIM,
    )
    content_top = y0 + 48
    if not actions:
        draw.text(
            (x0 + 22, content_top + 28),
            "暂无战术行动遥测数据",
            font=_font(13, True),
            fill=_DIM,
        )
        return

    shown = actions[:6]
    columns = 1 if len(shown) <= 3 else 2
    rows = math.ceil(len(shown) / columns)
    gap = 10
    card_width = (x1 - x0 - 28 - gap * (columns - 1)) // columns
    card_height = (y1 - content_top - 14 - gap * (rows - 1)) // rows
    for index, action in enumerate(shown):
        column = index % columns
        row = index // columns
        left = x0 + 14 + column * (card_width + gap)
        top = content_top + row * (card_height + gap)
        _draw_action_card(
            image, action, (left, top, left + card_width, top + card_height), now_utc
        )
    if len(actions) > len(shown):
        message = f"+{len(actions) - len(shown)} ADDITIONAL ACTIONS OMITTED"
        width = _text_width(draw, message, _font(8, True))
        draw.text((x1 - width - 18, y1 - 13), message, font=_font(8, True), fill=_RED)


class DSSRenderer:
    """Render a normalized DSS station dictionary to a tactical 1200×960 PNG.

    Parameters:
        output_dir: Cache/output directory. It is created if needed.
        width: Output width; values below 1000 are clamped to 1000.
        height: Output height; values below 800 are clamped to 800.
        local_timezone: IANA zone name or ``tzinfo`` used for LOCAL labels. If
            omitted, the operating system's local timezone is used.

    ``render`` returns ``None`` only when Pillow is unavailable or rendering fails.
    """

    def __init__(
        self,
        output_dir: str | Path,
        *,
        width: int = WIDTH,
        height: int = HEIGHT,
        local_timezone: str | tzinfo | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = max(1000, int(width))
        self.height = max(800, int(height))
        self.local_timezone = _timezone(local_timezone)

    @property
    def available(self) -> bool:
        return Image is not None and ImageDraw is not None

    def cache_path(
        self, station: Mapping[str, Any] | None, *, now: datetime | None = None
    ) -> Path:
        """Return the deterministic minute-bucket cache path without rendering."""
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        current = current.astimezone(UTC)
        minute_bucket = int(current.timestamp() // 60)
        payload = {
            "renderer": VERSION,
            "size": [self.width, self.height],
            "timezone": _tz_key(self.local_timezone),
            "minute": minute_bucket,
            "station": _canonical(station or {}),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()[:20]
        return self.output_dir / f"dss_{VERSION}_{minute_bucket}_{digest}.png"

    def render(
        self,
        station: Mapping[str, Any] | None,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> Path | None:
        """Render ``station`` and atomically save it at its deterministic cache path."""
        if not self.available:
            logger.warning("DSS renderer unavailable because Pillow is not installed")
            return None
        data = dict(station) if isinstance(station, Mapping) else {}
        output = self.cache_path(data, now=now)
        if output.is_file() and not force:
            return output
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        current = current.astimezone(UTC)
        minute_bucket = int(current.timestamp() // 60)
        render_now = datetime.fromtimestamp(minute_bucket * 60, UTC)
        try:
            image = self._render_image(data, render_now)
            self._atomic_save(image, output)
            prune_render_outputs(output.parent, keep=60)
            return output
        except Exception:
            logger.exception("DSS tactical renderer failed")
            return None

    def _render_image(self, station: Mapping[str, Any], now_utc: datetime) -> Any:
        base = Image.new("RGBA", (WIDTH, HEIGHT), _BG)
        draw = ImageDraw.Draw(base, "RGBA")
        # Background technical grid and restrained blue atmospheric bloom.
        _draw_grid(draw, (0, 0, WIDTH, HEIGHT), 48)
        bloom = Image.new("RGBA", base.size, (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(bloom, "RGBA")
        bdraw.ellipse((-180, 160, 630, 970), fill=(20, 126, 164, 48))
        bdraw.ellipse((730, -260, 1350, 360), fill=(39, 94, 164, 34))
        base = Image.alpha_composite(base, bloom.filter(ImageFilter.GaussianBlur(90)))
        draw = ImageDraw.Draw(base, "RGBA")

        planet = _mapping(station.get("planet"))
        planet_name = _safe_text(
            _first(planet, ("name", "planetName")),
            _safe_text(station.get("planetName"), "未知轨道"),
        )
        draw.rectangle((0, 0, WIDTH, 82), fill=(7, 20, 31, 248))
        draw.line((0, 81, WIDTH, 81), fill=_CYAN, width=2)
        draw.text((30, 16), "DSS", font=_font(34, True), fill=_TEXT)
        draw.text(
            (122, 17), "民主空间站", font=_font(19, True), fill=_CYAN
        )
        draw.text(
            (123, 47),
            "战略指挥链路 // 实时遥测",
            font=_font(10),
            fill=_MUTED,
        )
        local_now, utc_now = _exact_time(now_utc, self.local_timezone)
        stamp = f"快照  本地 {local_now}  //  UTC {utc_now}"
        stamp_w = _text_width(draw, stamp, _font(9, True))
        draw.text((WIDTH - stamp_w - 28, 29), stamp, font=_font(9, True), fill=_DIM)

        left_box = (24, 104, 494, 934)
        planet_box = (516, 104, 1176, 334)
        actions_box = (516, 354, 1176, 934)
        _draw_blueprint(base, station, planet_name, left_box)
        _draw_planet(base, station, planet, planet_box, now_utc, self.local_timezone)
        _draw_actions(base, station, actions_box, now_utc)
        draw = ImageDraw.Draw(base, "RGBA")
        footer = "超级地球武装部队 // DSS 遥测 // api.helldivers2.dev"
        draw.text((28, 942), footer, font=_font(8, True), fill=_DIM)

        if (self.width, self.height) != (WIDTH, HEIGHT):
            return base.resize(
                (self.width, self.height), Image.Resampling.LANCZOS
            ).convert("RGB")
        return base.convert("RGB")

    @staticmethod
    def _atomic_save(image: Any, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.stem}.", suffix=".tmp.png", dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            image.save(temporary, format="PNG", optimize=True)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)


def render_dss(
    station: Mapping[str, Any] | None,
    output_dir: str | Path,
    *,
    now: datetime | None = None,
    local_timezone: str | tzinfo | None = None,
    force: bool = False,
) -> Path | None:
    """Convenience wrapper around :class:`DSSRenderer`."""
    return DSSRenderer(output_dir, local_timezone=local_timezone).render(
        station,
        now=now,
        force=force,
    )


__all__ = ["DSSRenderer", "HEIGHT", "VERSION", "WIDTH", "render_dss"]
