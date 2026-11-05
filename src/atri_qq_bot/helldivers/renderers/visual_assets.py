"""Shared Pillow helpers for the plugin's original visual asset library.

SVG files under :mod:`assets` are editable source artwork. Runtime renderers load the
matching pre-rendered PNG files so Pillow remains the only image dependency.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Sequence, TypeAlias

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .asset_resolver import AssetResolver, AssetResolverError

logger = logging.getLogger(__name__)

RGB: TypeAlias = tuple[int, int, int]
RGBA: TypeAlias = tuple[int, int, int, int]
Color: TypeAlias = RGB | RGBA | str
Font: TypeAlias = ImageFont.FreeTypeFont | ImageFont.ImageFont
FitMode: TypeAlias = Literal["contain", "cover"]

ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets"
_MANIFEST_PATH: Path | None = None
_FONT_TOKEN_RE = re.compile(r"\s+|[A-Za-z0-9][A-Za-z0-9_./:%+\-]*|.", re.DOTALL)

_LOGICAL_ASSET_IDS = {
    "emblems/super_earth.png": "emblem.super_earth",
    "emblems/automaton.png": "emblem.automaton",
    "emblems/terminids.png": "emblem.terminids",
    "emblems/illuminate.png": "emblem.illuminate",
    # The user DSS upload is a hero/reference image, not a compact transparent emblem.
    "emblems/dss.png": "emblem.dss",
    "dss/dss_wireframe.png": "dss.wireframe",
    "user/map.png": "map.reference",
    "user/map_template.png": "template.map",
    "user/warfront_template.png": "template.warfront",
    "user/steam_hero.png": "steam.hero",
    "global_events/super_earth_flag.png": "global_event.default_flag",
    "tactical/eagle_storm.png": "tactical.eagle_storm",
    "tactical/heavy_ordnance.png": "tactical.heavy_ordnance",
    "tactical/orbital_blockade.png": "tactical.orbital_blockade",
    "tactical/orbital_napalm.png": "tactical.orbital_napalm",
}


@lru_cache(maxsize=1)
def _asset_resolver() -> AssetResolver | None:
    try:
        return AssetResolver(asset_root=ASSET_ROOT, manifest_path=_MANIFEST_PATH)
    except (AssetResolverError, OSError) as exc:
        logger.warning("[HD2] asset resolver 初始化失败，登记资产全部停用: %s", exc)
        return None


def configure_asset_manifest(path: str | Path | None) -> None:
    """Use an optional administrator manifest for subsequent asset lookups."""
    global _MANIFEST_PATH
    _MANIFEST_PATH = Path(path).expanduser() if path else None
    _asset_resolver.cache_clear()

_CJK_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)
_CJK_BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)
_NUMBER_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def asset_path(name: str | Path, *, extension: str = ".png") -> Path:
    """Resolve an asset below :data:`ASSET_ROOT` and reject path traversal."""
    relative = Path(name)
    if relative.is_absolute():
        candidate = relative.resolve()
    else:
        if extension and not relative.suffix:
            relative = relative.with_suffix(extension)
        candidate = (ASSET_ROOT / relative).resolve()
    root = ASSET_ROOT.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"asset path escapes asset root: {name}")
    return candidate


@lru_cache(maxsize=96)
def _load_cached(path: str, modified_ns: int) -> Image.Image:
    del modified_ns
    with Image.open(path) as source:
        image = source.convert("RGBA")
        image.load()
    return image


def _crop_alpha(image: Image.Image, *, padding: int = 0) -> Image.Image:
    """Trim transparent margins while retaining a small optical-safe padding."""
    source = image.convert("RGBA")
    bbox = source.getchannel("A").getbbox()
    if bbox is None:
        return source
    left, top, right, bottom = bbox
    padding = max(0, int(padding))
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(source.width, right + padding)
    bottom = min(source.height, bottom + padding)
    return source.crop((left, top, right, bottom))


def _add_safe_margin(image: Image.Image, ratio: float = 0.06) -> Image.Image:
    """Add transparent optical padding so edge-touching emblems are never clipped."""
    source = _crop_alpha(image)
    padding = max(4, round(max(source.size) * max(0.0, float(ratio))))
    canvas = Image.new(
        "RGBA",
        (source.width + padding * 2, source.height + padding * 2),
        (0, 0, 0, 0),
    )
    canvas.alpha_composite(source, (padding, padding))
    return canvas


def _prepare_super_earth(image: Image.Image) -> Image.Image:
    """Keep the crest and stars, excluding the uploaded wordmark below them."""
    source = image.convert("RGBA")
    # The supplied crest has two blank separator rows before the SUPER EARTH wordmark.
    # Detect the last occupied row group and drop it instead of relying on one filename's
    # exact dimensions.
    alpha = source.getchannel("A")
    occupied = []
    for y in range(source.height):
        if alpha.crop((0, y, source.width, y + 1)).getbbox() is not None:
            occupied.append(y)
    if occupied:
        groups: list[tuple[int, int]] = []
        start = previous = occupied[0]
        for y in occupied[1:]:
            if y > previous + 1:
                groups.append((start, previous + 1))
                start = y
            previous = y
        groups.append((start, previous + 1))
        # The wordmark is a short, detached final group. Preserve detached star rows.
        if len(groups) >= 2:
            final_start, final_end = groups[-1]
            if final_end - final_start <= max(28, source.height // 8):
                source = source.crop((0, 0, source.width, final_start))
    return _crop_alpha(source, padding=3)


def _prepare_dss(image: Image.Image) -> Image.Image:
    """Convert the uploaded black-background DSS render into transparent line art."""
    source = image.convert("RGB")
    luminance = ImageOps.grayscale(source)
    # Remove the uploaded dark-gray border/background more aggressively. The white
    # station silhouette survives while the rectangular halo disappears on dark cards.
    threshold = 42
    alpha = luminance.point(
        lambda value: 0
        if value <= threshold
        else min(255, int(((value - threshold) / (255 - threshold)) ** 0.68 * 255))
    )
    result = Image.new("RGBA", source.size, (220, 244, 255, 0))
    result.putalpha(alpha)
    return _add_safe_margin(result, 0.025)


def _prepare_logical_asset(key: str, image: Image.Image) -> Image.Image:
    """Keep manifest-controlled artwork pixel-identical apart from RGBA conversion."""
    del key
    return image.convert("RGBA")


def load_asset(name: str | Path, *, copy: bool = True) -> Image.Image:
    """Load a pre-rendered PNG with a modification-aware decode cache.

    A copy is returned by default so callers can safely resize or draw on it without
    mutating the cached master image.
    """
    path = asset_path(name)
    stat = path.stat()
    image = _load_cached(str(path), stat.st_mtime_ns)
    return image.copy() if copy else image


def asset_image(name: str | Path) -> Image.Image | None:
    """Load a verified logical asset, preferring manifest-declared user artwork."""
    key = str(name).replace("\\", "/").lstrip("/")
    logical_id = _LOGICAL_ASSET_IDS.get(key)
    resolver = _asset_resolver()
    if logical_id:
        if resolver is None:
            return None
        try:
            resolved = resolver.resolve_local(logical_id)
            if resolved is not None:
                return _prepare_logical_asset(key, resolver.load_image(resolved))
        except (AssetResolverError, OSError, ValueError):
            pass
        logger.warning(
            "[HD2] 资产未通过 manifest 校验（sha256 不匹配或文件缺失），已停用并回退: %s (logical_id=%s)",
            key,
            logical_id,
        )
        # Manifest-controlled assets must not bypass checksum and type validation.
        return None
    # Non-manifest assets such as defense/major-order emblems remain bundled paths.
    try:
        return _prepare_logical_asset(key, load_asset(key))
    except (FileNotFoundError, OSError, ValueError):
        return None


def asset_sha256(name: str | Path) -> str:
    """Return the verified SHA-256 for a logical asset, or an empty string."""
    key = str(name).replace("\\", "/").lstrip("/")
    logical_id = _LOGICAL_ASSET_IDS.get(key)
    if logical_id:
        resolver = _asset_resolver()
        if resolver is None:
            return ""
        try:
            resolved = resolver.resolve_local(logical_id)
            if resolved is None:
                return ""
            path = resolved.path
        except (AssetResolverError, OSError, ValueError):
            return ""
    else:
        try:
            path = asset_path(key)
        except ValueError:
            return ""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def paste_contain(
    base: Image.Image,
    overlay: Image.Image,
    box: tuple[int, int, int, int],
    *,
    opacity: float = 1.0,
) -> Image.Image:
    """Contain-fit an image into ``(x, y, width, height)`` and alpha-composite it."""
    x, y, width, height = box
    fitted = fit_image(overlay, (width, height), mode="contain")
    if opacity < 1.0:
        alpha = fitted.getchannel("A").point(
            lambda value: round(value * _clamp01(opacity))
        )
        fitted.putalpha(alpha)
    # ``paste`` 会直接修改 RGB/RGBA 原画布，调用方无需接收新对象。
    base.paste(fitted, (x, y), fitted)
    return base


def clear_asset_cache() -> None:
    """Clear decoded assets, fonts, and procedural planet sprites."""
    _load_cached.cache_clear()
    _load_font_cached.cache_clear()
    _procedural_planet_cached.cache_clear()


def fit_image(
    image: Image.Image,
    size: tuple[int, int],
    *,
    mode: FitMode = "contain",
    align: tuple[float, float] = (0.5, 0.5),
    background: Color = (0, 0, 0, 0),
    resample: Image.Resampling = Image.Resampling.LANCZOS,
) -> Image.Image:
    """Return an exact-size RGBA image using contain or cover fitting."""
    width, height = (max(1, int(value)) for value in size)
    source = image.convert("RGBA")
    centering = (_clamp01(align[0]), _clamp01(align[1]))
    if mode == "cover":
        return ImageOps.fit(
            source,
            (width, height),
            method=resample,
            centering=centering,
        )
    if mode != "contain":
        raise ValueError(f"unsupported fit mode: {mode}")
    fitted = ImageOps.contain(source, (width, height), method=resample)
    canvas = Image.new("RGBA", (width, height), background)
    x = round((width - fitted.width) * centering[0])
    y = round((height - fitted.height) * centering[1])
    canvas.alpha_composite(fitted, (x, y))
    return canvas


def transparent_paste(
    base: Image.Image,
    overlay: Image.Image,
    position: tuple[int, int],
    *,
    anchor: Literal["lt", "center", "rt", "lb", "rb"] = "lt",
    opacity: float = 1.0,
) -> Image.Image:
    """Alpha-composite ``overlay`` and return an RGBA target image."""
    target = base if base.mode == "RGBA" else base.convert("RGBA")
    sprite = overlay.convert("RGBA")
    opacity = _clamp01(opacity)
    if opacity < 1.0:
        alpha = sprite.getchannel("A").point(lambda value: round(value * opacity))
        sprite.putalpha(alpha)
    x, y = _anchored_position(position, sprite.size, anchor)
    target.alpha_composite(sprite, (x, y))
    return target


def add_glow(
    image: Image.Image,
    color: Color,
    *,
    radius: float = 12.0,
    intensity: float = 1.0,
    spread: int = 0,
    padding: int = 0,
) -> Image.Image:
    """Place a colored blurred copy of an image's alpha below the image."""
    padding = max(0, int(padding))
    source = image.convert("RGBA")
    if padding:
        expanded = Image.new(
            "RGBA",
            (source.width + padding * 2, source.height + padding * 2),
            (0, 0, 0, 0),
        )
        expanded.alpha_composite(source, (padding, padding))
        source = expanded
    alpha = source.getchannel("A")
    if spread > 0:
        kernel = max(3, int(spread) * 2 + 1)
        if kernel % 2 == 0:
            kernel += 1
        alpha = alpha.filter(ImageFilter.MaxFilter(kernel))
    alpha = alpha.filter(ImageFilter.GaussianBlur(max(0.0, float(radius))))
    alpha = alpha.point(lambda value: min(255, round(value * max(0.0, intensity))))
    glow = Image.new("RGBA", source.size, color)
    glow.putalpha(alpha)
    return Image.alpha_composite(glow, source)


