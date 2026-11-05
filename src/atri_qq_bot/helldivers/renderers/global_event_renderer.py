"""Dedicated one-event-per-image renderer for Helldivers global events.

The renderer consumes ordinary mappings and does not mutate API data. Callers may pass
translated display fields separately, an optional raw war clock for relative expiry,
and a local media manifest keyed by event media IDs. No remote media is fetched here.
"""

from __future__ import annotations

import importlib
import math
import os
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from .visual_assets import prune_render_outputs
from typing import Any, Literal

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

EventTreatment = Literal["SUCCESS", "FAILURE", "BRIEFING"]
WIDTH = 1040
HERO_HEIGHT = 390
CARD_MARGIN = 30
CONTENT_PAD = 42
MAX_MESSAGE_LINES = 22

BG = (12, 15, 21)
PANEL = (28, 31, 38)
TEXT = (240, 242, 246)
MUTED = (151, 158, 171)
DIVIDER = (61, 66, 77)
YELLOW = (248, 202, 40)
TREATMENTS: dict[EventTreatment, tuple[int, int, int]] = {
    "SUCCESS": (82, 204, 125),
    "FAILURE": (232, 84, 88),
    "BRIEFING": (98, 174, 232),
}
_FONT_CACHE: dict[tuple[int, bool], Any] = {}

_SUCCESS_WORDS = (
    "success",
    "successful",
    "victory",
    "victorious",
    "complete",
    "completed",
    "secured",
    "liberated",
    "胜利",
    "成功",
    "完成",
    "解放",
)
_FAILURE_WORDS = (
    "failure",
    "failed",
    "defeat",
    "defeated",
    "lost",
    "loss",
    "失败",
    "战败",
    "失守",
)


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
        return float(draw.textlength(text, font=font))
    except (AttributeError, TypeError, ValueError):
        return len(text) * 8.0


def _wrap(draw: Any, text: str, font: Any, max_width: int, max_lines: int) -> list[str]:
    # 先完整折行再裁剪：省略号只在真的丢弃了内容（行数超出上限）时出现，
    # 内容恰好铺满时不加。折行按行独立，截断前缀与逐行提前 break 等价。
    output: list[str] = []
    for raw_line in str(text or "").replace("\r", "").split("\n"):
        raw_line = " ".join(raw_line.split())
        if not raw_line:
            if output and output[-1] != "":
                output.append("")
            continue
        current = ""
        for character in raw_line:
            candidate = current + character
            if not current or _text_width(draw, candidate, font) <= max_width:
                current = candidate
            else:
                output.append(current.rstrip())
                current = character.lstrip()
        if current:
            output.append(current.rstrip())
    if len(output) > max_lines:
        output = output[:max_lines]
        final = output[-1].rstrip("… ")
        while final and _text_width(draw, final + "…", font) > max_width:
            final = final[:-1]
        output[-1] = final.rstrip() + "…"
    return output or [""]


def classify_event(
    event: Mapping[str, Any], title: str | None = None
) -> EventTreatment:
    """Classify only from explicit status fields and normalized title wording."""

    explicit = str(
        event.get("treatment")
        or event.get("result")
        or event.get("status")
        or event.get("eventStatus")
        or ""
    ).casefold()
    if explicit in {"success", "successful", "victory", "completed", "complete"}:
        return "SUCCESS"
    if explicit in {"failure", "failed", "defeat", "lost"}:
        return "FAILURE"
    normalized = " ".join(
        str(title if title is not None else event.get("title") or "").split()
    ).casefold()
    if any(word in normalized for word in _FAILURE_WORDS):
        return "FAILURE"
    if any(word in normalized for word in _SUCCESS_WORDS):
        return "SUCCESS"
    return "BRIEFING"


