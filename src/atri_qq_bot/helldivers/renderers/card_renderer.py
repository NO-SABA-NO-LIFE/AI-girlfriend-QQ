"""GWW 风格 embed 卡片渲染器（Pillow）。

深色卡片 + 左侧阵营色条 + 标题/副标题 + 元素（字段/统计格/段落/进度条/分隔线）
+ 页脚。自适应高度，CJK 自动换行，含 Pillow 手绘徽记（参考 Helldivers 战备图标风格）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._logging import logger

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore

try:
    from .visual_assets import asset_image, fit_image
except Exception:  # pragma: no cover
    asset_image = None  # type: ignore
    fit_image = None  # type: ignore


# ---- 主题 ----
WIDTH = 760
PAD_X = 28
ACCENT_W = 8
BG = (24, 27, 34)
BG_PANEL = (32, 36, 45)
FG = (235, 239, 247)
FG_LABEL = (150, 170, 205)
FG_MUTED = (140, 150, 168)
DIVIDER = (52, 58, 74)
TRACK = (46, 52, 66)

FACTION_COLORS: dict[str, tuple[int, int, int]] = {
    "Humans": (107, 183, 234),
    "Super Earth": (107, 183, 234),
    "Terminids": (234, 167, 43),
    "Automaton": (252, 108, 115),
    "Illuminate": (107, 59, 187),
    "Unknown": (150, 155, 170),
}
COLOR_MO = (255, 220, 0)
COLOR_DSS = (214, 232, 248)

_FONT_CACHE: dict[int, Any] = {}


def _font(size: int):
    if ImageFont is None:
        return None
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    fnt = None
    for path in candidates:
        try:
            if Path(path).exists():
                fnt = ImageFont.truetype(path, size=size)
                break
        except Exception:
            continue
    if fnt is None:
        try:
            fnt = ImageFont.load_default()
        except Exception:
            fnt = None
    _FONT_CACHE[size] = fnt
    return fnt


def _text_w(draw, text: str, font) -> float:
    if font is None:
        return len(text) * 8
    try:
        return draw.textlength(text, font=font)
    except Exception:
        return len(text) * 8


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    """CJK 友好的贪心换行。"""
    out: list[str] = []
    for raw_line in str(text).split("\n"):
        if not raw_line:
            out.append("")
            continue
        cur = ""
        for ch in raw_line:
            trial = cur + ch
            if _text_w(draw, trial, font) <= max_w:
                cur = trial
            else:
                if cur:
                    out.append(cur)
                cur = ch
        if cur:
            out.append(cur)
    return out


@dataclass
class Card:
    title: str
    accent: tuple[int, int, int] = FACTION_COLORS["Humans"]
    subtitle: str = ""
    footer: str = "为了超级地球！🌍  ·  api.helldivers2.dev"
    emblem: str = "super_earth"  # super_earth | Terminids | Automaton | Illuminate | dss | mo | ""
    elements: list[dict[str, Any]] = field(default_factory=list)

    def heading(self, text: str) -> "Card":
        self.elements.append({"type": "heading", "text": text})
        return self

    def field(self, label: str, value: str) -> "Card":
        self.elements.append({"type": "field", "label": label, "value": value})
        return self

    def grid(self, pairs: list[tuple[str, str]]) -> "Card":
        self.elements.append({"type": "grid", "pairs": pairs})
        return self

    def text(self, s: str, muted: bool = False) -> "Card":
        self.elements.append({"type": "text", "text": s, "muted": muted})
        return self

    def bar(
        self,
        label: str,
        pct: float,
        color: tuple[int, int, int] | None = None,
        note: str = "",
    ) -> "Card":
        self.elements.append(
            {"type": "bar", "label": label, "pct": pct, "color": color, "note": note}
        )
        return self

    def divider(self) -> "Card":
        self.elements.append({"type": "divider"})
        return self


# ---- 手绘徽记 ----
def _draw_emblem(draw, cx: int, cy: int, r: int, kind: str, color) -> None:
    if kind == "super_earth":
        # 圆环 + 五角星
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=2)
        _draw_star(draw, cx, cy, r * 0.62, r * 0.26, color)
    elif kind == "dss":
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=2)
        draw.line((cx - r * 0.6, cy, cx + r * 0.6, cy), fill=color, width=2)
        draw.line((cx, cy - r * 0.6, cx, cy + r * 0.6), fill=color, width=2)
        draw.ellipse(
            (cx - r * 0.25, cy - r * 0.25, cx + r * 0.25, cy + r * 0.25),
            outline=color,
            width=2,
        )
    elif kind == "mo":
        _draw_star(draw, cx, cy, r, r * 0.42, color)
    elif kind in ("Terminids", "Automaton", "Illuminate", "Humans"):
        # 菱形阵营徽
        draw.polygon(
            [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
            outline=color,
            width=2,
        )
        draw.ellipse(
            (cx - r * 0.3, cy - r * 0.3, cx + r * 0.3, cy + r * 0.3), fill=color
        )


def _draw_star(draw, cx: float, cy: float, r_out: float, r_in: float, color) -> None:
    import math

    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = r_out if i % 2 == 0 else r_in
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    draw.polygon(pts, fill=color)


class CardRenderer:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return Image is not None

    def _cleanup(self, max_age: int = 3600) -> None:
        now = time.time()
        try:
            for p in self.output_dir.glob("card_*.png"):
                if now - p.stat().st_mtime > max_age:
                    p.unlink(missing_ok=True)
        except Exception:
            pass

    def render(self, card: Card) -> Path | None:
        if not self.available:
            return None
        try:
            content_w = WIDTH - PAD_X * 2
            probe = Image.new("RGB", (10, 10))
            pdraw = ImageDraw.Draw(probe)

            f_title = _font(28)
            f_head = _font(20)
            f_label = _font(16)
            f_val = _font(18)
            f_text = _font(17)
            f_small = _font(14)

            # ---- 预计算高度 ----
            y = 24 + 40  # header
            layout: list[tuple[str, dict, Any]] = []
            for el in card.elements:
                t = el["type"]
                if t == "heading":
                    layout.append(("heading", el, None))
                    y += 34
                elif t == "field":
                    lines = _wrap(pdraw, el["value"], f_val, content_w)
                    layout.append(("field", el, lines))
                    y += 24 + 26 * max(1, len(lines)) + 6
                elif t == "grid":
                    pairs = el["pairs"]
                    rows = (len(pairs) + 1) // 2
                    layout.append(("grid", el, None))
                    y += rows * 52 + 6
                elif t == "text":
                    lines = _wrap(pdraw, el["text"], f_text, content_w)
                    layout.append(("text", el, lines))
                    y += 26 * max(1, len(lines)) + 8
                elif t == "bar":
                    layout.append(("bar", el, None))
                    y += 46
                elif t == "divider":
                    layout.append(("divider", el, None))
                    y += 18
            y += 20 + 34  # footer
            height = max(180, int(y))

            # ---- 绘制 ----
            img = Image.new("RGB", (WIDTH, height), BG)
            draw = ImageDraw.Draw(img)
            accent = tuple(int(c) for c in card.accent)

            # 左侧色条
            draw.rectangle((0, 0, ACCENT_W, height), fill=accent)
            # 顶部渐隐条
            draw.rectangle((0, 0, WIDTH, 4), fill=accent)

            # 标题 + 徽记
            emblem_r = 18
            emblem_cx = WIDTH - PAD_X - emblem_r
            emblem_cy = 24 + 14
            if card.emblem:
                asset_names = {
                    "super_earth": "super_earth",
                    "Humans": "super_earth",
                    "dss": "dss",
                    "mo": "major_order",
                    "Automaton": "automaton",
                    "Terminids": "terminids",
                    "Illuminate": "illuminate",
                }
                icon = (
                    asset_image(
                        f"emblems/{asset_names.get(card.emblem, card.emblem)}.png"
                    )
                    if asset_image is not None
                    else None
                )
                if icon is not None and fit_image is not None:
                    fitted = fit_image(icon, (emblem_r * 2, emblem_r * 2))
                    img.paste(
                        fitted,
                        (emblem_cx - emblem_r, emblem_cy - emblem_r),
                        fitted,
                    )
                else:
                    _draw_emblem(
                        draw, emblem_cx, emblem_cy, emblem_r, card.emblem, accent
                    )
            title = card.title
            # 标题描边
            tx, ty = PAD_X, 22
            draw.text((tx, ty), title, font=f_title, fill=FG)
            if card.subtitle and f_small:
                sw = _text_w(draw, card.subtitle, f_small)
                draw.text(
                    (WIDTH - PAD_X - emblem_r * 2 - 10 - sw, 30),
                    card.subtitle,
                    font=f_small,
                    fill=FG_MUTED,
                )
            draw.line((PAD_X, 62, WIDTH - PAD_X, 62), fill=DIVIDER, width=1)

            y = 24 + 40
            for kind, el, extra in layout:
                if kind == "heading":
                    draw.text((PAD_X, y + 4), el["text"], font=f_head, fill=accent)
                    y += 34
                elif kind == "field":
                    draw.text((PAD_X, y), el["label"], font=f_label, fill=FG_LABEL)
                    y += 24
                    for ln in extra or [""]:
                        draw.text((PAD_X, y), ln, font=f_val, fill=FG)
                        y += 26
                    y += 6
                elif kind == "grid":
                    pairs = el["pairs"]
                    col_w = (content_w - 16) // 2
                    for i, (lab, val) in enumerate(pairs):
                        col = i % 2
                        row = i // 2
                        cx = PAD_X + col * (col_w + 16)
                        cy = y + row * 52
                        draw.rectangle((cx, cy, cx + col_w, cy + 44), fill=BG_PANEL)
                        draw.text((cx + 12, cy + 6), lab, font=f_small, fill=FG_LABEL)
                        vshow = val
                        while (
                            f_val
                            and _text_w(draw, vshow, f_val) > col_w - 24
                            and len(vshow) > 4
                        ):
                            vshow = vshow[:-2]
                        draw.text((cx + 12, cy + 22), vshow, font=f_val, fill=FG)
                    rows = (len(pairs) + 1) // 2
                    y += rows * 52 + 6
                elif kind == "text":
                    color = FG_MUTED if el.get("muted") else FG
                    for ln in extra or [""]:
                        draw.text((PAD_X, y), ln, font=f_text, fill=color)
                        y += 26
                    y += 8
                elif kind == "bar":
                    label = el["label"]
                    pct = max(0.0, min(100.0, float(el["pct"])))
                    bcolor = el.get("color") or accent
                    note = el.get("note") or ""
                    draw.text((PAD_X, y), label, font=f_small, fill=FG_LABEL)
                    if note:
                        nw = _text_w(draw, note, f_small)
                        draw.text(
                            (WIDTH - PAD_X - nw, y), note, font=f_small, fill=FG_MUTED
                        )
                    by = y + 22
                    bw = content_w
                    draw.rounded_rectangle(
                        (PAD_X, by, PAD_X + bw, by + 12), radius=6, fill=TRACK
                    )
                    fillw = int(bw * pct / 100)
                    if fillw > 0:
                        draw.rounded_rectangle(
                            (PAD_X, by, PAD_X + max(6, fillw), by + 12),
                            radius=6,
                            fill=tuple(int(c) for c in bcolor),
                        )
                    y += 46
                elif kind == "divider":
                    draw.line(
                        (PAD_X, y + 8, WIDTH - PAD_X, y + 8), fill=DIVIDER, width=1
                    )
                    y += 18

            # 页脚
            draw.line(
                (PAD_X, height - 40, WIDTH - PAD_X, height - 40),
                fill=DIVIDER,
                width=1,
            )
            if card.footer and f_small:
                draw.text(
                    (PAD_X, height - 30), card.footer, font=f_small, fill=FG_MUTED
                )

            self._cleanup()
            out = self.output_dir / f"card_{uuid.uuid4().hex}.png"
            tmp = out.with_suffix(".tmp.png")
            img.save(tmp, format="PNG", optimize=True)
            tmp.replace(out)
            return out
        except Exception as e:
            logger.exception(f"[HD2] card render failed: {e}")
            return None
