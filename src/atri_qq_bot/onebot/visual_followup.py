"""In-memory evidence for exact follow-ups on OneBot images."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from typing import Any

from ..toolbox_parts.analysis_result import ToolAnalysisResult
from ..toolbox_parts.visual_origin import ImageOrigin
from .message_parser import extract_reply_message_id


def referenced_message_id(event: dict[str, Any]) -> str:
    message_id = extract_reply_message_id(event.get("message"))
    if message_id:
        return str(message_id)
    reply = event.get("reply")
    return str(reply.get("id") or reply.get("message_id") or "") if isinstance(reply, dict) else ""


@dataclass(frozen=True)
class CachedImage:
    origin: ImageOrigin
    result: ToolAnalysisResult | None


class VisualFollowupCache:
    def __init__(self) -> None:
        self._conversations: OrderedDict[str, deque[tuple[float, list[CachedImage]]]] = OrderedDict()

    def remember(
        self, conversation_id: str, context: ToolAnalysisResult, *, now: float, max_bytes: int
    ) -> None:
        if not isinstance(context, ToolAnalysisResult) or not context.image_origins:
            return
        evidence = context.image_evidence
        images: list[CachedImage] = []
        for origin in (*context.image_origins, *context.dropped_images):
            if not origin.message_id or origin.segment_index < 0:
                continue
            result = evidence.get(origin.key)
            if result is not None:
                size = len(result.visual_data or b"")
                if size > max_bytes:
                    result = None
                else:
                    result = replace(result, knowledge_materials=[])
                    max_bytes -= size
            images.append(CachedImage(origin, result))
        if not images:
            return
        turns = self._conversations.setdefault(conversation_id, deque(maxlen=2))
        turns.append((now, images))
        self._conversations.move_to_end(conversation_id)
        if len(self._conversations) > 256:
            self._conversations.popitem(last=False)

    def find(
        self,
        conversation_id: str,
        *,
        sender_id: str,
        message_id: str,
        now: float,
    ) -> CachedImage | None:
        if message_id:
            matches = self.find_message(conversation_id, message_id, now=now)
            return matches[0] if len(matches) == 1 and matches[0].result is not None else None
        if sender_id:
            for at, images in reversed(self._conversations.get(conversation_id, ())):
                if now - at <= 900:
                    for image in reversed(images):
                        if image.origin.sender_id == sender_id and image.result is not None:
                            return image
        return None

    def find_message(self, conversation_id: str, message_id: str, *, now: float) -> list[CachedImage]:
        matches: dict[int, CachedImage] = {}
        if not message_id:
            return []
        for at, images in reversed(self._conversations.get(conversation_id, ())):
            if now - at > 900:
                continue
            for image in images:
                if image.origin.message_id != message_id:
                    continue
                index = image.origin.segment_index
                if index not in matches or matches[index].result is None:
                    matches[index] = image
        return [matches[index] for index in sorted(matches)]
