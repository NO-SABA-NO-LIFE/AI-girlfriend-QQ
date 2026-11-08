"""StickerManager 审核模式的单元测试。

验证：
- 审核模式下新图落 _review 待审核区、不调用分类器；
- 关闭审核模式后仍走视觉/文字分类流程；
- 待审核区路径位于 sticker_dir 下且以下划线开头；
- 待审核图不会被发送选择逻辑（下划线目录）选中。
本地无 .env、不直连中转站，用 monkeypatch 替换 httpx.AsyncClient。
"""
from __future__ import annotations

import asyncio

from atri_qq_bot.stickers import StickerManager


class _FakeResponse:
    def __init__(self, content: bytes, content_type: str = "image/png") -> None:
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        return None


class _FakeDownloadClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> "_FakeDownloadClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        return self._response


def _image_event(url: str = "http://example.com/a.png") -> dict:
    return {"message": [{"type": "image", "data": {"url": url}}]}


def _patch_download(monkeypatch) -> None:
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: _FakeDownloadClient(_FakeResponse(b"png-bytes")),
    )


def _make_manager(tmp_path, review_enabled: bool, classifier=None) -> StickerManager:
    sticker_dir = tmp_path / "lib"
    capture_dir = tmp_path / "capture"
    return StickerManager(
        sticker_dir,
        capture_dir=capture_dir,
        emotion_classifier=classifier,
        review_enabled=review_enabled,
    )


def test_review_dir_is_underscore_under_sticker_dir(tmp_path) -> None:
    manager = _make_manager(tmp_path, review_enabled=True)
    assert manager.review_dir == (tmp_path / "lib" / "_review")
    assert manager.review_dir.name.startswith("_")


def test_review_mode_stores_in_review_without_classifier(
    tmp_path, monkeypatch
) -> None:
    async def classifier(content: bytes, mime: str, hint: str) -> str:
        raise AssertionError("classifier must not run in review mode")

    manager = _make_manager(tmp_path, review_enabled=True, classifier=classifier)
    _patch_download(monkeypatch)

    saved = asyncio.run(manager.capture_from_event(_image_event(), "我生气了"))

    assert len(saved) == 1
    assert saved[0].parent.name == "_review"
    # Sidecar metadata is written alongside the pending image.
    assert saved[0].with_suffix(saved[0].suffix + ".json").exists()


def test_review_disabled_runs_classification(tmp_path, monkeypatch) -> None:
    async def classifier(content: bytes, mime: str, hint: str) -> str:
        return "happy"

    manager = _make_manager(tmp_path, review_enabled=False, classifier=classifier)
    _patch_download(monkeypatch)

    saved = asyncio.run(manager.capture_from_event(_image_event(), ""))

    assert len(saved) == 1
    assert saved[0].parent.name == "happy"


def test_review_images_are_not_selected_for_sending(
    tmp_path, monkeypatch
) -> None:
    manager = _make_manager(tmp_path, review_enabled=True)
    _patch_download(monkeypatch)
    asyncio.run(manager.capture_from_event(_image_event(), "我生气了"))

    # The underscore-prefixed review area must never be picked for sending.
    picked = manager._pick_any_primary_image()
    assert picked is None