def paste_with_glow(
    base: Image.Image,
    overlay: Image.Image,
    position: tuple[int, int],
    *,
    glow_color: Color,
    glow_radius: float = 12.0,
    glow_intensity: float = 1.0,
    anchor: Literal["lt", "center", "rt", "lb", "rb"] = "lt",
) -> Image.Image:
    """Composite an overlay and same-size glow onto ``base``."""
    glowing = add_glow(
        overlay,
        glow_color,
        radius=glow_radius,
        intensity=glow_intensity,
    )
    return transparent_paste(base, glowing, position, anchor=anchor)


def gradient_mask(
    size: tuple[int, int],
    *,
    direction: Literal["horizontal", "vertical", "radial"] = "vertical",
    start: int = 0,
    end: int = 255,
) -> Image.Image:
    """Create an ``L`` mask for fades and compositing."""
    width, height = (max(1, int(value)) for value in size)
    start = max(0, min(255, int(start)))
    end = max(0, min(255, int(end)))
    if direction == "radial":
        mask = Image.radial_gradient("L").resize((width, height))
        if start == 0 and end == 255:
            return mask
        return mask.point(lambda value: round(start + (end - start) * value / 255))
    length = width if direction == "horizontal" else height
    strip = Image.new("L", (length, 1))
    pixels = strip.load()
    divisor = max(1, length - 1)
    for index in range(length):
        pixels[index, 0] = round(start + (end - start) * index / divisor)
    if direction == "horizontal":
        return strip.resize((width, height))
    if direction == "vertical":
        return strip.rotate(270, expand=True).resize((width, height))
    raise ValueError(f"unsupported gradient direction: {direction}")