def format_event_expiry(event: Mapping[str, Any], war_time: Any | None = None) -> str:
    """Format event expiry, preferring ``expireTime - war_time`` for raw war clocks."""

    expiry = event.get("expireTime")
    if expiry is None or expiry == "":
        expiry = event.get("endTime") or event.get("expiresAt")
    if expiry is None or expiry == "":
        return ""

    delta = _relative_seconds(expiry, war_time)
    if delta is not None:
        if delta <= 0:
            return "已结束"
        return f"剩余 {_duration(delta)}"

    if (
        war_time is None
        and isinstance(expiry, (int, float))
        and float(expiry) < 1_000_000_000
    ):
        return ""
    timestamp = _as_datetime(expiry)
    if timestamp is not None:
        now = (
            _as_datetime(war_time)
            if war_time is not None
            else datetime.now(timezone.utc)
        )
        if now is not None:
            seconds = int((timestamp - now).total_seconds())
            if seconds <= 0:
                return "已结束"
            return f"剩余 {_duration(seconds)}"
        return timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
    return ""


def _relative_seconds(expiry: Any, war_time: Any | None) -> int | None:
    if war_time is None:
        return None
    try:
        end_number = float(expiry)
        war_number = float(war_time)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(end_number) or not math.isfinite(war_number):
        return None
    return int(end_number - war_number)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(
            timezone.utc
        )
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分钟"
    return f"{max(1, minutes)}分钟"


def _event_id(event: Mapping[str, Any]) -> str:
    value = (
        event.get("eventId")
        or event.get("id32")
        or event.get("id")
        or event.get("event_id")
        or "UNKNOWN"
    )
    return str(value)


def _translated_value(
    event: Mapping[str, Any],
    translated: Mapping[str, Any] | None,
    *keys: str,
) -> str:
    for source in (translated or {}, event):
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip():
                return " ".join(str(value).replace("\x00", "").split())
    return ""


def _load_image_path(value: Any):
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


def _event_media(
    event: Mapping[str, Any],
    media_manifest: Mapping[Any, Any] | None,
    flag_asset: Any | None,
):
    direct_keys = ("local_media", "media_path", "hero_path", "image_path", "hero")
    for key in direct_keys:
        image = _load_image_path(event.get(key))
        if image is not None:
            return image
    if media_manifest:
        for key in ("introMediaId32", "outroMediaId32", "portraitId32", "mediaId32"):
            media_id = event.get(key)
            if media_id is None:
                continue
            for manifest_key in (media_id, str(media_id)):
                if manifest_key in media_manifest:
                    image = _load_image_path(media_manifest[manifest_key])
                    if image is not None:
                        return image
    image = _load_image_path(flag_asset)
    if image is not None:
        return image
    return _load_flag_from_optional_assets()


def _load_flag_from_optional_assets():
    if Image is None:
        return None
    package = __package__
    module_names = ([f"{package}.visual_assets"] if package else []) + ["visual_assets"]
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ValueError):
            continue
        for name in (
            "global_events/super_earth_flag.png",
            "global_events/super_earth_flag",
            "emblems/super_earth.png",
            "emblems/super_earth",
        ):
            for function_name in ("load_asset", "asset_image", "get_asset"):
                function = getattr(module, function_name, None)
                if not callable(function):
                    continue
                try:
                    image = function(name)
                except (OSError, TypeError, ValueError):
                    continue
                loaded = _load_image_path(image)
                if loaded is not None:
                    return loaded
    return None


