"""StickerManager 视觉情绪分类存图的单元测试。

验证：
- 无分类器时沿用文字情绪分类；
- 注入分类器后按视觉标签落盘；
- 分类器异常或返回 neutral 时分别回退文字情绪 / 归入 unsorted；
- 下载失败或无图片段时不产生文件。
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
    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc

    async def __aenter__(self) -> "_FakeDownloadClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


def _image_event(url: str = "http://example.com/a.png") -> dict:
    return {"message": [{"type": "image", "data": {"url": url}}]}


def _make_manager(tmp_path, classifier=None) -> StickerManager:
    sticker_dir = tmp_path / "lib"
    capture_dir = tmp_path / "capture"
    return StickerManager(sticker_dir, capture_dir=capture_dir, emotion_classifier=classifier)


def _patch_download(monkeypatch, response=None, exc=None) -> None:
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: _FakeDownloadClient(response=response, exc=exc),
    )


def test_capture_without_classifier_uses_text_emotion(tmp_path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    _patch_download(monkeypatch, response=_FakeResponse(b"png-bytes"))

    saved = asyncio.run(manager.capture_from_event(_image_event(), "我生气了"))

    assert len(saved) == 1
    assert saved[0].parent.name == "angry"


def test_capture_with_vision_classifier_uses_vision_label(tmp_path, monkeypatch) -> None:
    async def classifier(content: bytes, mime: str, hint: str) -> str:
        return "happy"

    manager = _make_manager(tmp_path, classifier=classifier)
    _patch_download(monkeypatch, response=_FakeResponse(b"png-bytes"))

    saved = asyncio.run(manager.capture_from_event(_image_event(), ""))

    assert len(saved) == 1
    assert saved[0].parent.name == "happy"


def test_capture_falls_back_when_classifier_raises(tmp_path, monkeypatch) -> None:
    async def broken_classifier(content: bytes, mime: str, hint: str) -> str:
        raise RuntimeError("vision down")

    manager = _make_manager(tmp_path, classifier=broken_classifier)
    _patch_download(monkeypatch, response=_FakeResponse(b"png-bytes"))

    saved = asyncio.run(manager.capture_from_event(_image_event(), "我生气了"))

    assert len(saved) == 1
    # Vision failed; text emotion (angry) must still decide the directory.
    assert saved[0].parent.name == "angry"


def test_capture_vision_neutral_goes_unsorted(tmp_path, monkeypatch) -> None:
    async def classifier(content: bytes, mime: str, hint: str) -> str:
        return "neutral"

    manager = _make_manager(tmp_path, classifier=classifier)
    _patch_download(monkeypatch, response=_FakeResponse(b"png-bytes"))

    saved = asyncio.run(manager.capture_from_event(_image_event(), ""))

    assert len(saved) == 1
    assert saved[0].parent.name == "unsorted"


def test_capture_skips_on_download_failure(tmp_path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    _patch_download(monkeypatch, exc=RuntimeError("network"))

    saved = asyncio.run(manager.capture_from_event(_image_event(), "我生气了"))

    assert saved == []
    assert not any((tmp_path / "capture").rglob("*"))


def test_capture_ignores_non_image_segments(tmp_path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    # If a download is attempted, this would raise and fail the test.
    _patch_download(monkeypatch, exc=AssertionError("should not download"))
    event = {"message": [{"type": "text", "data": {"text": "hi"}}]}

    saved = asyncio.run(manager.capture_from_event(event, "hi"))

    assert saved == []


def test_capture_disabled_returns_empty(tmp_path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    _patch_download(monkeypatch, response=_FakeResponse(b"x"))

    saved = asyncio.run(
        manager.capture_from_event(_image_event(), "x", enabled=False)
    )

    assert saved == []