@lru_cache(maxsize=64)
def _load_font_cached(
    size: int,
    bold: bool,
    numbers: bool,
    extra_candidates: tuple[str, ...],
) -> Font:
    if numbers:
        candidates = _NUMBER_FONT_CANDIDATES
    elif bold:
        candidates = _CJK_BOLD_FONT_CANDIDATES
    else:
        candidates = _CJK_FONT_CANDIDATES
    for candidate in (*extra_candidates, *candidates):
        try:
            path = Path(candidate)
            if path.exists():
                return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def get_font(
    size: int,
    *,
    bold: bool = False,
    numbers: bool = False,
    candidates: Sequence[str | Path] = (),
) -> Font:
    """Find a CJK-capable font, with a condensed numeric option."""
    extra = tuple(str(candidate) for candidate in candidates)
    return _load_font_cached(max(1, int(size)), bold, numbers, extra)


def font(size: int, *, bold: bool = False, numbers: bool = False) -> Font:
    """Short renderer-facing alias for :func:`get_font`."""
    return get_font(size, bold=bold, numbers=numbers)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: Font) -> float:
    """Measure text width with a compatibility fallback."""
    try:
        return float(draw.textlength(str(text), font=font))
    except (AttributeError, TypeError):
        left, _, right, _ = draw.textbbox((0, 0), str(text), font=font)
        return float(right - left)


