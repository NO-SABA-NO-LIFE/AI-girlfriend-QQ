"""Galactic Wide Web 风格银河战争总览渲染。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from typing import Any

try:
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore

try:
    from ..core.i18n import format_planet
    from .visual_assets import (
        asset_image,
        ellipsize,
        font,
        paste_contain,
        prune_render_outputs,
        text_width,
        wrap_text,
    )
except Exception:  # pragma: no cover
    format_planet = lambda value, default=None: default or str(value)  # type: ignore
    asset_image = None  # type: ignore
    prune_render_outputs = lambda *a, **k: None  # type: ignore
    paste_contain = None  # type: ignore

    try:
        from .card_renderer import _font as _fallback_font
    except ImportError:
        from card_renderer import _font as _fallback_font  # type: ignore

    def font(size: int, *, bold: bool = False):
        return _fallback_font(size)

    def text_width(draw, text: str, fnt) -> float:
        try:
            return draw.textlength(text, font=fnt)
        except Exception:
            return len(text) * 8

    def ellipsize(draw, text: str, fnt, max_width: int) -> str:
        out = str(text)
        while out and text_width(draw, out + "…", fnt) > max_width:
            out = out[:-1]
        return out + "…" if out != text else out

    def wrap_text(draw, text: str, fnt, max_width: int) -> list[str]:
        lines: list[str] = []
        cur = ""
        for ch in str(text):
            if text_width(draw, cur + ch, fnt) <= max_width:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        return lines


COLORS = {
    "Humans": (105, 185, 235),
    "DSS": (215, 232, 246),
    "Urgent": (224, 78, 87),
    "Defense": (89, 160, 225),
    "Automaton": (226, 74, 80),
    "Terminids": (225, 168, 39),
    "Illuminate": (159, 91, 224),
}


class DashboardRenderer:
    """把各战线渲染为独立 Discord Embed 风格面板。"""

    VERSION = "2"

    def __init__(self, output_dir: Path, *, width: int = 980) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = max(820, int(width))

    @property
    def available(self) -> bool:
        return Image is not None

    @staticmethod
    def _remaining(value: Any) -> str:
        if not value:
            return ""
        try:
            from datetime import datetime, timezone

            s = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            seconds = int((dt - datetime.now(timezone.utc)).total_seconds())
            if seconds <= 0:
                return "已结束"
            days, seconds = divmod(seconds, 86400)
            hours, seconds = divmod(seconds, 3600)
            minutes = seconds // 60
            if days:
                return f"{days}天{hours}小时"
            if hours:
                return f"{hours}小时{minutes}分"
            return f"{minutes}分钟"
        except Exception:
            return ""

    @staticmethod
    def _section_height(
        rows: list[dict[str, Any]], low: list[dict[str, Any]] | None = None
    ) -> int:
        shown = max(1, len(rows))
        return 108 + shown * 70 + (46 if low else 0)

    def _header(self, img, draw, y: int, subtitle: str) -> int:
        x, w, h = 18, self.width - 36, 132
        draw.rounded_rectangle((x, y, x + w, y + h), radius=12, fill=(24, 39, 58))
        draw.rectangle((x, y, x + w, y + 7), fill=COLORS["Humans"])
        draw.rectangle((x + 24, y + 24, x + 89, y + 105), fill=(76, 156, 207))
        emblem = asset_image("emblems/super_earth.png") if asset_image else None
        if emblem is not None and paste_contain is not None:
            paste_contain(img, emblem, (x + 31, y + 31, 52, 66))
        f_title = font(37, bold=True)
        f_sub = font(17)
        draw.text(
            (x + 112, y + 28), "银河总览", font=f_title, fill=(235, 243, 250)
        )
        draw.text(
            (x + 114, y + 78),
            subtitle or "银河战争总览",
            font=f_sub,
            fill=(150, 184, 208),
        )
        return y + h + 16

    def _panel(
        self,
        img,
        draw,
        y: int,
        *,
        title: str,
        color: tuple[int, int, int],
        emblem_key: str,
        rows: list[dict[str, Any]],
        low: list[dict[str, Any]] | None = None,
        subtitle: str = "",
    ) -> int:
        h = self._section_height(rows, low)
        x, w = 18, self.width - 36
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=8,
            fill=(30, 33, 40),
            outline=(48, 53, 64),
            width=1,
        )
        draw.rectangle((x, y, x + 7, y + h), fill=color)
        f_title = font(25, bold=True)
        f_sub = font(14)
        f_name = font(18, bold=True)
        f_meta = font(14)
        draw.text((x + 24, y + 18), title, font=f_title, fill=(241, 243, 247))
        if subtitle:
            draw.text((x + 26, y + 54), subtitle, font=f_sub, fill=(143, 151, 166))

        emblem = asset_image(emblem_key) if asset_image else None
        if emblem is not None and paste_contain is not None:
            paste_contain(img, emblem, (x + w - 104, y + 12, 78, 78))

        row_y = y + 86
        usable_w = w - 54
        for row in rows:
            name = format_planet(row.get("name"), "未知星球")
            players = int(row.get("players") or 0)
            progress = row.get("progress")
            pct = max(0.0, min(100.0, float(progress or 0.0)))
            ends = self._remaining(row.get("ends"))
            rate = row.get("recent_rate")
            result = str(row.get("estimated_result") or "")
            meta = f"在线 {players:,}"
            if ends:
                meta += f"  ·  {ends}"
            if rate is not None:
                try:
                    meta += f"  ·  {float(rate):+.2f}%/小时"
                except (TypeError, ValueError):
                    pass
            if result:
                meta += f"  ·  {result}"
            show_name = ellipsize(draw, name, f_name, 390)
            draw.text((x + 27, row_y), show_name, font=f_name, fill=(231, 234, 240))
            pct_text = f"{pct:.2f}%"
            pw = text_width(draw, pct_text, f_name)
            draw.text((x + w - 27 - pw, row_y), pct_text, font=f_name, fill=color)
            draw.text((x + 27, row_y + 27), meta, font=f_meta, fill=(143, 151, 166))
            by = row_y + 49
            draw.rounded_rectangle(
                (x + 27, by, x + usable_w, by + 8), radius=4, fill=(53, 58, 68)
            )
            if pct > 0:
                fill_w = max(8, int((usable_w - 27) * pct / 100))
                draw.rounded_rectangle(
                    (x + 27, by, x + 27 + fill_w, by + 8), radius=4, fill=color
                )
            row_y += 70

        if not rows:
            draw.text(
                (x + 27, row_y), "当前没有相关战役", font=f_meta, fill=(125, 133, 148)
            )

        if low:
            names = " · ".join(
                f"{format_planet(r.get('name'), '?')} {int(r.get('players') or 0):,}"
                for r in low[:8]
            )
            names = ellipsize(draw, f"低影响战役  {names}", f_meta, w - 56)
            draw.text((x + 27, y + h - 34), names, font=f_meta, fill=(115, 123, 138))
        return y + h + 13

    def _dss_panel(self, img, draw, y: int, sections: dict[str, Any]) -> int:
        dss = sections.get("dss") or {}
        planet = sections.get("dss_planet") or {}
        actions = [a for a in dss.get("tacticalActions") or [] if isinstance(a, dict)]
        active = []
        for action in actions:
            try:
                if int(action.get("status") or 0) == 2:
                    active.append(
                        str(
                            action.get("display_name")
                            or action.get("name")
                            or "战术行动"
                        )
                    )
            except (TypeError, ValueError):
                continue
        election = self._remaining(dss.get("electionEnd"))
        rows = [
            {
                "name": format_planet(
                    planet.get("name") or dss.get("planetName"), "未知位置"
                ),
                "players": ((planet.get("statistics") or {}).get("playerCount") or 0),
                "progress": 100,
                "estimated_result": "、".join(active) if active else "无进行中战术",
                "ends": dss.get("electionEnd"),
            }
        ]
        subtitle = f"民主空间站 · 下次跃迁 {election or '未知'}"
        return self._panel(
            img,
            draw,
            y,
            title="民主空间站",
            color=COLORS["DSS"],
            emblem_key="dss/dss_wireframe.png",
            rows=rows,
            subtitle=subtitle,
        )

    def render(self, sections: dict[str, Any], *, subtitle: str = "") -> Path | None:
        if not self.available:
            return None
        urgent = list(sections.get("urgent") or [])[:5]
        defending = list(sections.get("defending") or [])[:5]
        attacking = sections.get("attacking") or {}

        specs: list[tuple[str, tuple[int, int, int], str, list, list]] = []
        if urgent:
            specs.append(
                (
                    "紧急解放行动",
                    COLORS["Urgent"],
                    "emblems/defense.png",
                    urgent,
                    [],
                )
            )
        if defending:
            specs.append(
                ("防御中", COLORS["Defense"], "emblems/defense.png", defending, [])
            )
        for faction, title in (
            ("Automaton", "进攻中 · 机器人"),
            ("Terminids", "进攻中 · 终结族"),
            ("Illuminate", "进攻中 · 光能者"),
        ):
            block = attacking.get(faction) or {}
            main = list(block.get("main") or [])[:8]
            low = list(block.get("low") or [])
            if main or low:
                specs.append(
                    (
                        title,
                        COLORS[faction],
                        f"emblems/{faction.lower()}.png",
                        main,
                        low,
                    )
                )

        height = 166
        if sections.get("dss"):
            height += self._section_height([{}]) + 13
        height += sum(
            self._section_height(rows, low) + 13 for _, _, _, rows, low in specs
        )
        height += 82
        img = Image.new("RGB", (self.width, height), (14, 16, 21))
        draw = ImageDraw.Draw(img)
        y = self._header(img, draw, 18, subtitle)
        if sections.get("dss"):
            y = self._dss_panel(img, draw, y, sections)
        for title, color, emblem, rows, low in specs:
            total_players = sum(int(r.get("players") or 0) for r in rows + low)
            total = max(1, int(sections.get("total_players") or total_players))
            sub = f"{total_players:,} 名绝地潜兵 · 占活跃兵力 {total_players / total * 100:.1f}%"
            y = self._panel(
                img,
                draw,
                y,
                title=title,
                color=color,
                emblem_key=emblem,
                rows=rows,
                low=low,
                subtitle=sub,
            )

        f_footer = font(15)
        total = int(sections.get("total_players") or 0)
        footer = f"活跃绝地潜兵总数  {total:,}   ·   api.helldivers2.dev"
        draw.line((28, y + 12, self.width - 28, y + 12), fill=(51, 55, 65), width=1)
        draw.text((30, y + 30), footer, font=f_footer, fill=(126, 135, 150))

        out = self.output_dir / (
            f"dashboard_{self.VERSION}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        )
        tmp = out.with_suffix(".tmp.png")
        img.save(tmp, format="PNG", optimize=True)
        tmp.replace(out)
        prune_render_outputs(self.output_dir)
        return out
