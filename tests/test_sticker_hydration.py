import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from atri_qq_bot.onebot.message_batch import _merge_message_batch
from atri_qq_bot.sticker_parts import hydration
from atri_qq_bot.sticker_parts.hydration import StickerHydrationQueue
from atri_qq_bot.stickers import StickerManager
from atri_qq_bot.toolbox_parts.visual_origin import stamp_visual_segments


_ENCODED_GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
_GIF = base64.b64decode(_ENCODED_GIF)
_INLINE_GIF = "base64://" + _ENCODED_GIF
_UNFETCHED_URL = "https://gchat.qpic.cn/example?token=temporary"


def _image_event():
    return {
        "post_type": "message",
        "self_id": 123,
        "message_type": "group",
        "group_id": 456,
        "user_id": 789,
        "message_id": 1234,
        "sender": {"user_id": 789, "nickname": "sender"},
        "message": [
            {"type": "image", "data": {"url": _UNFETCHED_URL}},
            {"type": "text", "data": {"text": "看这个"}},
        ],
    }


def _lookup_message(**overrides):
    event = _image_event()
    event["message"][0]["data"] = {"file": _INLINE_GIF}
    event.update(overrides)
    return {"status": "ok", "retcode": 0, "data": event}


def _deferred_manager(sticker_dir):
    return StickerManager(
        sticker_dir,
        review_enabled=True,
        defer_capture=True,
        capture_account_id=123,
    )


def _register_request(manager):
    assert asyncio.run(manager.capture_from_event(_image_event(), "看这个")) == []
    queue = StickerHydrationQueue(manager.review_dir)
    [record] = queue.list_records("123")
    return queue, record


def test_legacy_review_mode_still_downloads_during_capture(tmp_path, monkeypatch):
    manager = StickerManager(tmp_path, review_enabled=True)
    downloads = []

    async def download(url, max_bytes):
        downloads.append((url, max_bytes))
        return _GIF, "image/gif", ".gif"

    monkeypatch.setattr(manager, "_fetch_sticker_bytes", download)

    [path] = asyncio.run(manager.capture_from_event(_image_event(), "看这个", max_bytes=100))

    assert downloads == [(_UNFETCHED_URL, 100)]
    assert path.parent == manager.review_dir
    assert path.read_bytes() == _GIF


def test_deferred_capture_deduplicates_without_fetching_or_materializing(tmp_path, monkeypatch):
    manager = _deferred_manager(tmp_path)

    async def unexpected_download(*args, **kwargs):
        pytest.fail("deferred capture downloaded the image")

    monkeypatch.setattr(manager, "_fetch_sticker_bytes", unexpected_download)
    queue, record = _register_request(manager)
    assert asyncio.run(manager.capture_from_event(_image_event(), "重复投递")) == []

    [duplicate] = queue.list_records("123")
    assert duplicate.capture_id == record.capture_id
    assert duplicate.status == "pending"
    assert duplicate.segment_index == 0
    assert not list(manager.review_dir.glob("*.gif"))
    assert queue.list_records("124") == []
    assert queue.enqueue("124", record.capture_id) is None


def test_deferred_capture_keeps_original_message_id_and_zero_based_segment(tmp_path):
    manager = _deferred_manager(tmp_path)
    event = _image_event()
    before = {**event, "message_id": 1233, "message": "发张图"}
    after = {**event, "message_id": 1235, "message": "看这个"}
    connection = object()
    _, merged = _merge_message_batch([
        (connection, before),
        (connection, stamp_visual_segments(event)),
        (connection, after),
    ])

    assert asyncio.run(manager.capture_from_event(merged, "看这个")) == []

    queue = StickerHydrationQueue(manager.review_dir)
    [record] = queue.list_records("123")
    assert str(record.message_id) == "1234"
    assert record.segment_index == 0