def ellipsize_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Font,
    max_width: int | float,
    *,
    ellipsis: str = "…",
) -> str:
    """Shorten one line to fit, preserving as much prefix text as possible."""
    value = str(text).replace("\n", " ").strip()
    if text_width(draw, value, font) <= max_width:
        return value
    if text_width(draw, ellipsis, font) > max_width:
        return ""
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = value[:middle].rstrip() + ellipsis
        if text_width(draw, candidate, font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip() + ellipsis


def ellipsize(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Font,
    max_width: int | float,
) -> str:
    """Short renderer-facing alias for :func:`ellipsize_text`."""
    return ellipsize_text(draw, text, font, max_width)


def get_faction_emblem(faction: str, size: int = 96) -> Image.Image | None:
    """Return a contain-fitted emblem for a normalized faction name."""
    key = str(faction or "").strip().casefold()
    aliases = {
        "super earth": "super_earth",
        "humans": "super_earth",
        "human": "super_earth",
        "automaton": "automaton",
        "automatons": "automaton",
        "terminid": "terminids",
        "terminids": "terminids",
        "illuminate": "illuminate",
        "dss": "dss",
        "defense": "defense",
        "major order": "major_order",
    }
    name = aliases.get(key, key.replace(" ", "_"))
    image = asset_image(f"emblems/{name}.png")
    return fit_image(image, (size, size)) if image is not None else None


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Font,
    max_width: int | float,
    *,
    max_lines: int | None = None,
    ellipsis: str = "…",
) -> list[str]:
    """Greedily wrap mixed CJK and Latin text, optionally truncating lines."""
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        current = ""
        for token in _FONT_TOKEN_RE.findall(raw_line):
            candidate = current + token
            if current and text_width(draw, candidate, font) > max_width:
                lines.append(current.rstrip())
                current = token.lstrip()
                if text_width(draw, current, font) > max_width:
                    current, fragments = _split_wide_token(
                        draw,
                        current,
                        font,
                        max_width,
                    )
                    lines.extend(fragments)
            else:
                current = candidate
        if current or not lines:
            lines.append(current.rstrip())
    if max_lines is not None and max_lines >= 0 and len(lines) > max_lines:
        if max_lines == 0:
            return []
        lines = lines[:max_lines]
        marker_width = text_width(draw, ellipsis, font)
        if marker_width > max_width:
            lines[-1] = ""
        else:
            prefix = ellipsize_text(
                draw,
                lines[-1],
                font,
                max_width - marker_width,
                ellipsis="",
            )
            lines[-1] = prefix.rstrip() + ellipsis
    return lines