def _fallback_flag(size: tuple[int, int]):
    width, height = size
    image = Image.new("RGBA", size, (20, 57, 88, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    for y in range(height):
        amount = y / max(1, height - 1)
        draw.line(
            (0, y, width, y),
            fill=(19, int(75 - 32 * amount), int(116 - 45 * amount), 255),
        )
    for index in range(7):
        top = int(height * (0.12 + index * 0.125))
        draw.polygon(
            ((0, top), (width, top - 28), (width, top + 28), (0, top + 56)),
            fill=(230, 239, 246, 18 if index % 2 else 26),
        )
    cx, cy = width // 2, height // 2
    radius = min(width, height) // 4
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=(224, 240, 250, 225),
        width=6,
    )
    draw.ellipse(
        (cx - radius + 16, cy - radius + 16, cx + radius - 16, cy + radius - 16),
        outline=(120, 191, 231, 210),
        width=3,
    )
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        r = radius * (0.63 if index % 2 == 0 else 0.27)
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(points, fill=(232, 244, 251, 235))
    return image


def _hero_image(source: Any | None, size: tuple[int, int]):
    source = source if source is not None else _fallback_flag(size)
    fitted = ImageOps.fit(
        source.convert("RGBA"),
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    shade = Image.new("RGBA", size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade, "RGBA")
    for y in range(size[1]):
        alpha = int(185 * (y / max(1, size[1] - 1)) ** 2)
        shade_draw.line((0, y, size[0], y), fill=(4, 8, 14, alpha))
    return Image.alpha_composite(fitted, shade)


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
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return destination


def render_global_event(
    event: Mapping[str, Any],
    output_path: str | Path,
    *,
    war_time: Any | None = None,
    translated: Mapping[str, Any] | None = None,
    display_fields: Mapping[str, Any] | None = None,
    media_manifest: Mapping[Any, Any] | None = None,
    flag_asset: Any | None = None,
    source_label: str = "api.helldivers2.dev",
) -> Path:
    """Render exactly one global event to ``output_path``."""

    if Image is None or ImageDraw is None or ImageOps is None:
        raise RuntimeError("Pillow is required to render global events")
    if not isinstance(event, Mapping):
        raise TypeError("event must be a mapping")
    translated_fields = display_fields if display_fields is not None else translated
    title = (
        _translated_value(event, translated_fields, "title", "display_title")
        or "全局事件"
    )
    message = (
        _translated_value(
            event,
            translated_fields,
            "message",
            "display_message",
            "body",
            "description",
        )
        or "暂无事件简报。"
    )
    # 状态必须依据 API 原始标题/显式字段；中文译名不用于判定 SUCCESS/FAILURE。
    treatment = classify_event(event)
    explicit_treatment = _translated_value(
        event, translated_fields, "treatment", "status_label"
    )
    treatment_labels = {"SUCCESS": "成功", "FAILURE": "失败", "BRIEFING": "事件简报"}
    treatment_label = explicit_treatment or treatment_labels[treatment]
    accent = TREATMENTS[treatment]
    expiry = format_event_expiry(event, war_time)

    probe = Image.new("RGB", (10, 10))
    probe_draw = ImageDraw.Draw(probe)
    title_font = _font(34, bold=True)
    treatment_font = _font(58, bold=True)
    body_font = _font(24)
    meta_font = _font(17)
    title_lines = _wrap(
        probe_draw, title, title_font, WIDTH - CARD_MARGIN * 2 - CONTENT_PAD * 2, 3
    )
    body_lines = _wrap(
        probe_draw,
        message,
        body_font,
        WIDTH - CARD_MARGIN * 2 - CONTENT_PAD * 2,
        MAX_MESSAGE_LINES,
    )
    body_line_h = 37
    title_line_h = 46
    content_height = (
        42
        + 72
        + len(title_lines) * title_line_h
        + 24
        + len(body_lines) * body_line_h
        + (54 if expiry else 20)
        + 66
    )
    height = CARD_MARGIN * 2 + HERO_HEIGHT + content_height
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    card_box = (CARD_MARGIN, CARD_MARGIN, WIDTH - CARD_MARGIN, height - CARD_MARGIN)
    draw.rounded_rectangle(
        card_box, radius=26, fill=PANEL, outline=(50, 55, 65), width=2
    )
    hero_source = _event_media(event, media_manifest, flag_asset)
    hero = _hero_image(hero_source, (WIDTH - CARD_MARGIN * 2, HERO_HEIGHT))
    hero_mask = Image.new("L", hero.size, 0)
    ImageDraw.Draw(hero_mask).rounded_rectangle(
        (0, 0, hero.width, hero.height + 28), radius=26, fill=255
    )
    hero.putalpha(hero_mask)
    image.paste(hero, (CARD_MARGIN, CARD_MARGIN), hero)
    hero.close()
    if hero_source is not None:
        hero_source.close()

    content_left = CARD_MARGIN + CONTENT_PAD
    content_right = WIDTH - CARD_MARGIN - CONTENT_PAD
    y = CARD_MARGIN + HERO_HEIGHT + 34
    draw.rectangle(
        (
            CARD_MARGIN,
            CARD_MARGIN + HERO_HEIGHT - 4,
            CARD_MARGIN + 9,
            height - CARD_MARGIN,
        ),
        fill=YELLOW,
    )
    draw.text((content_left, y), treatment_label, font=treatment_font, fill=accent)
    y += 76
    for line in title_lines:
        draw.text((content_left, y), line, font=title_font, fill=TEXT)
        y += title_line_h
    y += 10
    draw.line((content_left, y, content_right, y), fill=DIVIDER, width=2)
    y += 25
    for line in body_lines:
        draw.text((content_left, y), line, font=body_font, fill=TEXT if line else MUTED)
        y += body_line_h
    y += 12
    if expiry:
        badge_width = int(_text_width(draw, expiry, meta_font)) + 34
        draw.rounded_rectangle(
            (content_left, y, content_left + badge_width, y + 38),
            radius=19,
            fill=(41, 46, 55),
            outline=accent,
            width=1,
        )
        draw.text((content_left + 17, y + 8), expiry, font=meta_font, fill=accent)
        y += 54
    draw.line(
        (
            content_left,
            height - CARD_MARGIN - 50,
            content_right,
            height - CARD_MARGIN - 50,
        ),
        fill=DIVIDER,
        width=1,
    )
    footer = f"事件 ID  {_event_id(event)}   ·   {source_label}"
    draw.text(
        (content_left, height - CARD_MARGIN - 35), footer, font=meta_font, fill=MUTED
    )

    destination = Path(output_path)
    try:
        result = _write_png_atomic(image, destination)
    finally:
        image.close()
        probe.close()
    prune_render_outputs(destination.parent)
    return result


class GlobalEventRenderer:
    """Directory-oriented wrapper that emits one PNG for every event."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        media_manifest: Mapping[Any, Any] | None = None,
        flag_asset: Any | None = None,
        source_label: str = "api.helldivers2.dev",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.media_manifest = media_manifest
        self.flag_asset = flag_asset
        self.source_label = source_label

    @property
    def available(self) -> bool:
        return Image is not None

    def render(
        self,
        event: Mapping[str, Any],
        *,
        war_time: Any | None = None,
        translated: Mapping[str, Any] | None = None,
        display_fields: Mapping[str, Any] | None = None,
        filename: str | None = None,
    ) -> Path:
        name = (
            filename or f"global_event_{_event_id(event)}_{uuid.uuid4().hex[:10]}.png"
        )
        if not name.lower().endswith(".png"):
            name += ".png"
        return render_global_event(
            event,
            self.output_dir / Path(name).name,
            war_time=war_time,
            translated=translated,
            display_fields=display_fields,
            media_manifest=self.media_manifest,
            flag_asset=self.flag_asset,
            source_label=self.source_label,
        )

    def render_many(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        war_time: Any | None = None,
        translations: Mapping[Any, Mapping[str, Any]] | None = None,
    ) -> list[Path]:
        paths: list[Path] = []
        for event in events:
            if not isinstance(event, Mapping):
                continue
            event_id = _event_id(event)
            translated = None
            if translations:
                translated = translations.get(event_id) or translations.get(
                    event.get("eventId")
                )
            paths.append(self.render(event, war_time=war_time, translated=translated))
        return paths


__all__ = [
    "EventTreatment",
    "GlobalEventRenderer",
    "classify_event",
    "format_event_expiry",
    "render_global_event",
]
