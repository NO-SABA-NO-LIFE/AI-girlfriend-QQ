"""Durable, account-scoped work queue for deferred sticker review."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .core import _write_capture_metadata


CAPTURE_ID_PATTERN = re.compile(r"[a-f0-9]{12}")
_QQ_MEDIA_HOST_SUFFIXES = (".qpic.cn", ".gtimg.cn")
_QQ_MEDIA_HOSTS = frozenset({"gxh.vip.qq.com", "multimedia.nt.qq.com", "multimedia.nt.qq.com.cn"})
_IMAGE_FORMATS = {
    "BMP": (".bmp", "image/bmp"),
    "GIF": (".gif", "image/gif"),
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}

ActionCaller = Callable[[str, dict[str, Any], float], Awaitable[Any]]


@dataclass(frozen=True)
class HydrationRecord:
    capture_id: str
    account_id: str
    message_type: str
    group_id: str
    user_id: str
    message_id: str
    segment_index: int
    kind: str
    created_at: float
    status: str = "pending"
    attempts: int = 0
    last_error: str = ""
    output_name: str = ""
    lease_id: str = ""
    claimed_at: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    capture_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    output_name TEXT NOT NULL DEFAULT '',
    lease_id TEXT NOT NULL DEFAULT '',
    claimed_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, capture_id)
)
"""


