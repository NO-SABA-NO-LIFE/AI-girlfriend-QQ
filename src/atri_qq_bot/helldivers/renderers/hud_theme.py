"""Shared cockpit HUD primitives for Pillow report renderers."""

from __future__ import annotations

from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .visual_assets import asset_image


def cockpit_backdrop(
    asset_name: str,
    size: tuple[int, int],
    *,
    blur: float = 5.0,
    darkness: int = 122,
    tint: tuple[int, int, int] = (4, 15, 24),
) -> Image.Image:
    """Create a subdued cockpit backplate from a user-provided visual reference."""

    source = asset_image(asset_name)
    if source is None:
        return Image.new("RGBA", size, (*tint, 255))
    fitted = ImageOps.fit(source.convert("RGBA"), size, method=Image.Resampling.LANCZOS)
    if blur > 0:
        fitted = fitted.filter(ImageFilter.GaussianBlur(blur))
    shade = Image.new("RGBA", size, (*tint, max(0, min(255, darkness))))
    return Image.alpha_composite(fitted, shade)


def chamfered_polygon(box: tuple[int, int, int, int], cut: int = 12) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = box
    cut = max(0, min(cut, (x1 - x0) // 3, (y1 - y0) // 3))
    return [
        (x0 + cut, y0),
        (x1 - cut, y0),
        (x1, y0 + cut),
        (x1, y1 - cut),
        (x1 - cut, y1),
        (x0 + cut, y1),
        (x0, y1 - cut),
        (x0, y0 + cut),
    ]


def draw_chamfered_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    fill: tuple[int, ...],
    outline: tuple[int, ...],
    accent: tuple[int, ...] | None = None,
    cut: int = 12,
    width: int = 2,
) -> None:
    points = chamfered_polygon(box, cut)
    draw.polygon(points, fill=fill)
    draw.line(points + [points[0]], fill=outline, width=width, joint="curve")
    x0, y0, x1, _ = box
    if accent is not None:
        draw.line((x0 + cut + 2, y0 + 2, x1 - cut - 2, y0 + 2), fill=accent, width=2)


def draw_segmented_bar(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    progress: float,
    *,
    color: tuple[int, ...],
    track: tuple[int, ...] = (45, 57, 68, 230),
    segments: int = 48,
    gap: int = 2,
) -> None:
    x0, y0, x1, y1 = box
    progress = max(0.0, min(1.0, float(progress)))
    draw.rectangle(box, fill=track, outline=(93, 111, 126, 170), width=1)
    usable = max(1, x1 - x0 - 4)
    segment_w = max(1, (usable - gap * (segments - 1)) // segments)
    filled = round(progress * segments)
    for index in range(segments):
        left = x0 + 2 + index * (segment_w + gap)
        right = min(x1 - 2, left + segment_w)
        if right <= left:
            break
        fill = color if index < filled else (23, 31, 39, 210)
        draw.rectangle((left, y0 + 2, right, y1 - 2), fill=fill)


def draw_tech_header(
    image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    accent: tuple[int, int, int] = (93, 190, 239),
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    x0, y0, x1, y1 = box
    for y in range(y0, y1 + 1):
        ratio = (y - y0) / max(1, y1 - y0)
        shade = tuple(round(51 - ratio * 26 + channel * 0.08) for channel in accent)
        draw.line((x0, y, x1, y), fill=(*shade, 245), width=1)
    draw_chamfered_panel(
        draw,
        box,
        fill=(0, 0, 0, 0),
        outline=(111, 139, 156, 230),
        accent=(*accent, 210),
        cut=13,
    )
    for offset in range(0, x1 - x0, 42):
        draw.line((x0 + offset, y1 - 18, min(x1, x0 + offset + 20), y1 - 18), fill=(110, 142, 160, 75), width=1)


def draw_scanlines(image: Image.Image, *, step: int = 4, alpha: int = 20) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(0, image.height, max(2, step)):
        draw.line((0, y, image.width, y), fill=(210, 235, 245, alpha), width=1)
    image.alpha_composite(overlay)


def paste_glow(image: Image.Image, overlay: Image.Image, position: tuple[int, int], color: tuple[int, int, int], radius: int = 10) -> None:
    sprite = overlay.convert("RGBA")
    alpha = sprite.getchannel("A")
    glow = Image.new("RGBA", sprite.size, (*color, 0))
    glow.putalpha(alpha.filter(ImageFilter.GaussianBlur(radius)).point(lambda value: min(190, value)))
    image.alpha_composite(glow, position)
    image.alpha_composite(sprite, position)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
