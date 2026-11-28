from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from ...toolbox_parts.visual_origin import is_visual_segment, stamp_visual_segments
from ..message_batch import _message_to_segments

def _voice_remaining_text(
    original_text: str,
    segments: list[str],
    delivered_segments: list[str],
    failed_index: int,
) -> str:
    """返回语音失败后尚未呈现的文本，尽量保留原回复的标点。"""
    original = str(original_text or "").strip()
    if not delivered_segments:
        return original

    delivered_prefix = "".join(delivered_segments)
    if original.startswith(delivered_prefix):
        remainder = original[len(delivered_prefix) :]
        return remainder.lstrip(" \t\r\n，,、：:；;。！？!?-—")
    # 角色提示词可能包含已被语音清洗器移除的舞台动作，此时无法安全地
    # 用原文定位前缀，退回到已清洗的未发送片段，确保不重复发送已发语音。
    return "".join(segments[failed_index:]).strip()


def _onebot_status_is_healthy(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    try:
        retcode = int(result.get("retcode", -1))
    except (TypeError, ValueError):
        return False
    data = result.get("data")
    return bool(
        retcode == 0
        and str(result.get("status") or "ok").lower() in {"ok", "async"}
        and isinstance(data, dict)
        and data.get("online") is True
        and data.get("good") is True
    )


def _quoted_payload_matches(
    event: dict[str, Any], message_id: str, payload: dict[str, Any], bot_qq: Any = None,
) -> bool:
    """Reject conflicting quote identities, including in OneBot's inline reply."""
    for key in ("message_id", "id"):
        supplied_id = payload.get(key)
        if message_id and supplied_id not in (None, "") and str(supplied_id) != message_id:
            return False
    current_type = event.get("message_type")
    if current_type and payload.get("message_type") not in (None, "", current_type):
        return False

    receiver = str(event.get("self_id") or bot_qq or "")
    fetched_receiver = str(payload.get("self_id") or "")
    if receiver and fetched_receiver and receiver != fetched_receiver:
        return False

    group = str(payload.get("group_id") or "")
    if group not in ("", "0") and group != str(event.get("group_id") or ""):
        return False
    if current_type == "private":
        peer = str(event.get("user_id") or "")
        if peer:
            sender = payload.get("sender")
            sender_id = sender.get("user_id") if isinstance(sender, dict) else None
            participants = {peer, receiver}
            for participant in (sender_id, payload.get("user_id"), payload.get("target_id")):
                if participant not in (None, "", 0, "0") and str(participant) not in participants:
                    return False
            peer_id = payload.get("peer_id")
            if peer_id not in (None, "", 0, "0") and str(peer_id) != peer:
                return False
    return True


def _quoted_response_data(
    event: dict[str, Any], message_id: str, response: Any, bot_qq: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(response, dict) or response.get("retcode", 0) not in (0, "0"):
        return None
    if str(response.get("status") or "ok").lower() != "ok":
        return None
    data = response.get("data")
    if isinstance(data, dict) and _quoted_payload_matches(event, message_id, data, bot_qq):
        return data
    return None


def _is_visual_followup(text: str) -> bool:
    normalized = "".join(str(text or "").split()).lower()
    return any(
        phrase in normalized
        for phrase in (
            "这是谁",
            "这个是谁",
            "图里是谁",
            "图片里是谁",
            "什么角色",
            "哪个角色",
            "出自哪里",
            "哪部作品",
            "图上写",
            "图片写",
            "写了什么",
            "什么文字",
            "图片内容",
            "图里内容",
            "这个表情",
            "刚才那张",
            "上一张图",
            "这张图",
            "这张图片",
        )
    )


def _event_has_visual_material(event: dict[str, Any]) -> bool:
    message = event.get("message")
    return isinstance(message, list) and any(is_visual_segment(segment) for segment in message)


def _should_reuse_cached_visual(event: dict[str, Any], text: str) -> bool:
    return not _event_has_visual_material(event) and _is_visual_followup(text)


def _personal_event_message(event: dict[str, Any]) -> str:
    title = str(event.get("title") or "今天的重要安排").strip()
    if event.get("kind") == "birthday":
        return "生日快乐！今天是你的重要日子。高性能亚托莉记得，也希望你今天能被认真地祝福。"
    if len(title) > 72:
        title = title[:72].rstrip("，,：:；;")
    return f"提醒你一下：你之前提到“{title}”。时间到了，先确认要带的东西和下一步，别让重要安排被忙乱挤掉。"


def _pending_visual_key(event: dict[str, Any]) -> str:
    user_id = event.get("user_id")
    message_type = event.get("message_type")
    if user_id in (None, ""):
        return ""
    if message_type == "private":
        return f"private:{user_id}"
    group_id = event.get("group_id")
    if message_type == "group" and group_id not in (None, ""):
        return f"group:{group_id}:user:{user_id}"
    return ""


def _merge_pending_visual_event(
    visual_event: dict[str, Any],
    current_event: dict[str, Any],
    *,
    image_attribution: bool = True,
) -> dict[str, Any]:
    if image_attribution:
        visual_event = stamp_visual_segments(visual_event, origin="pending")
    visual_segments = [
        dict(segment)
        for segment in _message_to_segments(visual_event.get("message"))
        if is_visual_segment(segment)
    ]
    if not visual_segments:
        return current_event
    merged = dict(current_event)
    merged["message"] = visual_segments + _message_to_segments(current_event.get("message"))
    return merged



def _compose_model_input(
    current_text: str,
    quoted_text: str,
    *,
    provenance: str = "",
) -> str:
    """将引用内容和用户当前输入放入明确边界，避免模型混淆说话人。

    ``provenance`` 是引用来源的客观描述（谁发的、多久以前、什么类型、
    是否刚生成的情报图），只补事实，不写限制性规则。
    """

    current = str(current_text or "").strip() or "[用户未附加文字]"
    quoted = str(quoted_text or "").strip()
    if not quoted and not provenance:
        return current
    header = "【引用消息】"
    if provenance:
        header = f"{header}\n（引用来源：{provenance}）"
    if not quoted:
        quoted = "[引用内容无文字]"
    return f"{header}\n{quoted[:800]}\n【用户当前消息】\n{current}"


def _message_queue_id(event: dict[str, Any]) -> str:
    if event.get("message_type") == "group":
        return f"group:{event.get('group_id')}:user:{event.get('user_id')}"
    return _conversation_id(event)


def _conversation_id(event: dict[str, Any]) -> str:
    if event.get("message_type") == "group":
        return f"group:{event.get('group_id')}"
    return f"private:{event.get('user_id')}"


def _profile_id(event: dict[str, Any]) -> str:
    if event.get("message_type") == "group":
        return f"group:{event.get('group_id')}:user:{event.get('user_id')}"
    return f"private:{event.get('user_id')}"


def _nickname(event: dict[str, Any]) -> str | None:
    sender = event.get("sender")
    if not isinstance(sender, dict):
        return None
    return sender.get("card") or sender.get("nickname")


def _send_delay(config: Any) -> float:
    delay_min = max(0.0, float(config.message_send_delay_min))
    delay_max = max(delay_min, float(config.message_send_delay_max))
    return random.uniform(delay_min, delay_max)


def _visible_char_count(text: Any) -> int:
    """估算打字耗时用的可见字数（不计空白，兼容任何可 str 化的输入）。"""
    return sum(1 for char in str(text or "") if not char.isspace())


def _typing_delay(config: Any, text: Any) -> float:
    """段间间隔：模拟"把上一条消息打完"所需的时间。

    ``MESSAGE_SEND_DELAY_MIN/MAX`` 在这里表示**停手后的停顿**区间，真正的打字耗时
    按 ``TYPING_CHARS_PER_SECOND`` 估算后叠加，整体由 ``TYPING_MAX_DELAY_SECONDS``
    封顶。这样长消息天然等得更久，短句仍然干脆，避免"毫秒级连发"的机器特征。
    """
    speed = max(0.5, float(getattr(config, "typing_chars_per_second", 5.5) or 5.5))
    typing_seconds = _visible_char_count(text) / speed * random.uniform(0.85, 1.25)
    pause_min = max(0.0, float(getattr(config, "message_send_delay_min", 0.55) or 0.0))
    pause_max = max(
        pause_min, float(getattr(config, "message_send_delay_max", 1.35) or 0.0)
    )
    ceiling = max(
        pause_min, float(getattr(config, "typing_max_delay_seconds", 12.0) or 12.0)
    )
    pause = random.uniform(pause_min, pause_max)
    return min(ceiling, max(pause_min, typing_seconds + pause))


def _first_reply_delay(config: Any, text: Any) -> float:
    """首条回复前的停顿：看到消息 → 起手打字 → 发出，比段间间隔更长也更随机。"""
    speed = max(0.5, float(getattr(config, "typing_chars_per_second", 5.5) or 5.5))
    floor = max(0.0, float(getattr(config, "reply_first_delay_min", 0.8) or 0.0))
    top = max(floor, float(getattr(config, "reply_first_delay_max", 2.5) or 0.0))
    estimate = floor + _visible_char_count(text) / speed * 0.4 * random.uniform(0.85, 1.3)
    return max(floor, min(top, estimate))


def _require_onebot_action_success(result: dict[str, Any] | None) -> None:
    if not isinstance(result, dict):
        raise RuntimeError("NapCat 没有返回语音发送结果")
    try:
        retcode = int(result.get("retcode", -1))
    except (TypeError, ValueError):
        retcode = -1
    if retcode == 0 and str(result.get("status") or "ok").lower() in {"ok", "async"}:
        return
    detail = _onebot_action_error_detail(result, default=f"retcode={retcode}")
    raise RuntimeError(f"NapCat 消息发送失败：{detail}")


def _onebot_action_error_detail(
    result: dict[str, Any] | None,
    *,
    default: str,
) -> str:
    if not isinstance(result, dict):
        return default
    return str(
        result.get("message")
        or result.get("wording")
        or result.get("msg")
        or default
    ).strip()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
