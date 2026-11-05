"""文本清理与通用格式化工具。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def clean_game_text(text: str | None) -> str:
    """清理游戏文本中的格式占位符，转换为纯文本。"""
    if not text:
        return text or ""

    cleaned = text

    # 游戏内 <i=x></i>
    cleaned = re.sub(r"<i=\d+>", " ", cleaned)
    cleaned = re.sub(r"</i>", " ", cleaned)

    # BBCode
    cleaned = re.sub(r"\[/?p\]", "\n", cleaned)
    cleaned = re.sub(r"\[/?h[1-6]\]", "\n", cleaned)
    cleaned = re.sub(r"\[/?b\]", "", cleaned)
    cleaned = re.sub(r"\[/?i\]", "", cleaned)
    cleaned = re.sub(r"\[/?u\]", "", cleaned)
    cleaned = re.sub(r"\[/?list\]", "\n", cleaned)
    cleaned = re.sub(r"\[\*\]", "\n• ", cleaned)
    cleaned = re.sub(r"\[/\*\]", "", cleaned)
    cleaned = re.sub(r"\[color=[^\]]+\]", "", cleaned)
    cleaned = re.sub(r"\[/color\]", "", cleaned)
    cleaned = re.sub(r"\[/?img(?:\s+[^\]]*)?\]", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\[url=[^\]]+\]", "", cleaned)
    cleaned = re.sub(r"\[/url\]", "", cleaned)
    cleaned = re.sub(r"\[/?[a-zA-Z][a-zA-Z0-9]*(?:=[^\]]+)?\]", "", cleaned)

    # HTML
    cleaned = re.sub(r"<[^>]+>", "", cleaned)

    # 垃圾链接与标记
    cleaned = re.sub(r"zendesk\.com[^\s\]]*", "", cleaned)
    cleaned = re.sub(r'style="[^"]*"', "", cleaned)
    cleaned = re.sub(r"https?://[^\s\]]+", "", cleaned)
    cleaned = re.sub(r"--HELLDIVERS-2-[^\]]*", "", cleaned)

    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def format_number(num: Any) -> str:
    if isinstance(num, (int, float)):
        return f"{num:,}"
    return str(num)


def format_percentage(num: Any) -> str:
    if isinstance(num, (int, float)):
        return f"{num:.1f}%"
    return str(num)


def format_time_hours(seconds: Any) -> str:
    if isinstance(seconds, (int, float)) and seconds > 0:
        hours = int(seconds // 3600)
        return f"{hours:,}小时"
    return "0小时"


_EPOCH_FLOOR = 100_000_000  # 低于此值的数字不视为 Unix 时间戳，避免脏数据变成 1970 年


def _format_epoch(number: float) -> str:
    if number > 10_000_000_000:
        number /= 1000.0  # 毫秒时间戳
    if number < _EPOCH_FLOOR:
        return "未知时间"
    try:
        dt = datetime.fromtimestamp(number, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return "未知时间"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_iso_time(time_str: str | int | float | None) -> str:
    """格式化时间显示；兼容 ISO 字符串与 Unix 时间戳（秒/毫秒，int/float/数字字符串）。

    Steam 官方接口的 ``date`` 字段是秒级时间戳，DSS 的 ``electionEnd`` 等字段
    也可能以数字下发，不能只按 ISO 解析。
    """
    if not time_str:
        return "未知时间"
    if isinstance(time_str, (int, float)):
        try:
            return _format_epoch(float(time_str))
        except OverflowError:
            # float() 换算超大整数（如 10**400）会溢出
            return "未知时间"
    text = str(time_str).strip()
    if not text:
        return "未知时间"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            number = float(text)
        except ValueError:
            return "未知时间"
        return _format_epoch(number)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def truncate_at_boundary(content: str, max_length: int) -> str:
    if len(content) <= max_length:
        return content

    truncate_pos = max(max_length - 20, 1)

    last_paragraph = content.rfind("\n\n", 0, truncate_pos)
    if last_paragraph > max_length * 0.6:
        return content[:last_paragraph] + "\n\n..."

    sentence_endings = [". ", "! ", "? ", "。", "！", "？"]
    last_sentence = -1
    for ending in sentence_endings:
        pos = content.rfind(ending, 0, truncate_pos)
        if pos > last_sentence:
            last_sentence = pos + len(ending)
    if last_sentence > max_length * 0.7:
        return content[:last_sentence] + "..."

    last_space = content.rfind(" ", 0, truncate_pos)
    if last_space > max_length * 0.8:
        return content[:last_space] + "..."

    return content[:truncate_pos] + "..."


def format_expires_in(expires_in: int | float) -> str:
    expires_in = int(expires_in or 0)
    if expires_in <= 0:
        return ""
    hours = expires_in // 3600
    minutes = (expires_in % 3600) // 60
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


DISPATCH_TYPE_NAMES = {
    0: "一般快讯",
    1: "紧急通告",
    2: "战术更新",
    3: "系统公告",
}


def dispatch_type_name(dispatch_type: Any) -> str:
    try:
        key = int(dispatch_type)
    except (TypeError, ValueError):
        return f"类型{dispatch_type}"
    return DISPATCH_TYPE_NAMES.get(key, f"类型{key}")
