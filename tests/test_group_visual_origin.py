from __future__ import annotations

from types import SimpleNamespace

from atri_qq_bot.onebot.server import OneBotServer
from atri_qq_bot.toolbox_parts.request_collection import (
    SENDER_NAME_META_KEY,
    _collect_request,
)

GROUP = "1051645731"
SHIORI_ID = "200001"  # 栞栞
TIANXIN_ID = "200002"  # 天心


def _image_event(user_id: str, nickname: str, file_name: str) -> dict:
    return {
        "message_type": "group",
        "group_id": GROUP,
        "user_id": user_id,
        "sender": {"card": nickname, "nickname": nickname},
        "message": [
            {
                "type": "image",
                "data": {
                    "url": f"https://example.com/{file_name}",
                    "file": file_name,
                },
            }
        ],
    }


def _poke_event(user_id: str, nickname: str) -> dict:
    return {
        "message_type": "group",
        "group_id": GROUP,
        "user_id": user_id,
        "sender": {"card": nickname, "nickname": nickname},
        "message": [
            {"type": "text", "data": {"text": "[用户戳了戳亚托莉]"}}
        ],
        "_atri_poke_event": True,
    }


def _server() -> OneBotServer:
    # 绕过 __init__，只装配近期群图功能依赖的最小属性。
    server = object.__new__(OneBotServer)
    server.config = SimpleNamespace(
        group_recent_visual_enabled=True,
        group_recent_visual_ttl_seconds=300,
        group_recent_visual_max=3,
    )
    server._recent_group_visuals = {}
    server._pending_group_visual_events = {}
    return server


def test_other_sender_image_is_marked_and_origin_hint_generated() -> None:
    """栞栞发图、天心随后戳一戳：图必须标注成栞栞发的，不能被天心认领。"""
    server = _server()

    # 栞栞发了一张“何伊薇”表情图。
    server._remember_pending_group_visual(
        _image_event(SHIORI_ID, "栞栞", "heyiwei.png")
    )
    # 天心戳一戳，事件本身没有图片，合并近期群图。
    poke = _poke_event(TIANXIN_ID, "天心")
    merged = server._event_with_recent_group_visuals(poke)

    image_segments = [
        seg for seg in merged["message"] if seg.get("type") == "image"
    ]
    assert len(image_segments) == 1
    # 图片段带上了真实发送者标注，且指向栞栞而不是天心。
    assert image_segments[0]["data"][SENDER_NAME_META_KEY] == "栞栞"

    # 下游收集请求时生成明确的归属提示。
    request = _collect_request(merged, "[用户戳了戳亚托莉]")
    assert any("图片归属" in hint and "栞栞" in hint for hint in request.share_hints)
    assert request.image_refs, "别人发的图仍作为视觉材料保留"


def test_own_sender_image_is_not_marked() -> None:
    """本人发的图、本人随后触发：归属天然正确，不加来源标注。"""
    server = _server()

    server._remember_pending_group_visual(
        _image_event(TIANXIN_ID, "天心", "own.png")
    )
    poke = _poke_event(TIANXIN_ID, "天心")
    merged = server._event_with_recent_group_visuals(poke)

    image_segments = [
        seg for seg in merged["message"] if seg.get("type") == "image"
    ]
    assert len(image_segments) == 1
    assert SENDER_NAME_META_KEY not in image_segments[0]["data"]

    request = _collect_request(merged, "[用户戳了戳亚托莉]")
    assert not any("图片归属" in hint for hint in request.share_hints)


def test_disabled_feature_keeps_event_unchanged() -> None:
    """功能关闭时不合并任何近期图片，行为退化为原样返回。"""
    server = _server()
    server.config.group_recent_visual_enabled = False

    server._remember_pending_group_visual(
        _image_event(SHIORI_ID, "栞栞", "heyiwei.png")
    )
    poke = _poke_event(TIANXIN_ID, "天心")
    merged = server._event_with_recent_group_visuals(poke)

    assert merged is poke
    assert not any(
        seg.get("type") == "image" for seg in merged["message"]
    )
