"""Galaxy war map rendering with Pillow.

The renderer accepts the legacy compact point dictionaries produced by
``WarState.map_points`` and richer planet dictionaries when callers have them.
All optional visual metadata is handled defensively so incomplete API payloads
still produce a useful map.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import threading
import time
import uuid
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

try:
    from ..core.i18n import format_faction, format_planet, format_status, t
    from .hud_theme import cockpit_backdrop, draw_chamfered_panel, draw_scanlines
    from .visual_assets import asset_image, asset_sha256, paste_contain
except Exception:  # pragma: no cover
    format_faction = lambda value, default=None: default or str(value)  # type: ignore
    format_planet = lambda value, default=None: default or str(value)  # type: ignore
    format_status = lambda value, default=None: default or str(value)  # type: ignore
    t = lambda key, default=None: default or str(key)  # type: ignore
    asset_image = None  # type: ignore
    asset_sha256 = lambda value: ""  # type: ignore
    paste_contain = None  # type: ignore

try:
    from .._logging import logger
except Exception:  # pragma: no cover
    logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFilter = None  # type: ignore
    ImageFont = None  # type: ignore


FACTION_COLORS: dict[str, tuple[int, int, int]] = {
    "Humans": (90, 170, 255),
    "Super Earth": (90, 170, 255),
    "Terminids": (245, 200, 60),
    "Automaton": (235, 80, 70),
    "Illuminate": (170, 110, 235),
    "Unknown": (150, 155, 170),
}

_FACTION_LABELS = {
    "Humans": "超级地球",
    "Terminids": "终结族",
    "Automaton": "机器人",
    "Illuminate": "光能者",
    "Unknown": "未占领",
}
_BIOME_PALETTES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "desert": ((194, 146, 72), (232, 196, 116), (103, 69, 42)),
    "jungle": ((43, 111, 70), (91, 155, 78), (23, 66, 52)),
    "forest": ((48, 105, 75), (91, 140, 89), (28, 61, 54)),
    "deciduous": ((48, 105, 75), (91, 140, 89), (28, 61, 54)),
    "ice": ((136, 190, 213), (219, 239, 244), (77, 122, 155)),
    "snow": ((164, 198, 218), (236, 246, 248), (91, 127, 158)),
    "arctic": ((136, 190, 213), (219, 239, 244), (77, 122, 155)),
    "glacier": ((136, 190, 213), (219, 239, 244), (77, 122, 155)),
    "volcan": ((101, 52, 45), (218, 89, 45), (45, 36, 43)),
    "lava": ((91, 45, 39), (231, 91, 36), (41, 32, 38)),
    "magma": ((101, 52, 45), (218, 89, 45), (45, 36, 43)),
    "swamp": ((65, 91, 61), (130, 135, 72), (38, 53, 45)),
    "toxic": ((91, 117, 57), (177, 184, 69), (45, 60, 42)),
    "moon": ((114, 119, 128), (185, 186, 181), (64, 67, 76)),
    "rock": ((111, 91, 82), (171, 148, 125), (59, 54, 57)),
    "oasis": ((194, 146, 72), (91, 155, 78), (103, 69, 42)),
    "cyberstan": ((91, 45, 39), (114, 119, 128), (45, 36, 43)),
    "rift": ((64, 40, 90), (130, 80, 160), (40, 25, 60)),
    "blackhole": ((10, 8, 20), (40, 25, 60), (5, 4, 10)),
    "default": ((86, 117, 139), (137, 166, 170), (48, 68, 86)),
}

_BG_INNER = (19, 31, 60)
_BG_OUTER = (3, 5, 13)
_RENDERER_VERSION = "map-renderer-v5"
_TAU = math.tau


@lru_cache(maxsize=32)
def _find_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    if bold:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "none"}
    return bool(value)


def _faction_key(value: Any) -> str:
    raw = str(value or "Unknown").strip()
    key = raw.casefold().replace("_", " ").replace("-", " ")
    aliases = {
        "human": "Humans",
        "humans": "Humans",
        "super earth": "Humans",
        "super earth federation": "Humans",
        "terminid": "Terminids",
        "terminids": "Terminids",
        "bugs": "Terminids",
        "automaton": "Automaton",
        "automatons": "Automaton",
        "bots": "Automaton",
        "illuminate": "Illuminate",
        "illuminates": "Illuminate",
        "squid": "Illuminate",
    }
    return aliases.get(key, raw if raw in FACTION_COLORS else "Unknown")


def _stable_seed(*parts: Any) -> int:
    payload = "|".join(str(part) for part in parts).encode("utf-8", "replace")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _target_indices(value: Any) -> list[int]:
    """Extract planet indices from old scalar/list and richer object shapes."""
    result: list[int] = []

    def visit(item: Any) -> None:
        if isinstance(item, bool) or item is None:
            return
        if isinstance(item, (int, float)):
            result.append(int(item))
            return
        if isinstance(item, str):
            text = item.strip()
            if text.lstrip("-").isdigit():
                result.append(int(text))
            return
        if isinstance(item, dict):
            for key in (
                "targetIndex",
                "target_index",
                "planetIndex",
                "planet_index",
                "index",
                "id",
                "target",
                "planet",
            ):
                if key in item:
                    visit(item[key])
                    return
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, Iterable):
            for nested in item:
                visit(nested)

    visit(value)
    return list(dict.fromkeys(result))


def _biome_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "slug", "id", "description", "type"):
            if value.get(key):
                return str(value[key])
        return ""
    return str(value or "")


def _event_present(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and not bool(value.get("inactive"))
    return _truthy(value)


class MapRenderer:
    def __init__(
        self,
        output_dir: Path,
        *,
        width: int = 1100,
        height: int = 1100,
        cache_ttl: int = 600,
        label_top_n: int = 15,
        visual_mode: str = "uploaded_reference",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.cache_ttl = max(60, int(cache_ttl))
        self.label_top_n = max(0, int(label_top_n))
        self.visual_mode = (
            visual_mode if visual_mode in {"uploaded_reference", "derived"} else "uploaded_reference"
        )
        self._last_path: Path | None = None
        self._last_ts: float = 0
        self._last_fingerprint = ""
        self.header_h = 70
        self.footer_h = 150
        self.plot_top = self.header_h + 10
        self.plot_size = min(
            self.width - 40,
            self.height - self.plot_top - self.footer_h - 10,
        )
        self.plot_left = (self.width - self.plot_size) // 2
        self._static_layer = None
        self._static_layer_lock = threading.Lock()
        self._sprite_cache: dict[tuple[Any, ...], "Image.Image"] = {}

    @property
    def available(self) -> bool:
        return Image is not None

    @property
    def _center(self) -> tuple[int, int]:
        return (
            self.plot_left + self.plot_size // 2,
            self.plot_top + self.plot_size // 2,
        )

    def _map_xy(self, x: float, y: float) -> tuple[int, int]:
        # Clamp unusual payloads to the circular galaxy instead of the square plot.
        x = max(-1.0, min(1.0, x))
        y = max(-1.0, min(1.0, y))
        magnitude = math.hypot(x, y)
        if magnitude > 0.96:
            x *= 0.96 / magnitude
            y *= 0.96 / magnitude
        cx, cy = self._center
        radius = self.plot_size / 2 - 9
        return int(cx + x * radius), int(cy - y * radius)

    def _normalise_points(self, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalised: list[dict[str, Any]] = []
        for order, raw in enumerate(points):
            if not isinstance(raw, dict):
                continue
            position = raw.get("position")
            if not isinstance(position, dict):
                position = {}
            x = _safe_float(raw.get("x", position.get("x", 0.0)))
            y = _safe_float(raw.get("y", position.get("y", 0.0)))
            idx_value = raw.get("index", raw.get("planetIndex", raw.get("id")))
            idx = None if idx_value is None else _safe_int(idx_value, order)
            owner = _faction_key(
                raw.get("owner", raw.get("currentOwner", raw.get("faction")))
            )
            event = raw.get("event")
            status_value = raw.get("status", raw.get("state", ""))
            if isinstance(status_value, dict):
                status = str(
                    status_value.get("name")
                    or status_value.get("type")
                    or status_value.get("status")
                    or ""
                )
            elif isinstance(status_value, list):
                status = ", ".join(str(item) for item in status_value[:3])
            else:
                status = str(status_value or "")
            defense = any(
                _truthy(raw.get(key))
                for key in ("defense", "defending", "isDefense", "underAttack")
            )
            if isinstance(event, dict):
                event_type = str(
                    event.get("eventType") or event.get("type") or ""
                ).casefold()
                defense = defense or "defen" in event_type
            health = raw.get(
                "health_pct",
                raw.get("healthPct", raw.get("liberation", raw.get("progress"))),
            )
            health_pct = None
            if health is not None:
                health_pct = max(0.0, min(100.0, _safe_float(health)))
            normalised.append(
                {
                    "index": idx,
                    "name": format_planet(raw.get("name") or raw.get("planetName"), ""),
                    "x": round(x, 6),
                    "y": round(y, 6),
                    "owner": owner,
                    "true_owner": _faction_key(raw.get("true_owner", owner)),
                    "players": max(
                        0,
                        _safe_int(
                            raw.get(
                                "players",
                                raw.get("playerCount", raw.get("player_count", 0)),
                            )
                        ),
                    ),
                    "active": any(
                        _truthy(raw.get(key))
                        for key in ("active", "campaign", "isActive", "contested")
                    )
                    or _event_present(event),
                    "dss": any(
                        _truthy(raw.get(key))
                        for key in ("dss", "hasDss", "isDss", "station")
                    ),
                    "defense": defense,
                    "event": _event_present(event),
                    "event_type": (
                        str(event.get("eventType") or event.get("type") or "")
                        if isinstance(event, dict)
                        else ""
                    ),
                    "health_pct": health_pct,
                    "waypoints": _target_indices(
                        raw.get("waypoints", raw.get("supplyRoutes", []))
                    ),
                    "attacking": _target_indices(
                        raw.get("attacking", raw.get("attacks", []))
                    ),
                    "sector": str(raw.get("sector") or "UNKNOWN SECTOR"),
                    "biome": _biome_name(
                        raw.get("biome", raw.get("environment", raw.get("climate")))
                    ),
                    "status": status,
                }
            )
        return normalised

    def _fingerprint(
        self,
        points: list[dict[str, Any]],
        title: str,
        subtitle: str,
    ) -> str:
        payload = {
            "renderer": _RENDERER_VERSION,
            "width": self.width,
            "height": self.height,
            "label_top_n": self.label_top_n,
            "visual_mode": self.visual_mode,
            "reference_asset_sha256": asset_sha256("user/map.png") if asset_sha256 else "",
            "title": title,
            "subtitle": subtitle,
            "points": points,
        }
        packed = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(packed).hexdigest()[:20]

    def _circle_mask(self):
        mask = Image.new("L", (self.width, self.height), 0)
        draw = ImageDraw.Draw(mask)
        margin = 3
        draw.ellipse(
            (
                self.plot_left + margin,
                self.plot_top + margin,
                self.plot_left + self.plot_size - margin,
                self.plot_top + self.plot_size - margin,
            ),
            fill=255,
        )
        return mask

    def _build_static_layer(self):
        # /map 经 to_thread 并发渲染，懒加载需要互斥，避免半初始化层被旁路读到
        with self._static_layer_lock:
            if self._static_layer is None:
                self._static_layer = self._build_static_layer_unlocked()
        return self._static_layer.copy()

    def _build_static_layer_unlocked(self):
        try:
            gradient = Image.radial_gradient("L").resize((self.width, self.height))
            inner = Image.new("RGB", (self.width, self.height), _BG_INNER)
            outer = Image.new("RGB", (self.width, self.height), _BG_OUTER)
            base = Image.composite(outer, inner, gradient).convert("RGBA")
        except Exception:
            base = Image.new("RGBA", (self.width, self.height), (*_BG_OUTER, 255))

        draw = ImageDraw.Draw(base)
        rnd = random.Random(_stable_seed(_RENDERER_VERSION, self.width, self.height))
        for _ in range(max(350, self.width * self.height // 1800)):
            sx = rnd.randrange(self.width)
            sy = rnd.randrange(self.height)
            brightness = rnd.randrange(38, 174)
            color = (brightness, brightness, min(255, brightness + 35), 210)
            if rnd.random() < 0.065:
                draw.ellipse((sx - 1, sy - 1, sx + 1, sy + 1), fill=color)
            else:
                draw.point((sx, sy), fill=color)

        galaxy = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(galaxy)
        cx, cy = self._center
        radius = self.plot_size // 2 - 4
        gdraw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(8, 14, 31, 218),
            outline=(73, 113, 170, 230),
            width=2,
        )
        for fraction in (0.25, 0.5, 0.75):
            ring = int(radius * fraction)
            gdraw.ellipse(
                (cx - ring, cy - ring, cx + ring, cy + ring),
                outline=(60, 91, 137, 86),
                width=1,
            )
        for degree in range(0, 360, 30):
            angle = math.radians(degree)
            ex = cx + math.cos(angle) * radius
            ey = cy + math.sin(angle) * radius
            gdraw.line((cx, cy, ex, ey), fill=(55, 82, 122, 62), width=1)
        for degree in range(0, 360, 10):
            angle = math.radians(degree)
            tick = 8 if degree % 30 == 0 else 4
            x1 = cx + math.cos(angle) * (radius - tick)
            y1 = cy + math.sin(angle) * (radius - tick)
            x2 = cx + math.cos(angle) * radius
            y2 = cy + math.sin(angle) * radius
            gdraw.line((x1, y1, x2, y2), fill=(90, 129, 181, 150), width=1)
        base = Image.alpha_composite(base, galaxy)
        self._static_layer = base.copy()
        return base

    def _point_angle(self, point: dict[str, Any]) -> float:
        return math.atan2(-float(point["y"]), float(point["x"])) % _TAU

    def _sector_specs(self, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for point in points:
            grouped[point["sector"]].append(point)
        sectors: list[dict[str, Any]] = []
        for name, members in grouped.items():
            sin_sum = sum(math.sin(self._point_angle(point)) for point in members)
            cos_sum = sum(math.cos(self._point_angle(point)) for point in members)
            angle = math.atan2(sin_sum, cos_sum) % _TAU
            owners = Counter(point["owner"] for point in members)
            owner = owners.most_common(1)[0][0]
            sectors.append(
                {
                    "name": name,
                    "members": members,
                    "angle": angle,
                    "owner": owner,
                }
            )
        sectors.sort(key=lambda item: item["angle"])
        if not sectors:
            return sectors
        if len(sectors) == 1:
            sectors[0]["start"] = 0.0
            sectors[0]["end"] = _TAU
            return sectors
        for index, sector in enumerate(sectors):
            previous_angle = sectors[index - 1]["angle"]
            current_angle = sector["angle"]
            next_angle = sectors[(index + 1) % len(sectors)]["angle"]
            before = (current_angle - previous_angle) % _TAU
            after = (next_angle - current_angle) % _TAU
            sector["start"] = current_angle - before / 2
            sector["end"] = current_angle + after / 2
        return sectors

    def _draw_pattern(
        self,
        layer: "Image.Image",
        mask: "Image.Image",
        owner: str,
        color: tuple[int, int, int],
    ) -> None:
        if owner == "Humans":
            return
        pattern = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(pattern)
        spacing = 14
        if owner == "Terminids":
            for y in range(self.plot_top, self.plot_top + self.plot_size, spacing):
                offset = spacing // 2 if (y // spacing) % 2 else 0
                for x in range(
                    self.plot_left + offset,
                    self.plot_left + self.plot_size,
                    spacing,
                ):
                    draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=(*color, 105))
        elif owner == "Automaton":
            start = self.plot_left - self.plot_size
            end = self.plot_left + self.plot_size * 2
            for offset in range(start, end, spacing):
                draw.line(
                    (
                        offset,
                        self.plot_top + self.plot_size,
                        offset + self.plot_size,
                        self.plot_top,
                    ),
                    fill=(*color, 88),
                    width=2,
                )
        elif owner == "Illuminate":
            for offset in range(-self.plot_size, self.plot_size * 2, spacing + 5):
                draw.line(
                    (
                        self.plot_left + offset,
                        self.plot_top,
                        self.plot_left + offset + self.plot_size,
                        self.plot_top + self.plot_size,
                    ),
                    fill=(*color, 72),
                    width=1,
                )
                draw.line(
                    (
                        self.plot_left + offset,
                        self.plot_top + self.plot_size,
                        self.plot_left + offset + self.plot_size,
                        self.plot_top,
                    ),
                    fill=(*color, 56),
                    width=1,
                )
        else:
            return
        pattern.putalpha(
            Image.composite(pattern.getchannel("A"), Image.new("L", pattern.size), mask)
        )
        layer.alpha_composite(pattern)

    def _draw_sector_wedges(
        self,
        img: "Image.Image",
        points: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        sectors = self._sector_specs(points)
        if not sectors:
            return sectors
        cx, cy = self._center
        outer = self.plot_size // 2 - 10
        inner = max(65, int(outer * 0.18))
        circle_mask = self._circle_mask()
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        for sector in sectors:
            owner = sector["owner"]
            color = FACTION_COLORS.get(owner, FACTION_COLORS["Unknown"])
            wedge_mask = Image.new("L", img.size, 0)
            mask_draw = ImageDraw.Draw(wedge_mask)
            start = math.degrees(sector["start"])
            end = math.degrees(sector["end"])
            while end <= start:
                end += 360
            box = (cx - outer, cy - outer, cx + outer, cy + outer)
            mask_draw.pieslice(box, start=start, end=end, fill=255)
            mask_draw.ellipse(
                (cx - inner, cy - inner, cx + inner, cy + inner),
                fill=0,
            )
            wedge_mask = Image.composite(
                wedge_mask, Image.new("L", img.size), circle_mask
            )
            fill = Image.new(
                "RGBA", img.size, (*color, 42 if owner == "Humans" else 58)
            )
            fill.putalpha(
                Image.composite(
                    fill.getchannel("A"), Image.new("L", img.size), wedge_mask
                )
            )
            layer.alpha_composite(fill)
            self._draw_pattern(layer, wedge_mask, owner, color)
            boundary = ImageDraw.Draw(layer)
            for angle in (sector["start"], sector["end"]):
                boundary.line(
                    (
                        cx + math.cos(angle) * inner,
                        cy + math.sin(angle) * inner,
                        cx + math.cos(angle) * outer,
                        cy + math.sin(angle) * outer,
                    ),
                    fill=(*color, 90),
                    width=1,
                )
        img.alpha_composite(layer)
        return sectors

    def _draw_routes(
        self,
        img: "Image.Image",
        points: list[dict[str, Any]],
        coords: dict[int, tuple[int, int]],
    ) -> None:
        route_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(route_layer)
        drawn: set[tuple[int, int]] = set()
        for point in points:
            idx = point["index"]
            if idx is None or idx not in coords:
                continue
            start = coords[idx]
            for waypoint in point["waypoints"]:
                if waypoint not in coords:
                    continue
                key = (min(idx, waypoint), max(idx, waypoint))
                if key in drawn:
                    continue
                drawn.add(key)
                end = coords[waypoint]
                draw.line((*start, *end), fill=(55, 82, 119, 135), width=3)
                draw.line((*start, *end), fill=(122, 164, 211, 135), width=1)
        try:
            glow = route_layer.filter(ImageFilter.GaussianBlur(2))
            img.alpha_composite(glow)
        except Exception:
            pass
        img.alpha_composite(route_layer)

    def _draw_arrow(
        self,
        draw: "ImageDraw.ImageDraw",
        start: tuple[int, int],
        end: tuple[int, int],
        color: tuple[int, int, int, int],
    ) -> None:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.hypot(dx, dy)
        if distance < 10:
            return
        ux, uy = dx / distance, dy / distance
        sx, sy = start[0] + ux * 10, start[1] + uy * 10
        ex, ey = end[0] - ux * 13, end[1] - uy * 13
        draw.line((sx, sy, ex, ey), fill=color, width=3)
        head = 9
        wing = 4.5
        left = (ex - ux * head - uy * wing, ey - uy * head + ux * wing)
        right = (ex - ux * head + uy * wing, ey - uy * head - ux * wing)
        draw.polygon(((ex, ey), left, right), fill=color)

    def _draw_attacks(
        self,
        img: "Image.Image",
        points: list[dict[str, Any]],
        coords: dict[int, tuple[int, int]],
    ) -> None:
        arrows = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(arrows)
        for point in points:
            idx = point["index"]
            if idx is None or idx not in coords:
                continue
            color = FACTION_COLORS.get(point["owner"], FACTION_COLORS["Unknown"])
            for target in point["attacking"]:
                if target in coords:
                    self._draw_arrow(draw, coords[idx], coords[target], (*color, 225))
        try:
            glow = arrows.filter(ImageFilter.GaussianBlur(4))
            glow.putalpha(glow.getchannel("A").point(lambda alpha: alpha // 2))
            img.alpha_composite(glow)
        except Exception:
            pass
        img.alpha_composite(arrows)

    def _biome_palette(self, biome: str) -> tuple[tuple[int, int, int], ...]:
        key = biome.casefold()
        for fragment, palette in _BIOME_PALETTES.items():
            if fragment != "default" and fragment in key:
                return palette
        return _BIOME_PALETTES["default"]

    def _planet_sprite(
        self,
        point: dict[str, Any],
        diameter: int,
        is_super_earth: bool,
    ) -> "Image.Image":
        cache_key = (
            point["index"],
            point["name"],
            point["biome"],
            point["owner"],
            diameter,
            is_super_earth,
        )
        cached = self._sprite_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
        scale = 3
        size = diameter * scale
        sprite = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        pixels = sprite.load()
        radius = size / 2 - scale
        center = (size - 1) / 2
        palette = self._biome_palette(point["biome"])
        owner_color = FACTION_COLORS.get(point["owner"], FACTION_COLORS["Unknown"])
        rnd = random.Random(
            _stable_seed(
                point["index"],
                point["name"],
                point["biome"],
                point["owner"],
            )
        )
        spots = [
            (
                rnd.uniform(-0.62, 0.62),
                rnd.uniform(-0.62, 0.62),
                rnd.uniform(0.09, 0.28),
                rnd.choice((-1, 1)),
            )
            for _ in range(8)
        ]
        light = (-0.58, -0.72, 0.38)
        for y in range(size):
            ny = (y - center) / radius
            for x in range(size):
                nx = (x - center) / radius
                rr = nx * nx + ny * ny
                if rr > 1.0:
                    continue
                nz = math.sqrt(max(0.0, 1.0 - rr))
                illumination = max(0.1, nx * light[0] + ny * light[1] + nz * light[2])
                shade = 0.42 + illumination * 0.68
                base = palette[0]
                for spot_x, spot_y, spot_r, polarity in spots:
                    if (nx - spot_x) ** 2 + (ny - spot_y) ** 2 < spot_r**2:
                        base = palette[1] if polarity > 0 else palette[2]
                        break
                rim = max(0.0, min(1.0, (1.0 - rr) * 4.5))
                tint = 0.16 if is_super_earth else 0.08
                red = int((base[0] * (1 - tint) + owner_color[0] * tint) * shade)
                green = int((base[1] * (1 - tint) + owner_color[1] * tint) * shade)
                blue = int((base[2] * (1 - tint) + owner_color[2] * tint) * shade)
                pixels[x, y] = (
                    max(0, min(255, red)),
                    max(0, min(255, green)),
                    max(0, min(255, blue)),
                    int(255 * rim),
                )
        draw = ImageDraw.Draw(sprite)
        draw.ellipse(
            (scale, scale, size - scale - 1, size - scale - 1),
            outline=(*owner_color, 235),
            width=max(2, scale),
        )
        sprite = sprite.resize((diameter, diameter), Image.Resampling.LANCZOS)
        if len(self._sprite_cache) >= 512:
            self._sprite_cache.pop(next(iter(self._sprite_cache)))
        self._sprite_cache[cache_key] = sprite.copy()
        return sprite

    def _draw_status_marks(
        self,
        draw: "ImageDraw.ImageDraw",
        point: dict[str, Any],
        x: int,
        y: int,
        radius: int,
    ) -> None:
        if point["health_pct"] is not None and (point["active"] or point["defense"]):
            extent = max(3, int(360 * point["health_pct"] / 100))
            draw.arc(
                (x - radius - 5, y - radius - 5, x + radius + 5, y + radius + 5),
                start=-90,
                end=-90 + extent,
                fill=(236, 241, 250, 235),
                width=2,
            )
        if point["active"]:
            draw.ellipse(
                (x - radius - 4, y - radius - 4, x + radius + 4, y + radius + 4),
                outline=(255, 255, 255, 205),
                width=1,
            )
        if point["defense"]:
            shield_y = y - radius - 12
            draw.polygon(
                (
                    (x, shield_y - 5),
                    (x + 6, shield_y - 2),
                    (x + 4, shield_y + 5),
                    (x, shield_y + 9),
                    (x - 4, shield_y + 5),
                    (x - 6, shield_y - 2),
                ),
                fill=(66, 169, 255, 245),
                outline=(221, 242, 255, 255),
            )
        if point["event"]:
            ex, ey = x + radius + 7, y - radius - 7
            draw.polygon(
                ((ex, ey - 6), (ex + 6, ey), (ex, ey + 6), (ex - 6, ey)),
                fill=(245, 182, 62, 245),
                outline=(255, 240, 190, 255),
            )
            font = _find_font(9, True)
            if font:
                draw.text(
                    (ex, ey - 1), "!", fill=(24, 25, 29, 255), font=font, anchor="mm"
                )
        if point["dss"]:
            cyan = (46, 224, 255, 255)
            draw.ellipse(
                (x - radius - 9, y - radius - 9, x + radius + 9, y + radius + 9),
                outline=cyan,
                width=2,
            )
            draw.rectangle((x - 2, y - radius - 13, x + 2, y - radius - 9), fill=cyan)
            draw.rectangle((x - 2, y + radius + 9, x + 2, y + radius + 13), fill=cyan)

    def _draw_planets(
        self,
        img: "Image.Image",
        points: list[dict[str, Any]],
        coords: dict[int, tuple[int, int]],
        *,
        compact: bool = False,
    ) -> None:
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        ordered = sorted(
            points,
            key=lambda point: (
                point["index"] == 0 or "super earth" in point["name"].casefold(),
                point["dss"],
                point["active"],
                point["players"],
            ),
        )
        for point in ordered:
            idx = point["index"]
            if idx is not None and idx in coords:
                x, y = coords[idx]
            else:
                x, y = self._map_xy(point["x"], point["y"])
            is_super_earth = idx == 0 or "super earth" in point["name"].casefold()
            diameter = 30 if is_super_earth else (7 if compact else 10)
            if point["active"]:
                diameter += 5 if compact else 4
            if point["dss"]:
                diameter += 3 if compact else 2
            radius = diameter // 2
            color = FACTION_COLORS.get(point["owner"], FACTION_COLORS["Unknown"])
            if is_super_earth:
                glow_radius = 42
                glow_draw.ellipse(
                    (
                        x - glow_radius,
                        y - glow_radius,
                        x + glow_radius,
                        y + glow_radius,
                    ),
                    fill=(64, 160, 255, 130),
                )
            elif point["active"] or point["dss"]:
                glow_radius = 18 if point["active"] else 22
                glow_draw.ellipse(
                    (
                        x - glow_radius,
                        y - glow_radius,
                        x + glow_radius,
                        y + glow_radius,
                    ),
                    fill=(*color, 95),
                )
        try:
            glow = glow.filter(ImageFilter.GaussianBlur(10))
        except Exception:
            pass
        img.alpha_composite(glow)
        draw = ImageDraw.Draw(img)
        for point in ordered:
            idx = point["index"]
            if idx is not None and idx in coords:
                x, y = coords[idx]
            else:
                x, y = self._map_xy(point["x"], point["y"])
            is_super_earth = idx == 0 or "super earth" in point["name"].casefold()
            diameter = 30 if is_super_earth else (7 if compact else 10)
            if point["active"]:
                diameter += 5 if compact else 4
            if point["dss"]:
                diameter += 3 if compact else 2
            radius = diameter // 2
            sprite = self._planet_sprite(point, diameter, is_super_earth)
            img.alpha_composite(sprite, (x - radius, y - radius))
            self._draw_status_marks(draw, point, x, y, radius)
            if is_super_earth:
                font = _find_font(11, True)
                if font:
                    draw.text(
                        (x, y + radius + 9),
                        "超级地球",
                        fill=(210, 232, 255, 245),
                        font=font,
                        anchor="ma",
                        stroke_width=2,
                        stroke_fill=(3, 7, 16, 255),
                    )

    def _draw_sector_labels(
        self,
        draw: "ImageDraw.ImageDraw",
        sectors: list[dict[str, Any]],
    ) -> None:
        font = _find_font(10)
        if not font or len(sectors) > 45:
            return
        cx, cy = self._center
        radius = self.plot_size * 0.39
        for sector in sectors:
            name = str(sector["name"]).upper()
            if not name or name == "UNKNOWN SECTOR":
                continue
            angle = sector["angle"]
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius
            draw.text(
                (x, y),
                name[:22],
                fill=(144, 163, 191, 175),
                font=font,
                anchor="mm",
                stroke_width=2,
                stroke_fill=(5, 9, 20, 200),
            )

    def _draw_faction_labels(
        self,
        draw: "ImageDraw.ImageDraw",
        points: list[dict[str, Any]],
    ) -> None:
        font = _find_font(12, True)
        if not font:
            return
        by_faction: dict[str, list[float]] = defaultdict(list)
        for point in points:
            by_faction[point["owner"]].append(self._point_angle(point))
        cx, cy = self._center
        radius = self.plot_size / 2 + 16
        for faction, angles in by_faction.items():
            if faction == "Unknown" or not angles:
                continue
            sin_sum = sum(math.sin(angle) for angle in angles)
            cos_sum = sum(math.cos(angle) for angle in angles)
            angle = math.atan2(sin_sum, cos_sum)
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius
            color = FACTION_COLORS[faction]
            text = _FACTION_LABELS[faction]
            bbox = draw.textbbox((x, y), text, font=font, anchor="mm")
            x += max(8 - bbox[0], 0) - max(bbox[2] - self.width + 8, 0)
            y += max(self.header_h + 8 - bbox[1], 0) - max(
                bbox[3] - (self.height - self.footer_h - 8),
                0,
            )
            bbox = draw.textbbox((x, y), text, font=font, anchor="mm")
            padded = (bbox[0] - 6, bbox[1] - 3, bbox[2] + 6, bbox[3] + 3)
            draw.rounded_rectangle(
                padded,
                radius=4,
                fill=(5, 9, 19, 225),
                outline=(*color, 210),
                width=1,
            )
            draw.text((x, y), text, fill=(*color, 255), font=font, anchor="mm")

    def _draw_point_labels(
        self,
        draw: "ImageDraw.ImageDraw",
        points: list[dict[str, Any]],
        coords: dict[int, tuple[int, int]],
    ) -> None:
        font = _find_font(13)
        status_font = _find_font(10)
        if not font:
            return
        labels = [
            point
            for point in points
            if point["active"] or point["dss"] or point["defense"] or point["event"]
        ]
        labels.sort(
            key=lambda point: (
                point["dss"],
                point["defense"],
                point["event"],
                point["players"],
            ),
            reverse=True,
        )
        for point in labels[: self.label_top_n]:
            idx = point["index"]
            if idx is not None and idx in coords:
                x, y = coords[idx]
            else:
                x, y = self._map_xy(point["x"], point["y"])
            if idx == 0 or "super earth" in point["name"].casefold():
                continue
            name = point["name"] or f"PLANET {idx if idx is not None else '?'}"
            tx, ty = x + 10, y - 9
            draw.text(
                (tx, ty),
                name,
                fill=(237, 242, 250, 255),
                font=font,
                stroke_width=2,
                stroke_fill=(3, 6, 14, 235),
            )
            if status_font and (point["status"] or point["event_type"]):
                raw_status = point["status"] or point["event_type"]
                status = format_status(raw_status, str(raw_status))
                draw.text(
                    (tx, ty + 16),
                    status[:28],
                    fill=(164, 185, 212, 230),
                    font=status_font,
                    stroke_width=2,
                    stroke_fill=(3, 6, 14, 230),
                )

    def _draw_chrome(
        self,
        image: "Image.Image",
        draw: "ImageDraw.ImageDraw",
        points: list[dict[str, Any]],
        title: str,
        subtitle: str,
    ) -> None:
        font_s = _find_font(12)
        font_m = _find_font(16, True)
        font_l = _find_font(25, True)
        draw.rectangle((0, 0, self.width, self.header_h), fill=(7, 17, 29, 238))
        draw.line((0, self.header_h - 2, self.width, self.header_h - 2), fill=(78, 118, 151, 230), width=2)
        if font_l:
            draw.text((28, 17), "GALACTIC WAR MAP · HELLDIVERS 2", fill=(231, 241, 251, 255), font=font_l)
        if font_s and subtitle:
            text_width = draw.textlength(subtitle, font=font_s)
            draw.text(
                (self.width - text_width - 28, 27),
                subtitle,
                fill=(151, 173, 207, 255),
                font=font_s,
            )

        footer_y = self.height - self.footer_h
        draw.rectangle((0, footer_y - 8, self.width, self.height), fill=(5, 11, 18, 246))
        draw.line((0, footer_y - 8, self.width, footer_y - 8), fill=(84, 120, 145, 230), width=3)
        gap = 12
        margin = 16
        available = self.width - margin * 2 - gap * 2
        left_w = int(available * 0.33)
        center_w = int(available * 0.35)
        right_w = available - left_w - center_w
        panel_top = footer_y + 4
        panel_bottom = self.height - 12
        left_box = (margin, panel_top, margin + left_w, panel_bottom)
        center_box = (left_box[2] + gap, panel_top, left_box[2] + gap + center_w, panel_bottom)
        right_box = (center_box[2] + gap, panel_top, center_box[2] + gap + right_w, panel_bottom)
        for box in (left_box, center_box, right_box):
            draw_chamfered_panel(
                draw,
                box,
                fill=(8, 19, 29, 242),
                outline=(102, 132, 151, 230),
                accent=(111, 171, 202, 230),
                cut=9,
                width=2,
            )
        if font_s:
            draw.text((left_box[0] + 18, panel_top + 8), "ZONE ALLOCATION KEY", fill=(225, 236, 245, 255), font=font_s)
            draw.text((center_box[0] + 18, panel_top + 8), "COMMAND SECTOR READOUT", fill=(225, 236, 245, 255), font=font_s)
            draw.text((right_box[0] + 18, panel_top + 8), "TACTICAL LEGEND", fill=(225, 236, 245, 255), font=font_s)
        draw.line((left_box[0] + 12, panel_top + 28, left_box[2] - 12, panel_top + 28), fill=(75, 106, 125, 180), width=1)
        draw.line((center_box[0] + 12, panel_top + 28, center_box[2] - 12, panel_top + 28), fill=(75, 106, 125, 180), width=1)
        draw.line((right_box[0] + 12, panel_top + 28, right_box[2] - 12, panel_top + 28), fill=(75, 106, 125, 180), width=1)
        legend = [
            ("超级地球", "Humans"),
            ("机器人", "Automaton"),
            ("终结族", "Terminids"),
            ("光能者", "Illuminate"),
        ]
        cell_w = max(60, (left_box[2] - left_box[0] - 24) // 4)
        for index, (name, faction) in enumerate(legend):
            x = left_box[0] + 12 + index * cell_w
            y = panel_top + 38
            color = FACTION_COLORS[faction]
            emblem = asset_image(f"emblems/{'super_earth' if faction == 'Humans' else faction.lower()}.png") if asset_image else None
            if emblem is not None and paste_contain is not None:
                paste_contain(image, emblem, (x + 6, y, cell_w - 12, 48))
            else:
                draw.ellipse((x + cell_w // 2 - 20, y + 2, x + cell_w // 2 + 20, y + 42), fill=(*color, 230), outline=(225, 235, 242, 180), width=2)
            if font_s:
                label_w = draw.textlength(name, font=font_s)
                draw.text((x + (cell_w - label_w) / 2, y + 52), name, fill=(*color, 255), font=font_s)

        active_count = sum(1 for point in points if point["active"])
        defense_count = sum(1 for point in points if point["defense"])
        total_players = sum(point["players"] for point in points)
        cx = center_box[0] + 20
        cy = panel_top + 40
        draw.ellipse((cx, cy, cx + 44, cy + 44), fill=(41, 183, 220, 230), outline=(183, 238, 248, 230), width=2)
        draw.ellipse((cx + 11, cy + 11, cx + 20, cy + 20), fill=(5, 29, 42, 255))
        draw.ellipse((cx + 25, cy + 11, cx + 34, cy + 20), fill=(5, 29, 42, 255))
        draw.rectangle((cx + 15, cy + 29, cx + 29, cy + 35), fill=(5, 29, 42, 255))
        if font_m:
            draw.text((cx + 58, cy + 3), f"ONLINE DIVERS: {total_players:,}", fill=(111, 218, 244, 255), font=font_m)
        if font_s:
            draw.text((center_box[0] + 18, panel_top + 92), f"活跃战役 {active_count}", fill=(235, 218, 121, 255), font=font_s)
            draw.text((center_box[0] + center_w // 3 + 8, panel_top + 92), f"防御事件 {defense_count}", fill=(235, 218, 121, 255), font=font_s)
            draw.text((center_box[0] + center_w * 2 // 3, panel_top + 92), f"争夺星球 {len(points)}", fill=(235, 218, 121, 255), font=font_s)
        if font_s:
            icons = [
                ("活跃", (235, 239, 244), "ring"),
                ("防御", (68, 153, 238), "shield"),
                ("事件", (245, 190, 62), "diamond"),
                ("DSS", (43, 213, 220), "target"),
                ("进攻", (239, 92, 87), "arrow"),
            ]
            icon_w = max(48, (right_box[2] - right_box[0] - 20) // len(icons))
            for index, (label, color, kind) in enumerate(icons):
                x = right_box[0] + 10 + index * icon_w
                center_x = x + icon_w // 2
                center_y = panel_top + 58
                if kind == "ring":
                    draw.ellipse((center_x - 17, center_y - 17, center_x + 17, center_y + 17), outline=(*color, 255), width=4)
                elif kind == "shield":
                    draw.polygon([(center_x, center_y - 20), (center_x + 17, center_y - 12), (center_x + 13, center_y + 13), (center_x, center_y + 21), (center_x - 13, center_y + 13), (center_x - 17, center_y - 12)], outline=(*color, 255))
                elif kind == "diamond":
                    draw.polygon([(center_x, center_y - 19), (center_x + 19, center_y), (center_x, center_y + 19), (center_x - 19, center_y)], outline=(*color, 255))
                elif kind == "target":
                    draw.ellipse((center_x - 17, center_y - 17, center_x + 17, center_y + 17), outline=(*color, 255), width=3)
                    draw.line((center_x - 23, center_y, center_x + 23, center_y), fill=(*color, 255), width=2)
                    draw.line((center_x, center_y - 23, center_x, center_y + 23), fill=(*color, 255), width=2)
                else:
                    draw.polygon([(center_x - 20, center_y - 12), (center_x + 18, center_y), (center_x - 20, center_y + 12), (center_x - 8, center_y)], fill=(*color, 255))
                label_w = draw.textlength(label, font=font_s)
                draw.text((center_x - label_w / 2, panel_top + 91), label, fill=(202, 214, 226, 255), font=font_s)

    def _prune_cache(self, keep: Path) -> None:
        cutoff = time.time() - self.cache_ttl
        try:
            for path in self.output_dir.glob("map_v5_*.png"):
                if path != keep and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def _reference_base(self):
        if self.visual_mode != "uploaded_reference" or asset_image is None:
            return None
        try:
            reference = asset_image("user/map.png")
            if reference is None:
                return None
            fitted = reference.resize(
                (self.plot_size, self.plot_size), Image.Resampling.LANCZOS
            )
            canvas = cockpit_backdrop(
                "user/map_template.png",
                (self.width, self.height),
                blur=3.5,
                darkness=72,
                tint=(3, 10, 16),
            )
            draw_scanlines(canvas, step=5, alpha=8)
            # The uploaded map already contains the high-fidelity sector topology,
            # labels, and route network. Keep it as the base plate and alpha-composite
            # live status layers above it.
            canvas.alpha_composite(fitted, (self.plot_left, self.plot_top))
            return canvas
        except Exception:
            return None

    def render(
        self,
        points: list[dict[str, Any]],
        *,
        force: bool = False,
        title: str = "银河战争地图 · HELLDIVERS 2",
        subtitle: str = "",
    ) -> Path | None:
        if not self.available:
            logger.warning("[HD2] Pillow not installed; cannot render map")
            return None

        normalised = self._normalise_points(points)
        fingerprint = self._fingerprint(normalised, title, subtitle)
        out = self.output_dir / f"map_v5_{fingerprint}.png"
        if not force and out.exists():
            self._last_path = out
            self._last_ts = time.time()
            self._last_fingerprint = fingerprint
            return out

        temp = out.with_name(f".{out.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            img = self._reference_base()
            reference_mode = img is not None
            if img is None:
                img = self._build_static_layer().convert("RGBA")
            # The uploaded map already includes high-fidelity sectors, borders, labels,
            # and supply routes. In reference mode only live status is overlaid; drawing
            # derived wedges/routes again would create an unreadable double network.
            sectors = [] if reference_mode else self._draw_sector_wedges(img, normalised)
            coords: dict[int, tuple[int, int]] = {}
            for point in normalised:
                idx = point["index"]
                if idx is None:
                    continue
                if idx == 0 or "super earth" in point["name"].casefold():
                    coords[idx] = self._center
                else:
                    coords[idx] = self._map_xy(point["x"], point["y"])

            if not reference_mode:
                self._draw_routes(img, normalised, coords)
            self._draw_attacks(img, normalised, coords)
            self._draw_planets(img, normalised, coords, compact=reference_mode)
            draw = ImageDraw.Draw(img)
            if not reference_mode:
                self._draw_sector_labels(draw, sectors)
                self._draw_faction_labels(draw, normalised)
            self._draw_point_labels(draw, normalised, coords)
            self._draw_chrome(img, draw, normalised, title, subtitle)

            img.convert("RGB").save(temp, format="PNG", optimize=True)
            temp.replace(out)
            self._last_path = out
            self._last_ts = time.time()
            self._last_fingerprint = fingerprint
            self._prune_cache(out)
            return out
        except Exception as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            logger.exception(f"[HD2] map render failed: {exc}")
            return None
