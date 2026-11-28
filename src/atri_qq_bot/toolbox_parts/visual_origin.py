from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


VISUAL_SEGMENT_TYPES = frozenset({"image", "mface", "marketface"})
MESSAGE_ID_META_KEY = "_atri_message_id"
SENDER_ID_META_KEY = "_atri_sender_id"
SENDER_NAME_META_KEY = "_atri_sender_name"
SENT_AT_META_KEY = "_atri_sent_at"
SEGMENT_INDEX_META_KEY = "_atri_segment_index"
MESSAGE_ORDER_META_KEY = "_atri_message_order"
ORIGIN_META_KEY = "_atri_image_origin"

IMAGE_ORIGIN_PRIORITY = {
    "current": 0,
    "pending": 1,
    "quoted": 2,
    "same_sender_recent": 3,
    "explicit_other": 4,
    "legacy_other": 5,
}


@dataclass(frozen=True)
class ImageOrigin:
    message_id: str = ""
    segment_index: int = -1
    sender_id: str = ""
    sender_name: str = ""
    sent_at: float = 0.0
    message_order: int = 0
    origin: str = "current"
    label: str = ""
    status: str = "unavailable"
    evidence_level: str = "none"
    reason: str = ""
    elapsed_ms: int = 0

    @property
    def key(self) -> tuple[str, int]:
        return self.message_id, self.segment_index

    def origin_text(self) -> str:
        sender = self.sender_name or self.sender_id
        if self.origin == "current":
            return "当前说话人本轮发的"
        if self.origin == "pending":
            return "当前说话人先前发的，和本条一起处理"
        if self.origin == "same_sender_recent":
            return "当前说话人先前发的"
        if self.origin == "quoted":
            return f"引用消息里的图，原发送者是“{sender}”" if sender else "引用消息里的图，原发送者未确认"
        return f"群友“{sender}”发的" if sender else "群图，原发送者未确认"


def is_visual_segment(segment: Any) -> bool:
    return (
        isinstance(segment, dict)
        and str(segment.get("type") or "").lower() in VISUAL_SEGMENT_TYPES
    )


def segment_data(segment: dict[str, Any]) -> dict[str, Any]:
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def _timestamp(value: Any) -> float:
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def stamp_visual_segments(
    event: dict[str, Any],
    *,
    origin: str = "current",
    message_id: str | None = None,
    sender_id: str | None = None,
    sender_name: str | None = None,
    sent_at: float | None = None,
    message_order: int = 0,
) -> dict[str, Any]:
    """Attach original QQ message identity before segments are combined."""
    segments = event.get("message")
    if not isinstance(segments, list) or not any(is_visual_segment(segment) for segment in segments):
        return event

    sender = event.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    owner = _text(sender_id if sender_id is not None else event.get("user_id"))
    name = (
        _text(sender_name)
        if sender_name is not None
        else _text(sender.get("card")) or _text(sender.get("nickname")) or owner
    )
    keep_identity = origin not in {"current", "quoted"}
    marked_segments = deepcopy(segments)
    for index, segment in enumerate(marked_segments):
        if not is_visual_segment(segment):
            continue
        data = segment_data(segment).copy()
        metadata = {
            MESSAGE_ID_META_KEY: _text(message_id if message_id is not None else event.get("message_id")),
            SENDER_ID_META_KEY: owner,
            SENDER_NAME_META_KEY: name,
            SENT_AT_META_KEY: _timestamp(sent_at if sent_at is not None else event.get("time")),
            SEGMENT_INDEX_META_KEY: index,
            MESSAGE_ORDER_META_KEY: message_order,
        }
        for key, value in metadata.items():
            if keep_identity:
                data.setdefault(key, value)
            else:
                data[key] = value
        data[ORIGIN_META_KEY] = origin
        segment["data"] = data
    return {**event, "message": marked_segments}


def image_origin_from_segment(
    segment: dict[str, Any], *, index: int
) -> ImageOrigin:
    data = segment_data(segment)
    sender_name = _text(data.get(SENDER_NAME_META_KEY))
    default_origin = "legacy_other" if sender_name else "current"
    origin = _text(data.get(ORIGIN_META_KEY)) or default_origin
    return ImageOrigin(
        message_id=_text(data.get(MESSAGE_ID_META_KEY)),
        segment_index=_integer(data.get(SEGMENT_INDEX_META_KEY), index),
        sender_id=_text(data.get(SENDER_ID_META_KEY)),
        sender_name=sender_name,
        sent_at=_timestamp(data.get(SENT_AT_META_KEY)),
        message_order=_integer(data.get(MESSAGE_ORDER_META_KEY), 0),
        origin=origin if origin in IMAGE_ORIGIN_PRIORITY else "legacy_other",
    )