def test_concurrent_duplicate_requests_cannot_claim_same_capture_twice(tmp_path):
    manager = _deferred_manager(tmp_path)
    queue, record = _register_request(manager)
    second_queue = StickerHydrationQueue(manager.review_dir)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(queue.enqueue, "123", record.capture_id)
        second = pool.submit(second_queue.enqueue, "123", record.capture_id)
        assert first.result().capture_id == second.result().capture_id == record.capture_id

        first_claim = pool.submit(queue.claim, "123")
        second_claim = pool.submit(second_queue.claim, "123")
        claims = [claim for claim in (first_claim.result(), second_claim.result()) if claim is not None]

    [claimed] = claims
    assert claimed.capture_id == record.capture_id
    assert queue.enqueue("123", record.capture_id).capture_id == record.capture_id
    assert second_queue.claim("123") is None


def test_sticker_resend_deduplicates_by_stable_asset_within_conversation(tmp_path):
    manager = _deferred_manager(tmp_path)
    event = _image_event()
    event["message"][0]["data"]["file"] = "a" * 32 + ".gif"
    queue = StickerHydrationQueue(manager.review_dir)
    asyncio.run(manager.capture_from_event(event, ""))
    asyncio.run(manager.capture_from_event({**event, "message_id": 1235}, ""))
    [record] = queue.list_records("123")
    assert record.message_id == "1235"
    asyncio.run(manager.capture_from_event({**event, "message_id": 1236, "group_id": 789}, ""))
    assert len(queue.list_records("123")) == 2


def test_manual_retry_after_limit_restarts_only_the_selected_task(tmp_path):
    manager = _deferred_manager(tmp_path)
    queue, record = _register_request(manager)
    for _ in range(3):
        queue.enqueue("123", record.capture_id)
        task = queue.claim("123")
        queue.fail(task, "image_unavailable", max_attempts=3)
    [unavailable] = queue.list_records("123")
    assert unavailable.status == "unavailable"
    assert unavailable.attempts == 3
    assert queue.enqueue_all("123") == 0
    assert queue.claim("123") is None
    assert queue.enqueue("123", record.capture_id).attempts == 0
    assert queue.claim("123").attempts == 1


def test_restarted_worker_reclaims_stale_task_without_allowing_late_write(tmp_path, monkeypatch):
    now = 1800000000.0
    monkeypatch.setattr(hydration, "time", SimpleNamespace(time=lambda: now))
    manager = _deferred_manager(tmp_path)
    queue, record = _register_request(manager)
    assert queue.enqueue("123", record.capture_id) is not None
    first_claim = queue.claim("123")
    assert first_claim.attempts == 1

    restarted = StickerHydrationQueue(manager.review_dir)
    now += 5
    assert restarted.claim("123", stale_seconds=60) is None
    now += 56
    second_claim = restarted.claim("123", stale_seconds=60)
    assert second_claim.capture_id == first_claim.capture_id
    assert second_claim.attempts == 2

    queue.finish(first_claim, _GIF, ".gif", "image/gif")
    assert not list(manager.review_dir.glob("*.gif"))
    restarted.finish(second_claim, _GIF, ".gif", "image/gif")
    [saved] = queue.list_records("123")
    assert saved.status == "hydrated"
    assert (manager.review_dir / saved.output_name).read_bytes() == _GIF


def test_worker_loads_exact_image_from_lookup_and_stores_it_for_review(tmp_path):
    manager = _deferred_manager(tmp_path)
    queue, record = _register_request(manager)
    assert queue.enqueue("123", record.capture_id) is not None
    actions = []

    async def call_action(action, params, timeout_seconds):
        actions.append(action)
        assert action == "get_msg"
        assert str(params["message_id"]) == "1234"
        assert timeout_seconds > 0
        return _lookup_message()

    assert asyncio.run(manager.hydrate_pending(call_action)) is True
    assert asyncio.run(manager.hydrate_pending(call_action)) is False

    [hydrated] = queue.list_records("123")
    assert hydrated.capture_id == record.capture_id
    assert actions == ["get_msg"]
    assert hydrated.status == "hydrated"
    image = manager.review_dir / hydrated.output_name
    assert image.suffix == ".gif"
    assert image.read_bytes() == _GIF