class StickerHydrationQueue:
    def __init__(self, review_dir: Path) -> None:
        self.review_dir = review_dir
        self.path = review_dir / ".hydration.sqlite3"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=0.5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _expire_missing_review_file(
        self, connection: sqlite3.Connection, record: HydrationRecord,
    ) -> HydrationRecord:
        if record.status != "hydrated":
            return record
        name = record.output_name
        if Path(name).name == name and name.startswith(f"hydrate_{record.capture_id}."):
            if (self.review_dir / name).is_file():
                return record
        connection.execute(
            """
            UPDATE captures SET status = 'expired', output_name = '', last_error = 'review_image_expired'
            WHERE account_id = ? AND capture_id = ? AND status = 'hydrated'
            """,
            (record.account_id, record.capture_id),
        )
        return replace(record, status="expired", output_name="", last_error="review_image_expired")

    @contextmanager
    def approving_review_image(self, output_name: str) -> Iterator[None]:
        """Serialize approval with queue reads that reconcile missing review files."""
        if not self.path.is_file() or not re.fullmatch(
            r"hydrate_[a-f0-9]{12}\.[a-z]+", output_name
        ):
            yield
            return
        with self._connect() as connection:
            connection.execute(
                "UPDATE captures SET status = 'approved' WHERE status = 'hydrated' AND output_name = ?",
                (output_name,),
            )
            yield

    def register(self, records: list[HydrationRecord]) -> None:
        if not records:
            return
        self.review_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        with self._connect() as connection:
            connection.execute(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            for account_id, capture_id in {(item.account_id, item.capture_id) for item in records}:
                existing = connection.execute(
                    "SELECT * FROM captures WHERE account_id = ? AND capture_id = ? AND status = 'hydrated'",
                    (account_id, capture_id),
                ).fetchone()
                if existing is not None:
                    self._expire_missing_review_file(connection, HydrationRecord(**existing))
            connection.executemany(
                """
                INSERT INTO captures (
                    capture_id, account_id, message_type, group_id, user_id, message_id,
                    segment_index, kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (account_id, capture_id) DO UPDATE SET
                    message_id = excluded.message_id, segment_index = excluded.segment_index,
                    kind = excluded.kind, created_at = excluded.created_at,
                    attempts = CASE WHEN captures.status = 'expired' THEN 0 ELSE captures.attempts END,
                    last_error = CASE WHEN captures.status = 'expired' THEN '' ELSE captures.last_error END,
                    status = CASE WHEN captures.status = 'expired' THEN 'pending' ELSE captures.status END
                WHERE captures.status IN ('pending', 'queued', 'failed', 'unavailable', 'expired')
                  AND (excluded.message_id != captures.message_id OR excluded.segment_index != captures.segment_index)
                  AND excluded.created_at >= captures.created_at
                """,
                [
                    (
                        record.capture_id, record.account_id, record.message_type, record.group_id,
                        record.user_id, record.message_id, record.segment_index, record.kind,
                        record.created_at,
                    )
                    for record in records
                ],
            )

    def list_records(
        self, account_id: str, max_age_seconds: float = 172800,
    ) -> list[HydrationRecord]:
        if not self.path.is_file():
            return []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM captures WHERE account_id = ? AND created_at > ?
                ORDER BY created_at DESC, segment_index LIMIT 100
                """,
                (account_id, time.time() - max_age_seconds),
            )
            return [self._expire_missing_review_file(connection, HydrationRecord(**row)) for row in rows]

    def enqueue(
        self,
        account_id: str,
        capture_id: str,
        *,
        max_attempts: int = 3,
        max_age_seconds: float = 172800,
    ) -> HydrationRecord | None:
        if not CAPTURE_ID_PATTERN.fullmatch(capture_id) or not self.path.is_file():
            return None
        age_cutoff = time.time() - max_age_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM captures WHERE account_id = ? AND capture_id = ? AND created_at > ?",
                (account_id, capture_id, age_cutoff),
            ).fetchone()
            if existing is None:
                return None
            self._expire_missing_review_file(connection, HydrationRecord(**existing))
            connection.execute(
                """
                UPDATE captures SET status = 'queued',
                    attempts = CASE WHEN status IN ('unavailable', 'expired') THEN 0 ELSE attempts END
                WHERE account_id = ? AND capture_id = ? AND created_at > ?
                  AND (status IN ('unavailable', 'expired') OR (status IN ('pending', 'failed') AND attempts < ?))
                """,
                (account_id, capture_id, age_cutoff, max_attempts),
            )
            row = connection.execute(
                "SELECT * FROM captures WHERE account_id = ? AND capture_id = ? AND created_at > ?",
                (account_id, capture_id, age_cutoff),
            ).fetchone()
            return HydrationRecord(**row) if row else None

    def enqueue_all(
        self, account_id: str, *, max_age_seconds: float = 172800,
    ) -> int:
        if not self.path.is_file():
            return 0
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE captures SET status = 'queued'
                WHERE account_id = ? AND created_at > ? AND status = 'pending'
                """,
                (account_id, time.time() - max_age_seconds),
            )
            return updated.rowcount

    def claim(
        self,
        account_id: str,
        *,
        max_attempts: int = 3,
        stale_seconds: float = 60,
        max_age_seconds: float = 172800,
    ) -> HydrationRecord | None:
        if not self.path.is_file():
            return None
        now = time.time()
        stale_cutoff = now - stale_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM captures WHERE account_id = ? AND created_at <= ?
                  AND (status != 'hydrating' OR claimed_at <= ?)
                """,
                (account_id, now - max_age_seconds, stale_cutoff),
            )
            connection.execute(
                """
                UPDATE captures SET status = 'unavailable', last_error = 'retry_limit',
                    lease_id = '', claimed_at = 0
                WHERE account_id = ? AND attempts >= ?
                  AND (status = 'queued' OR (status = 'hydrating' AND claimed_at <= ?))
                """,
                (account_id, max_attempts, stale_cutoff),
            )
            row = connection.execute(
                """
                UPDATE captures
                SET status = 'hydrating', attempts = attempts + 1, lease_id = ?, claimed_at = ?
                WHERE account_id = ? AND capture_id = (
                    SELECT capture_id FROM captures WHERE account_id = ? AND attempts < ?
                      AND (status = 'queued' OR (status = 'hydrating' AND claimed_at <= ?))
                    ORDER BY created_at, segment_index LIMIT 1
                )
                RETURNING *
                """,
                (secrets.token_hex(16), now, account_id, account_id, max_attempts, stale_cutoff),
            ).fetchone()
            return HydrationRecord(**row) if row else None

    def release(self, record: HydrationRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE captures SET status = 'queued', attempts = attempts - 1,
                    lease_id = '', claimed_at = 0
                WHERE account_id = ? AND capture_id = ? AND lease_id = ? AND status = 'hydrating'
                """,
                (record.account_id, record.capture_id, record.lease_id),
            )

    def fail(self, record: HydrationRecord, reason: str, max_attempts: int) -> str | None:
        status = "unavailable" if record.attempts >= max_attempts else "failed"
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE captures SET status = ?, last_error = ?, lease_id = '', claimed_at = 0
                WHERE account_id = ? AND capture_id = ? AND lease_id = ? AND status = 'hydrating'
                """,
                (status, reason, record.account_id, record.capture_id, record.lease_id),
            )
            return status if updated.rowcount else None

    def finish(
        self, record: HydrationRecord, content: bytes, suffix: str, content_type: str,
    ) -> bool:
        name = f"hydrate_{record.capture_id}{suffix}"
        target = self.review_dir / name
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE captures SET status = 'hydrated', last_error = '', output_name = ?,
                    lease_id = '', claimed_at = 0
                WHERE account_id = ? AND capture_id = ? AND lease_id = ? AND status = 'hydrating'
                """,
                (name, record.account_id, record.capture_id, record.lease_id),
            )
            if not updated.rowcount:
                return False
            descriptor, temporary_name = tempfile.mkstemp(dir=self.review_dir, prefix=".hydrate-")
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as file:
                    file.write(content)
                _write_capture_metadata(target, "", "review", content_type)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return True


