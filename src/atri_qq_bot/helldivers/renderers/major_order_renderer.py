"""Broadcast-style renderer for active Major Orders."""

from __future__ import annotations

import uuid
from pathlib import Path

from typing import Any

from PIL import Image, ImageDraw, ImageOps

from .visual_assets import (
    asset_image,
    font,
    prune_render_outputs,
    text_width,
    wrap_text,
)


class MajorOrderRenderer:
    """Render a readable command broadcast without relying on external artwork."""

    def __init__(self, output_dir: Path, *, width: int = 1200) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = max(900, int(width))

    @property
    def available(self) -> bool:
        return True

    def render(
        self,
        order: dict[str, Any],
        *,
        index: int,
        title: str,
        brief: str,
        task_desc: str,
    ) -> Path:
        width, height = self.width, 900
        image = Image.new("RGB", (width, height), (8, 14, 23))
        draw = ImageDraw.Draw(image)
        red, blue, yellow = (178, 41, 52), (20, 103, 150), (246, 194, 49)
        pale, muted = (241, 244, 247), (168, 181, 196)

        # A restrained scanline field keeps the broadcast treatment legible.
        for y in range(0, height, 5):
            draw.line((0, y, width, y), fill=(12, 20, 32), width=1)
        draw.rectangle((0, 0, width, 230), fill=(42, 18, 29))
        draw.rectangle((0, 0, width, 8), fill=red)
        for x in range(-height, width, 72):
            draw.line((x, 0, x + height, height), fill=(55, 24, 38), width=1)

        flag = asset_image("global_events/super_earth_flag.png")
        if flag is not None:
            hero = ImageOps.fit(
                flag.convert("RGB"), (width, 230), method=Image.Resampling.LANCZOS
            )
            image.paste(hero, (0, 0))
            shade = Image.new("RGBA", image.size, (0, 0, 0, 0))
            ImageDraw.Draw(shade).rectangle((0, 0, width, 230), fill=(12, 18, 28, 90))
            image = Image.alpha_composite(image.convert("RGBA"), shade).convert("RGB")
            draw = ImageDraw.Draw(image)
        draw.rectangle((width - 330, 34, width - 50, 76), outline=yellow, width=2)
        draw.text((width - 312, 43), "超级地球最高指挥部", font=font(17, bold=True), fill=yellow)
        draw.text((width - 330, 100), "银河战争广播", font=font(24, bold=True), fill=pale)
        draw.text((width - 330, 140), "加密频道  A-01", font=font(15), fill=muted)

        tab_w = 210
        draw.rounded_rectangle((38, 204, 38 + tab_w, 264), radius=18, fill=yellow)
        draw.text((62, 218), f"命令 #{index:02d}", font=font(19, bold=True), fill=(29, 25, 20))
        draw.rectangle((38, 264, width - 38, 332), fill=blue)
        heading = "新最高命令"
        draw.text((width // 2 - text_width(draw, heading, font(35, bold=True)) / 2, 276), heading, font=font(35, bold=True), fill=pale)

        card = (38, 332, width - 38, height - 40)
        draw.rectangle(card, fill=(27, 27, 32), outline=(57, 65, 78), width=2)
        body_x, body_y, body_w = 68, 370, width - 136
        display_title = title or "最高命令"
        draw.text((body_x, body_y), display_title, font=font(30, bold=True), fill=pale)
        draw.line((body_x, body_y + 52, width - 68, body_y + 52), fill=(66, 72, 82), width=1)
        text = "\n\n".join(part for part in (brief, task_desc) if part.strip()) or "最高指挥部正在整理行动简报。"
        y = body_y + 78
        for paragraph in text.split("\n"):
            for line in wrap_text(draw, paragraph, font(21), body_w, max_lines=3):
                draw.text((body_x, y), line, font=font(21), fill=(221, 224, 230))
                y += 33
            y += 15
            if y > height - 105:
                break
        expires = order.get("expiresIn")
        footer = f"行动状态：已下达    剩余时间：{expires if expires else '以银河战争终端为准'}"
        draw.line((body_x, height - 92, width - 68, height - 92), fill=(57, 65, 78), width=1)
        draw.text((body_x, height - 74), footer, font=font(15), fill=muted)

        output = self.output_dir / f"major_order_{uuid.uuid4().hex}.png"
        temporary = output.with_suffix(".tmp.png")
        image.save(temporary, format="PNG", optimize=True)
        temporary.replace(output)
        prune_render_outputs(self.output_dir)
        return output
