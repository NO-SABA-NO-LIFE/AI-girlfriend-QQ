from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator
from typing import Any

from ..toolbox_parts.visual_origin import (
    ORIGIN_META_KEY,
    VISUAL_SEGMENT_TYPES,
    image_origin_from_segment,
    is_visual_segment,
    segment_data,
)
from .rule_common import HISTORY_LIMIT

_IMAGE_REF_LIMIT = 8
_IMAGE_HISTORY_TTL_SECONDS = 30 * 60
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,256}")
_FILE_TOKEN = re.compile(r"[A-Za-z0-9_.+=-]{1,256}")


def history_message_id(value: Any) -> str:
    if type(value) is int:
        value = str(value)
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else ""


def _file_token(value: Any) -> str:
    if type(value) is int:
        value = str(value)
    if (
        isinstance(value, str)
        and _FILE_TOKEN.fullmatch(value)
        and value not in {".", ".."}
        and "rkey=" not in value.lower()
    ):
        return value
    return ""


def _timestamp(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def normalize_image_refs(refs: Iterable[Any]) -> list[dict[str, Any]]:
    """Retain only usable message locators and safe, bounded metadata."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("origin", "current") != "current":
            continue
        message_id = history_message_id(ref.get("message_id"))
        index = ref.get("segment_index")
        kind = ref.get("kind")
        if (
            not message_id
            or type(index) is not int
            or index < 0
            or not isinstance(kind, str)
            or kind not in VISUAL_SEGMENT_TYPES
        ):
            continue
        key = (message_id, index)
        if key in seen:
            continue
        seen.add(key)
        sender_id = history_message_id(ref.get("sender_id"))
        sender_name = ref.get("sender_name")
        if not isinstance(sender_name, str) or "://" in sender_name or "rkey=" in sender_name.lower():
            sender_name = sender_id
        order = ref.get("message_order")
        result.append(
            {
                "message_id": message_id,
                "segment_index": index,
                "file_id": _file_token(ref.get("file_id")),
                "kind": kind,
                "origin": "current",
                "sender_id": sender_id,
                "sender_name": sender_name[:128],
                "sent_at": _timestamp(ref.get("sent_at")),
                "message_order": order if type(order) is int and order >= 0 else 0,
            }
        )
        if len(result) == _IMAGE_REF_LIMIT:
            break
    return result


def _event_image_refs(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    segments = event.get("message")
    if not isinstance(segments, list):
        return

    sender = event.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    for index, segment in enumerate(segments):
        if not is_visual_segment(segment):
            continue
        data = segment_data(segment)
        if ORIGIN_META_KEY in data:
            origin = image_origin_from_segment(segment, index=index)
            message_id = origin.message_id
            segment_index = origin.segment_index
            sender_id = origin.sender_id
            sender_name = origin.sender_name
            sent_at = origin.sent_at
            message_order = origin.message_order
            source = origin.origin
        else:
            message_id = event.get("message_id")
            segment_index = index
            sender_id = event.get("user_id")
            sender_name = sender.get("card") or sender.get("nickname")
            sent_at = event.get("time")
            message_order = 0
            source = "current"
        yield {
            "message_id": message_id,
            "segment_index": segment_index,
            "file_id": _file_token(data.get("file_id")) or _file_token(data.get("file")),
            "kind": str(segment["type"]).lower(),
            "origin": source,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "sent_at": sent_at,
            "message_order": message_order,
        }


def build_image_refs(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect the current message's images, including original IDs in a batch."""
    return normalize_image_refs(_event_image_refs(event))


def find_history_image_refs(
    history: Any, message_id: str | int, *, now: float,
) -> list[dict[str, Any]]:
    target = history_message_id(message_id)
    if not target or not isinstance(history, list):
        return []
    cutoff = now - _IMAGE_HISTORY_TTL_SECONDS
    for entry in reversed(history[-HISTORY_LIMIT:]):
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        at = _timestamp(entry.get("at"))
        refs = entry.get("image_refs")
        if not cutoff <= at <= now or not isinstance(refs, list):
            continue
        matches = [
            ref for ref in normalize_image_refs(refs)
            if ref["message_id"] == target and cutoff <= (ref["sent_at"] or at) <= now
        ]
        if matches:
            return sorted(matches, key=lambda ref: ref["segment_index"])
    return []
