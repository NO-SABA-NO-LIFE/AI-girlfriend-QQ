"""Privacy-preserving operational logs for the QQ media pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..toolbox_parts.visual_origin import ImageOrigin
from .paths import LOG_DIR

_LOCK = threading.Lock()
_RETENTION_SECONDS = 7 * 86400
_LOGGER = logging.getLogger("atri.media")


def message_key(value: Any) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"-?[0-9]{1,20}", text) else ""


def identifier_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:12] if value else ""


def _append(stream: str, rows: list[dict], directory: Path) -> None:
    if not rows:
        return
    now = time.time()
    path = directory / f"{stream}.jsonl"
    today = datetime.fromtimestamp(now, timezone.utc).date()
    try:
        with _LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            previous = path.stat().st_mtime if path.exists() else None
            previous_day = datetime.fromtimestamp(previous, timezone.utc).date() if previous is not None else None
            if previous_day is not None and previous_day != today:
                path.replace(directory / f"{stream}-{previous_day}.jsonl")
            if previous_day != today:
                for old in directory.glob(f"{stream}-????-??-??.jsonl"):
                    if old.stat().st_mtime < now - _RETENTION_SECONDS:
                        old.unlink()
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as file:
                file.write("".join(json.dumps({"ts": now, **row}) + "\n" for row in rows))
    except OSError as exc:
        _LOGGER.debug("媒体日志写入失败 stream=%s error=%s", stream, type(exc).__name__)


def log_attribution(
    directory: Path, conversation: str, origins: list[ImageOrigin], dropped: list[ImageOrigin]
) -> None:
    conversation_hash = identifier_hash(conversation)
    rows = []
    for image, selected in [*((item, True) for item in origins), *((item, False) for item in dropped)]:
        rows.append({
            "conversation_hash": conversation_hash,
            "sender_hash": identifier_hash(image.sender_id),
            "message_id": message_key(image.message_id),
            "segment_index": image.segment_index,
            "message_order": image.message_order,
            "origin": image.origin,
            "evidence_level": image.evidence_level,
            "status": image.status,
            "elapsed_ms": image.elapsed_ms,
            "reason": image.reason,
            "selected": selected,
            "dropped": not selected,
        })
    _append("visual-attribution", rows, directory)


def log_visual_budget(directory: Path, record: dict) -> None:
    _append("visual-budget", [record], directory)


def log_hydration(
    capture_id: str, message_id: str, account_id: str, *, status: str,
    attempts: int = 0, reason: str = "", elapsed_ms: int = 0, directory: Path = LOG_DIR,
) -> None:
    _append("sticker-hydration", [{
        "capture_id": capture_id,
        "message_id": message_key(message_id),
        "account_hash": identifier_hash(account_id),
        "status": status,
        "attempts": attempts,
        "reason": reason,
        "elapsed_ms": elapsed_ms,
        "queue_mode": "sqlite",
    }], directory)