def onebot_id(value: Any) -> str:
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        text = str(value).strip()
        if re.fullmatch(r"-?[0-9]{1,20}", text):
            number = int(text)
            if number:
                return str(number)
    return ""


def build_capture(
    event: dict[str, Any],
    account_id: str,
    message_id: str,
    segment_index: int,
    kind: str,
    data: dict[str, Any] | None = None,
) -> HydrationRecord | None:
    receiver = onebot_id(event.get("self_id"))
    message_type = event.get("message_type")
    user_id = onebot_id(event.get("user_id"))
    group_id = onebot_id(event.get("group_id")) if message_type == "group" else ""
    if (
        not account_id or (receiver and receiver != account_id) or not user_id
        or user_id == account_id or not message_id
        or message_type not in ("group", "private") or (message_type == "group" and not group_id)
    ):
        return None
    identity: tuple[Any, ...] = (account_id, message_type, group_id, user_id, message_id, segment_index)
    if data is not None:
        # Use only stable vendor identifiers. Expiring URLs, opaque session
        # tokens and summaries are never persisted or used to merge captures.
        md5 = str(data.get("md5") or "")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", md5):
            filename = re.fullmatch(r"([a-fA-F0-9]{32})\.(?:image|png|jpe?g|gif|webp)",
                                    str(data.get("file") or ""), flags=re.IGNORECASE)
            md5 = filename[1] if filename else ""
        emoji = str(data.get("emoji_id") or data.get("emojiId") or "")
        package = str(data.get("emoji_package_id") or data.get("emojiPackageId") or "")
        if md5:
            identity = (account_id, message_type, group_id, user_id, kind, "md5", md5.lower())
        elif kind in {"mface", "marketface"} and re.fullmatch(r"[a-fA-F0-9]{32}", emoji):
            identity = (account_id, message_type, group_id, user_id, "marketface", emoji.lower())
        elif kind in {"mface", "marketface"} and emoji.isascii() and package.isascii() and (
            emoji.isdigit() and package.isdigit() and len(emoji) <= 20 and len(package) <= 20
        ):
            identity = (account_id, message_type, group_id, user_id, "marketface", package, emoji)
    return HydrationRecord(
        capture_id=hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:12],
        account_id=account_id,
        message_type=message_type,
        group_id=group_id,
        user_id=user_id,
        message_id=message_id,
        segment_index=segment_index,
        kind=kind,
        created_at=time.time(),
    )


