from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

def _shorten(text: str, limit: int = 28) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}..."


def _model_failure_reply(error: Exception) -> str:
    # 保留兼容函数签名；详细诊断由调用层写日志，用户侧始终静默。
    return ""


def _is_local_ollama_endpoint(base_url: str) -> bool:
    parsed = urlsplit(str(base_url or "").strip())
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == 11434


def _chat_http_timeout(config: Any) -> float:
    """所有聊天子调用共享同一延迟预算，防止审校或重写无限拖长。"""

    return max(
        5.0,
        min(60.0, float(getattr(config, "chat_request_timeout_seconds", 25.0))),
    )


def new_chat_deadline(config: Any) -> float:
    """本轮主聊天的单调截止时刻；同一轮的所有串行子调用都必须在它之前返回。"""

    return time.monotonic() + _chat_http_timeout(config)


def chat_attempt_timeout(
    config: Any,
    deadline: float | None,
    *,
    attempt_index: int,
    total_attempts: int,
) -> float:
    """分配给单次聊天请求的 httpx 超时（秒）。

    剩余预算在尚未执行的 attempt 间均分，保证后面的重试或修复阶段仍能拿到
    时间，而不是让第一个慢调用吃光整轮预算后把后续调用全部拖成 ReadTimeout。
    剩余时间不足 1 秒时返回 0，调用方据此跳过本次请求（fail-closed），
    不再发起注定要超时的调用。
    """

    budget = _chat_http_timeout(config)
    if deadline is None:
        # 主动消息等独立调用没有本轮 deadline，保持固定预算的旧行为。
        return budget
    remaining = deadline - time.monotonic()
    attempts_left = max(1, total_attempts - attempt_index)
    if remaining <= 1.0:
        return 0.0
    return max(1.0, min(budget, remaining / attempts_left))


def _intent_text(text: str) -> str:
    cleaned = re.sub(r"@\d+", "", text)
    cleaned = cleaned.replace("@群友", "").replace("@全体成员", "")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_owner_id(value: Any, owner_qqs: tuple[int, ...]) -> bool:
    user_id = _as_int(value)
    return user_id is not None and user_id in set(owner_qqs)


def _is_affection_command(text: str) -> bool:
    return str(text or "").strip().lower().startswith("/affection")


def _user_id_from_profile_id(profile_id: str | None) -> int | None:
    if not profile_id:
        return None
    private_match = re.fullmatch(r"private:(\d+)", profile_id)
    if private_match:
        return int(private_match.group(1))
    group_member_match = re.fullmatch(r"group:[^:]+:user:(\d+)", profile_id)
    if group_member_match:
        return int(group_member_match.group(1))
    return None


def _parse_affection_set_value(text: str) -> float | None:
    normalized = text.strip().lower()
    if "偏高" in normalized or "很高" in normalized or "亲近" in normalized:
        return 78.0
    if "普通" in normalized or "正常" in normalized or "中等" in normalized:
        return 55.0
    if "偏低" in normalized or "冷淡" in normalized or "低" in normalized:
        return 32.0
    match = re.search(r"(-?\d+(?:\.\d+)?)", normalized)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None



