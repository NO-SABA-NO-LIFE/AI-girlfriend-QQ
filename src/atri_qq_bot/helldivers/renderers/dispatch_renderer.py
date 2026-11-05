"""Renderer for official Helldivers 2 dispatches from api.helldivers2.dev."""

from __future__ import annotations

import uuid
from pathlib import Path

from typing import Any

from PIL import Image, ImageDraw

from .visual_assets import (
    asset_image,
    font,
    paste_contain,
    prune_render_outputs,
    wrap_text,
)


class DispatchRenderer:
    def __init__(self, output_dir: Path, *, width: int = 960) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = max(760, int(width))

    @property
    def available(self) -> bool:
        return True

    def render(
        self,
        dispatch: dict[str, Any],
        *,
        index: int,
        message_text: str,
        hero_path: Path | None = None,
    ) -> list[Path]:
        del hero_path  # Official dispatches have no Steam hero image.
        width = self.width
        title = str(dispatch.get("title") or "超级地球快讯").strip() or "超级地球快讯"
        published = str(dispatch.get("published") or "").strip()
        lines = wrap_text(
            ImageDraw.Draw(Image.new("RGB", (1, 1))), message_text, font(19), width - 76
        ) or ["暂无快讯正文。"]
        pages = [lines[offset : offset + 12] for offset in range(0, len(lines), 12)]
        outputs: list[Path] = []
        for page_index, page_lines in enumerate(pages, 1):
            first_page = page_index == 1
            content_top = 220 if first_page else 146
            height = max(520, content_top + len(page_lines) * 31 + 100)
            image = Image.new("RGB", (width, height), (12, 18, 28))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (20, 20, width - 20, height - 20),
                radius=16,
                fill=(24, 33, 46),
                outline=(68, 92, 118),
                width=2,
            )
            if first_page:
                draw.rectangle((20, 20, width - 20, 112), fill=(20, 90, 140))
                emblem = asset_image("emblems/super_earth.png")
                if emblem is not None:
                    paste_contain(image, emblem, (40, 34, 64, 64))
                draw.text((120, 42), title, font=font(28, bold=True), fill=(245, 246, 248))
                meta = f"超级地球最高指挥部 · 编号 #{dispatch.get('id', index)}"
                if published:
                    meta += f" · {published}"
                draw.text((120, 78), meta, font=font(14), fill=(188, 214, 232))
            else:
                draw.text(
                    (40, 54),
                    f"{title} · 续页",
                    font=font(26, bold=True),
                    fill=(245, 246, 248),
                )
                draw.line((40, 112, width - 40, 112), fill=(80, 100, 120), width=1)
            y = content_top
            for line in page_lines:
                draw.text((40, y), line, font=font(19), fill=(224, 230, 236))
                y += 31
            draw.text(
                (40, height - 58),
                f"api.helldivers2.dev/dispatches · #{index:02d} · {page_index}/{len(pages)}",
                font=font(13),
                fill=(150, 168, 186),
            )
            output = self.output_dir / f"dispatch_{uuid.uuid4().hex}.png"
            temporary = output.with_suffix(".tmp.png")
            image.save(temporary, format="PNG", optimize=True)
            temporary.replace(output)
            prune_render_outputs(self.output_dir)
            outputs.append(output)
        return outputs
