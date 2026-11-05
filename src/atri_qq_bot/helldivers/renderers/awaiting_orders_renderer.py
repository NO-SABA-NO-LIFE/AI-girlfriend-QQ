"""最高命令空状态渲染。"""

from __future__ import annotations

import uuid
from pathlib import Path


try:
    from PIL import Image, ImageDraw, ImageFilter
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFilter = None  # type: ignore

try:
    from .visual_assets import (
        asset_image,
        font,
        paste_contain,
        prune_render_outputs,
        text_width,
    )
except Exception:  # pragma: no cover
    asset_image = None  # type: ignore
    paste_contain = None  # type: ignore
    prune_render_outputs = lambda *a, **k: None  # type: ignore

    def font(size: int, *, bold: bool = False):
        try:
            from .card_renderer import _font
        except ImportError:
            from card_renderer import _font  # type: ignore

        return _font(size)

    def text_width(draw, text: str, fnt) -> float:
        try:
            return draw.textlength(text, font=fnt)
        except Exception:
            return len(text) * max(8, getattr(fnt, "size", 14) * 0.55)


class AwaitingOrdersRenderer:
    """渲染图像 8 风格的等待最高命令卡。"""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return Image is not None

    def render(
        self, message: str = "等待超级地球最高指挥部的进一步指示"
    ) -> Path | None:
        if not self.available:
            return None
        width, height = 960, 510
        img = Image.new("RGB", (width, height), (10, 17, 29))
        draw = ImageDraw.Draw(img)

        blue = (109, 193, 245)
        pale = (215, 239, 255)
        panel = (14, 25, 42)
        draw.rectangle((0, 0, width, 92), fill=(54, 129, 181))
        draw.rectangle((0, 0, width, 6), fill=blue)
        draw.rectangle((0, 92, width, height), fill=panel)

        # 顶栏的 AW 字样与装饰线。
        f_aw = font(48, bold=True)
        f_sub = font(16)
        draw.text((38, 18), "AW", font=f_aw, fill=pale)
        draw.text((142, 33), "等待最高命令", font=f_sub, fill=(176, 217, 242))
        draw.line((142, 60, width - 36, 60), fill=(135, 196, 232), width=2)

        # 顶栏右侧半透明徽记。
        emblem = asset_image("emblems/super_earth.png") if asset_image else None
        if emblem is not None and paste_contain is not None:
            ghost = emblem.copy().convert("RGBA")
            alpha = ghost.getchannel("A").point(lambda a: int(a * 0.22))
            ghost.putalpha(alpha)
            paste_contain(img, ghost, (width - 176, -26, 150, 150))

        # 中央徽记：发光底层 + 清晰图层。
        if emblem is not None and paste_contain is not None:
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            paste_contain(layer, emblem, (width // 2 - 116, 122, 232, 232))
            if ImageFilter is not None:
                glow = layer.filter(ImageFilter.GaussianBlur(18))
                img = Image.alpha_composite(img.convert("RGBA"), glow)
                img = Image.alpha_composite(img, layer).convert("RGB")
                draw = ImageDraw.Draw(img)
            else:
                img.paste(layer, (0, 0), layer)
                draw = ImageDraw.Draw(img)
        else:
            # 资源缺失时使用不会依赖 emoji 的简洁同心圆。
            cx, cy = width // 2, 234
            draw.ellipse((cx - 88, cy - 88, cx + 88, cy + 88), outline=blue, width=5)
            draw.ellipse(
                (cx - 68, cy - 68, cx + 68, cy + 68), outline=(70, 135, 180), width=2
            )
            draw.line((cx - 46, cy, cx + 46, cy), fill=blue, width=4)
            draw.line((cx, cy - 46, cx, cy + 46), fill=blue, width=4)

        f_msg = font(25, bold=True)
        tw = text_width(draw, message, f_msg)
        draw.text(((width - tw) / 2, 382), message, font=f_msg, fill=(227, 235, 245))
        f_hint = font(15)
        hint = "等待超级地球最高指挥部下达进一步指示"
        hw = text_width(draw, hint, f_hint)
        draw.text(((width - hw) / 2, 426), hint, font=f_hint, fill=(114, 145, 172))
        draw.line((50, 472, width - 50, 472), fill=(44, 70, 94), width=1)

        out = self.output_dir / f"awaiting_orders_{uuid.uuid4().hex}.png"
        tmp = out.with_suffix(".tmp.png")
        img.save(tmp, format="PNG", optimize=True)
        tmp.replace(out)
        prune_render_outputs(self.output_dir)
        return out