def _scoped_image(message: dict[str, Any], record: HydrationRecord) -> dict[str, Any] | None:
    sender = message.get("sender")
    sender_id = onebot_id(message.get("user_id"))
    if isinstance(sender, dict):
        nested_sender = onebot_id(sender.get("user_id"))
        if nested_sender and sender_id and nested_sender != sender_id:
            return None
        sender_id = sender_id or nested_sender
    receiver = onebot_id(message.get("self_id"))
    if (
        (receiver and receiver != record.account_id)
        or onebot_id(message.get("message_id")) != record.message_id
        or message.get("message_type") != record.message_type
        or onebot_id(message.get("group_id")) != record.group_id
        or sender_id != record.user_id
    ):
        return None
    segments = message.get("message")
    index = record.segment_index
    if not isinstance(segments, list) or not 0 <= index < len(segments):
        return None
    segment = segments[index]
    if not isinstance(segment, dict) or segment.get("type") != record.kind:
        return None
    data = segment.get("data")
    return data if isinstance(data, dict) else None


async def _action_data(
    caller: ActionCaller, action: str, params: dict[str, Any], timeout: float,
) -> dict[str, Any] | None:
    try:
        async with asyncio.timeout(timeout):
            response = await caller(action, params, timeout)
    except (TimeoutError, OSError):
        return None
    if not isinstance(response, dict) or response.get("retcode", 0) != 0:
        return None
    data = response.get("data")
    return data if isinstance(data, dict) else None


def _qq_media_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme not in ("http", "https") or parts.username is not None
            or parts.port not in (None, 80, 443)
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or not (host in _QQ_MEDIA_HOSTS or host.endswith(_QQ_MEDIA_HOST_SUFFIXES))
        ):
            return None
    except ValueError:
        return None
    return urlunsplit(("https", host, parts.path, parts.query, ""))


def _verify_image(content: bytes) -> tuple[bytes, str, str] | None:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(content)) as image:
            file_type = _IMAGE_FORMATS.get(image.format or "")
            if not file_type or image.width * image.height > 16_000_000:
                return None
            image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None
    return content, *file_type


async def _download_image(value: str, max_bytes: int) -> tuple[bytes, str, str] | None:
    if value.startswith("base64://"):
        encoded = value[len("base64://"):]
        if len(encoded) > ((max_bytes + 2) // 3) * 4:
            return None
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return None
        if 0 < len(content) <= max_bytes:
            return await asyncio.to_thread(_verify_image, content)
        return None

    url = _qq_media_url(value)
    if url is None:
        return None

    import httpx

    received = bytearray()
    try:
        async with asyncio.timeout(10):
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_raw():
                        if len(received) + len(chunk) > max_bytes:
                            return None
                        received.extend(chunk)
    except (httpx.HTTPError, TimeoutError):
        return None
    return await asyncio.to_thread(_verify_image, bytes(received)) if received else None


async def _read_image(data: dict[str, Any], max_bytes: int) -> tuple[bytes, str, str] | None:
    for key in ("url", "file_url", "origin_url", "preview", "file"):
        value = data.get(key)
        if isinstance(value, str) and value:
            image = await _download_image(value, max_bytes)
            if image is not None:
                return image
    return None


async def fetch_captured_image(
    record: HydrationRecord, caller: ActionCaller, max_bytes: int,
) -> tuple[tuple[bytes, str, str] | None, str]:
    message = await _action_data(caller, "get_msg", {"message_id": int(record.message_id)}, 3)
    if message is None:
        return None, "message_unavailable"
    data = _scoped_image(message, record)
    if data is None:
        return None, "source_mismatch"
    image = await _read_image(data, max_bytes)
    if image is not None:
        return image, ""

    file_key = data.get("file_id") or data.get("file")
    if not isinstance(file_key, str) or not re.fullmatch(r"[A-Za-z0-9_.=-]{1,256}", file_key):
        return None, "image_unavailable"

    group_params = {"group_id": int(record.group_id)} if record.group_id else {}
    for action, params in (
        ("get_image", {"file": file_key}),
        ("get_file", {"file": file_key, **group_params}),
        ("get_file", {"file_id": file_key, **group_params}),
    ):
        result = await _action_data(caller, action, params, 5)
        if result is not None:
            image = await _read_image(result, max_bytes)
            if image is not None:
                return image, ""
    return None, "image_unavailable"
