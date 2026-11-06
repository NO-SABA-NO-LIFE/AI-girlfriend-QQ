from __future__ import annotations

from typing import Any

from .schema import OutgoingMessage


def outgoing_to_onebot_message(message: OutgoingMessage) -> str | list[dict[str, Any]]:
    if message.kind == "text":
        return message.content
    if message.kind == "face":
        face_id: int | str = int(message.content) if message.content.isdigit() else message.content
        return [{"type": "face", "data": {"id": face_id}}]
    if message.kind == "record":
        return [{"type": "record", "data": {"file": message.content, "cache": 0}}]
    if message.kind == "sticker":
        return [
            {
                "type": "image",
                "data": {
                    "file": message.content,
                    "cache": 0,
                    "sub_type": message.sub_type or 1,
                    "summary": message.summary or "[动画表情]",
                },
            }
        ]
    return [{"type": "image", "data": {"file": message.content, "cache": 0}}]


def outgoing_to_segments(message: OutgoingMessage) -> list[dict[str, Any]]:
    """与 :func:`outgoing_to_onebot_message` 同源，但统一返回消息段数组。

    多条 :class:`OutgoingMessage` 合并成一条 QQ 消息时，需要每个元素都是段
    （纯文本也要包成 ``text`` 段），才能把它们 flatten 到同一个 ``message`` 数组。
    空文本返回空列表，避免注入空段。
    """
    converted = outgoing_to_onebot_message(message)
    if isinstance(converted, str):
        return (
            [{"type": "text", "data": {"text": converted}}] if converted else []
        )
    return converted
