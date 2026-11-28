import json
import os
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlencode

import pytest

from atri_qq_bot.sticker_parts.hydration import StickerHydrationQueue, build_capture
from deploy.sticker_gallery import GalleryHandler, purge_expired_review


def test_gallery_hydration_requires_token_and_an_existing_capture(tmp_path):
    account_id = "123"
    capture = build_capture(
        {"message_type": "group", "group_id": 456, "user_id": 789},
        account_id, "8945", 0, "image",
    )
    assert capture is not None

    server = ThreadingHTTPServer(("127.0.0.1", 0), GalleryHandler)
    server.sticker_dir = tmp_path
    server.hydration_queue = StickerHydrationQueue(tmp_path / "_review")
    server.hydration_queue.register([capture])
    server.hydration_account_id = account_id
    server.hydration_token = "gallery-test-token"
    server.hydration_pending_ttl = 3600
    server.hydration_max_attempts = 3
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()

    def request(method, path, payload=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection = HTTPConnection(*server.server_address, timeout=3)
        try:
            body = json.dumps(payload) if payload is not None else None
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.getheader("Cache-Control"), response.read()
        finally:
            connection.close()

    try:
        status, _, home = request("GET", "/")
        assert status == 200
        assert capture.capture_id.encode() not in home
        assert server.hydration_token.encode() not in home
        assert request("GET", "/api/hydration")[0] == 401
        assert request(
            "POST", "/api/hydrate", {"capture_id": capture.capture_id}, token="wrong-token",
        )[0] == 401

        token = server.hydration_token
        status, cache, body = request("GET", "/api/hydration", token=token)
        assert (status, cache) == (200, "no-store")
        assert json.loads(body)["records"][0]["status"] == "pending"
        assert request("POST", "/api/hydrate", {"capture_id": "a" * 12}, token=token)[0] == 404
        assert request(
            "POST", "/api/hydrate", {"capture_id": capture.capture_id, "file": "extra"},
            token=token,
        )[0] == 400
        for _ in range(2):
            status, _, body = request("POST", "/api/hydrate", {"capture_id": capture.capture_id}, token)
            assert status == 200
            assert json.loads(body)["status"] == "queued"

        account_rows = server.hydration_queue.list_records(account_id)
        assert len(account_rows) == 1 and account_rows[0].attempts == 0
        server.hydration_token = ""
        assert request("GET", "/api/hydration", token=token)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("after_review", ["resend", "retry", "approve", "manual", "move_error"])
def test_gallery_and_worker_agree_on_review_file_lifecycle(tmp_path, monkeypatch, after_review):
    account_id = "123"
    message = {"message_type": "group", "group_id": 456, "user_id": 789}
    metadata = {"md5": "a" * 32}
    capture = build_capture(message, account_id, "8945", 0, "image", metadata)
    assert capture is not None
    worker_queue = StickerHydrationQueue(tmp_path / "_review")
    worker_queue.register([capture])
    worker_queue.enqueue(account_id, capture.capture_id)
    review = worker_queue.claim(account_id)
    assert review is not None
    assert worker_queue.finish(review, b"image fixture", ".png", "image/png")
    [finished] = worker_queue.list_records(account_id)
    source = worker_queue.review_dir / finished.output_name

    monkeypatch.setattr(GalleryHandler, "_vision_emotion", lambda _self, _path: "happy")
    server = ThreadingHTTPServer(("127.0.0.1", 0), GalleryHandler)
    server.sticker_dir = tmp_path
    server.hydration_queue = StickerHydrationQueue(worker_queue.review_dir)
    server.hydration_account_id = account_id
    server.hydration_token = "gallery-test-token"
    server.hydration_pending_ttl = 48 * 3600
    server.hydration_max_attempts = 3
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()

    def request(method, path, payload=None, *, form=False, expected_status=200):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
            "Authorization": f"Bearer {server.hydration_token}",
        }
        connection = HTTPConnection(*server.server_address, timeout=3)
        try:
            body = (urlencode(payload) if form else json.dumps(payload)) if payload is not None else None
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            assert response.status == expected_status
            return json.loads(response.read())
        finally:
            connection.close()

    try:
        resent = build_capture(message, account_id, "8946", 0, "image", metadata)
        assert resent is not None and resent.capture_id == capture.capture_id
        if after_review == "move_error":
            def fail_moving_sidecar(image, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                image.rename(target)
                raise OSError("fixture sidecar move failed")

            monkeypatch.setattr("deploy.sticker_gallery._move_with_sidecar", fail_moving_sidecar)
            request("POST", "/api/manual", {"path": f"_review/{source.name}", "emotion": "happy"},
                    form=True, expected_status=500)
            assert source.is_file()
            assert request("GET", "/api/hydration")["records"][0]["status"] == "hydrated"
        elif after_review in {"approve", "manual"}:
            request("POST", f"/api/{after_review}", {
                "path": f"_review/{source.name}", "emotion": "happy",
            }, form=True)
            assert not source.exists() and (tmp_path / "happy" / source.name).is_file()
            worker_queue.register([resent])
            assert request("GET", "/api/hydration")["records"][0]["status"] == "approved"
            assert worker_queue.claim(account_id) is None
        else:
            expired_at = time.time() - 2 * 3600 - 10
            os.utime(source, (expired_at, expired_at))
            assert purge_expired_review(tmp_path, 2 * 3600) == 1
            if after_review == "resend":
                worker_queue.register([resent])
                row, = request("GET", "/api/hydration")["records"]
                assert (row["status"], row["message_id"]) == ("pending", "8946")
            else:
                row, = request("GET", "/api/hydration")["records"]
                assert (row["status"], row["last_error"]) == ("expired", "review_image_expired")
            assert request("POST", "/api/hydrate", {"capture_id": resent.capture_id})["status"] == "queued"
            recovered = worker_queue.claim(account_id)
            assert recovered is not None
            assert recovered.attempts == 1
            assert recovered.message_id == ("8946" if after_review == "resend" else "8945")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