def test_worker_uses_file_token_from_verified_message_for_image_action(tmp_path):
    manager = _deferred_manager(tmp_path)
    queue, record = _register_request(manager)
    assert queue.enqueue("123", record.capture_id) is not None
    actions = []

    async def call_action(action, params, timeout_seconds):
        actions.append(action)
        if action == "get_msg":
            return _lookup_message(
                message=[{"type": "image", "data": {"file_id": "saved-gif"}}]
            )
        assert action == "get_image"
        assert params == {"file": "saved-gif"}
        return {"status": "ok", "retcode": 0, "data": {"file": _INLINE_GIF}}

    assert asyncio.run(manager.hydrate_pending(call_action)) is True

    [review] = queue.list_records("123")
    assert review.status == "hydrated"
    assert actions == ["get_msg", "get_image"]


def test_worker_rejects_inline_image_larger_than_capture_limit(tmp_path):
    manager = _deferred_manager(tmp_path)
    queue, record = _register_request(manager)
    assert queue.enqueue("123", record.capture_id) is not None

    async def call_action(action, params, timeout_seconds):
        assert action == "get_msg"
        return _lookup_message()

    assert asyncio.run(manager.hydrate_pending(call_action, max_bytes=len(_GIF) - 1, max_attempts=1))

    [failed] = queue.list_records("123")
    assert failed.status == "unavailable"
    assert not failed.output_name


def test_worker_rejects_unapproved_download_host_and_local_image_path(tmp_path, monkeypatch):
    manager = _deferred_manager(tmp_path)
    queue, record = _register_request(manager)
    assert queue.enqueue("123", record.capture_id) is not None
    local_image = tmp_path / "other.gif"
    local_image.write_bytes(_GIF)

    def unexpected_client(*args, **kwargs):
        pytest.fail("unapproved download host was contacted")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)

    async def call_action(action, params, timeout_seconds):
        assert action == "get_msg"
        return _lookup_message(message=[{
            "type": "image",
            "data": {"url": "http://127.0.0.1/other.gif", "file": str(local_image)},
        }])

    assert asyncio.run(manager.hydrate_pending(call_action, max_attempts=1)) is True

    [failed] = queue.list_records("123")
    assert failed.status == "unavailable"
    assert not failed.output_name


@pytest.mark.parametrize(
    "overrides",
    [
        {"self_id": 124},
        {"group_id": 457},
        {"message_id": 1235},
        {"user_id": 790, "sender": {"user_id": 790}},
        {"message": [{"type": "text", "data": {"text": "不同的段"}}]},
    ],
    ids=["account", "group", "message", "sender", "segment"],
)
def test_worker_does_not_download_material_from_a_mismatched_lookup(tmp_path, overrides):
    manager = _deferred_manager(tmp_path)
    queue, record = _register_request(manager)
    assert queue.enqueue("123", record.capture_id) is not None

    async def call_action(action, params, timeout_seconds):
        assert action == "get_msg"
        return _lookup_message(**overrides)

    assert asyncio.run(manager.hydrate_pending(call_action, max_attempts=1)) is True

    [failed] = queue.list_records("123")
    assert failed.status == "unavailable"
    assert failed.attempts == 1
    assert failed.last_error
    assert not failed.output_name
    assert not list(manager.review_dir.glob("*.gif"))


def test_image_reader_prefers_file_over_preview(monkeypatch):
    calls: list[str] = []

    async def fake_download(value, max_bytes):
        calls.append(value)
        if value == "full":
            return _GIF, ".gif", "image/gif"
        return None

    monkeypatch.setattr(hydration, "_download_image", fake_download)
    image = asyncio.run(
        hydration._read_image({"file": "full", "preview": "thumb"}, 1000)
    )
    assert image is not None
    assert calls == ["full"]
