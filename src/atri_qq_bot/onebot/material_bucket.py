"""Choose when an inbound message should wait for its accompanying image."""

from __future__ import annotations

import re
from typing import Any

from ..toolbox_parts.visual_origin import is_visual_segment
from .message_parser import extract_plain_text, extract_reply_message_id
from .reply_policy import is_bot_mentioned

_IMAGE_CUE = re.compile(
    r"(?:看|瞅|瞧)(?:一下|下|这个|这张|这图|图|照片|截图)"
    r"|(?:给你|我(?:再|刚)?发|发你|下面|接着).{0,8}(?:图|照片|截图|表情)"
    r"|(?:这张图|截图|照片)(?:里|上|中)"
)


def material_wait_seconds(event: dict[str, Any], config: Any, base_seconds: float) -> float:
    if not getattr(config, "material_bucket_enabled", False):
        return base_seconds
    message = event.get("message")
    if (
        event.get("post_type") != "message"
        or event.get("_atri_poke_event")
        or event.get("_atri_group_increase_event")
        or extract_reply_message_id(message)
        or isinstance(message, list) and any(is_visual_segment(item) for item in message)
    ):
        return base_seconds
    plain_text = extract_plain_text(message)
    if len(plain_text) > 120 or not _IMAGE_CUE.search(plain_text):
        return base_seconds
    if event.get("message_type") == "private":
        return max(base_seconds, config.material_bucket_private_seconds)
    if event.get("message_type") == "group":
        if is_bot_mentioned(event, config.bot_qq, plain_text):
            return max(base_seconds, config.material_bucket_at_seconds)
        return max(base_seconds, config.material_bucket_group_seconds)
    return base_seconds