def draw_badge(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: Font,
    *,
    fill: Color,
    text_fill: Color = (245, 248, 255, 255),
    outline: Color | None = None,
    padding: tuple[int, int] = (10, 5),
    radius: int = 8,
) -> tuple[int, int, int, int]:
    """Draw a compact status badge and return its bounding box."""
    x, y = position
    left, top, right, bottom = draw.textbbox((0, 0), str(text), font=font)
    text_w = right - left
    text_h = bottom - top
    px, py = padding
    box = (x, y, x + text_w + px * 2, y + text_h + py * 2)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline)
    draw.text((x + px, y + py - top), str(text), font=font, fill=text_fill)
    return box


def draw_ring(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    radius: int,
    progress: float,
    *,
    color: Color,
    track: Color = (45, 53, 69, 255),
    width: int = 8,
    start_angle: float = -90.0,
) -> None:
    """Draw a progress ring where ``progress`` may be 0..1 or 0..100."""
    value = float(progress)
    if value > 1.0:
        value /= 100.0
    value = _clamp01(value)
    cx, cy = center
    radius = max(1, int(radius))
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    draw.ellipse(box, outline=track, width=max(1, int(width)))
    if value > 0:
        draw.arc(
            box,
            start=start_angle,
            end=start_angle + 360.0 * value,
            fill=color,
            width=max(1, int(width)),
        )


def procedural_planet(
    size: int,
    seed: str | int,
    *,
    palette: Sequence[RGB] = ((34, 83, 126), (45, 132, 112), (180, 198, 176)),
    atmosphere: RGB | None = (85, 180, 255),
    rings: bool = False,
) -> Image.Image:
    """Return a deterministic transparent planet sprite suitable for maps/cards."""
    normalized_palette = tuple(
        tuple(int(channel) for channel in color) for color in palette
    )
    if not normalized_palette:
        raise ValueError("palette must contain at least one color")
    result = _procedural_planet_cached(
        max(16, int(size)),
        str(seed),
        normalized_palette,
        atmosphere,
        bool(rings),
    )
    return result.copy()


@lru_cache(maxsize=128)
def _procedural_planet_cached(
    size: int,
    seed: str,
    palette: tuple[RGB, ...],
    atmosphere: RGB | None,
    rings: bool,
) -> Image.Image:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    state = int.from_bytes(digest[:8], "big")

    def random_unit() -> float:
        nonlocal state
        state = (6364136223846793005 * state + 1442695040888963407) & ((1 << 64) - 1)
        return state / ((1 << 64) - 1)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    diameter = round(size * (0.62 if rings else 0.76))
    left = (size - diameter) // 2
    top = (size - diameter) // 2
    planet_box = (left, top, left + diameter, top + diameter)

    if atmosphere is not None:
        glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        margin = max(2, round(size * 0.035))
        glow_draw.ellipse(
            (
                planet_box[0] - margin,
                planet_box[1] - margin,
                planet_box[2] + margin,
                planet_box[3] + margin,
            ),
            fill=(*atmosphere, 180),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(max(2, size * 0.035)))
        canvas = Image.alpha_composite(canvas, glow)
        draw = ImageDraw.Draw(canvas)

    if rings:
        ring_color = (*palette[-1], 150)
        ring_width = max(1, size // 70)
        ring_box = (
            round(size * 0.08),
            round(size * 0.34),
            round(size * 0.92),
            round(size * 0.66),
        )
        for offset in range(0, max(2, size // 32), ring_width + 1):
            draw.ellipse(
                (
                    ring_box[0] + offset,
                    ring_box[1] + offset // 3,
                    ring_box[2] - offset,
                    ring_box[3] - offset // 3,
                ),
                outline=ring_color,
                width=ring_width,
            )

    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
    surface = Image.new("RGBA", (diameter, diameter), (*palette[0], 255))
    surface_draw = ImageDraw.Draw(surface, "RGBA")
    patch_count = max(18, size // 5)
    for _ in range(patch_count):
        color = palette[int(random_unit() * len(palette)) % len(palette)]
        cx = round(random_unit() * diameter)
        cy = round(random_unit() * diameter)
        rx = round(diameter * (0.04 + random_unit() * 0.16))
        ry = round(diameter * (0.025 + random_unit() * 0.1))
        alpha = round(45 + random_unit() * 105)
        surface_draw.ellipse(
            (cx - rx, cy - ry, cx + rx, cy + ry),
            fill=(*color, alpha),
        )
    surface = surface.filter(ImageFilter.GaussianBlur(max(0.5, diameter * 0.012)))

    shade = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    shade_pixels = shade.load()
    light_x = diameter * 0.30
    light_y = diameter * 0.25
    maximum = math.hypot(diameter - light_x, diameter - light_y)
    for y in range(diameter):
        for x in range(diameter):
            distance = math.hypot(x - light_x, y - light_y) / maximum
            shade_pixels[x, y] = (0, 5, 18, round(205 * distance**1.65))
    surface = Image.alpha_composite(surface, shade)
    surface.putalpha(mask)
    canvas.alpha_composite(surface, (left, top))

    highlight = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    highlight_draw = ImageDraw.Draw(highlight)
    highlight_draw.arc(
        planet_box,
        start=118,
        end=292,
        fill=(235, 250, 255, 150),
        width=max(1, size // 90),
    )
    canvas = Image.alpha_composite(canvas, highlight)
    return canvas


def _split_wide_token(
    draw: ImageDraw.ImageDraw,
    token: str,
    font: Font,
    max_width: int | float,
) -> tuple[str, list[str]]:
    fragments: list[str] = []
    current = ""
    for character in token:
        candidate = current + character
        if current and text_width(draw, candidate, font) > max_width:
            fragments.append(current)
            current = character
        else:
            current = candidate
    return current, fragments


def _anchored_position(
    position: tuple[int, int],
    size: tuple[int, int],
    anchor: Literal["lt", "center", "rt", "lb", "rb"],
) -> tuple[int, int]:
    x, y = position
    width, height = size
    offsets = {
        "lt": (0, 0),
        "center": (width // 2, height // 2),
        "rt": (width, 0),
        "lb": (0, height),
        "rb": (width, height),
    }
    offset_x, offset_y = offsets[anchor]
    return x - offset_x, y - offset_y


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def prune_render_outputs(
    directory: str | Path, *, keep: int = 30, pattern: str = "*.png"
) -> None:
    """按 mtime 只保留最近 keep 个渲染产物（渲染后兜底清理）。

    部分渲染器每次生成唯一文件名且从不清算，长期运行会积累数百 MB；
    隐藏的 .tmp 文件跳过，由各自的原子写路径负责。
    """
    directory = Path(directory)
    try:
        files = sorted(
            (
                f
                for f in directory.glob(pattern)
                if f.is_file() and not f.name.startswith(".")
            ),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
