"""Companion 新闻订阅管理：会话列表 + 新闻指纹变更检测。

与 ``BilibiliMonitor`` 的订阅接口保持一致，但不含独立轮询循环——
变更检测由 ``main._companion_warmup_loop`` 在每次预热截图后驱动。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path

from ..core.push_policy import (
    MAX_PENDING_ENTRIES,
    MAX_PENDING_IMAGE_BYTES,
    PENDING_TTL_SECONDS,
    queued_timestamp,
)

try:
    from .._logging import logger
except Exception:  # pragma: no cover
    logger = logging.getLogger(__name__)


_RELATIVE_TIME = re.compile(
    r"^(?:(?:posted|published|updated)\s+)?(?:just now|today|yesterday|"
    r"(?:(?:\d+|an?|one)\s+)?(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago)$",
    re.IGNORECASE,
)


def news_fingerprint(texts: list[str]) -> str:
    """Hash stable article text, not the UI's changing relative-time labels."""
    stable = []
    for text in texts:
        for line in text.splitlines():
            normalized = " ".join(line.split())
            if normalized and not _RELATIVE_TIME.fullmatch(normalized):
                stable.append(normalized)
    if not stable:
        return ""
    return "v2:" + hashlib.sha256("\n".join(stable).encode("utf-8")).hexdigest()


class CompanionNewsMonitor:
    """管理新闻推送订阅会话和上次新闻指纹。

    state.json 结构::

        {"sessions": ["group_123", "group_456"],
         "last_fingerprint": "v2:abc123", "pending": {"v2:abc123": ["group_456"]}}
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.sessions: list[str] = []
        self.last_fingerprint: str = ""
        self.pending: dict[str, list[str]] = {}
        self.queued_at: dict[str, float] = {}
        self._load_state()
        self.prune_pending()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.sessions = [
                    str(x) for x in raw.get("sessions", []) if str(x).strip()
                ]
                self.last_fingerprint = str(raw.get("last_fingerprint", "") or "")
                pending = raw.get("pending", {})
                if isinstance(pending, dict):
                    self.pending = {
                        str(fp): [str(s) for s in sessions if str(s) in self.sessions]
                        for fp, sessions in pending.items()
                        if isinstance(sessions, list)
                    }
                timestamps = raw.get("queued_at", {})
                if not isinstance(timestamps, dict):
                    timestamps = {}
                now = time.time()
                for fp in self.pending:
                    try:
                        fallback = self.pending_image(fp).stat().st_mtime
                    except OSError:
                        fallback = now
                    self.queued_at[fp] = queued_timestamp(
                        timestamps.get(fp, fallback), now
                    )
        except Exception as e:
            logger.warning(f"[HD2] Companion news monitor load state failed: {e}")

    def _save_state(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "sessions": self.sessions,
                    "last_fingerprint": self.last_fingerprint,
                    "pending": self.pending,
                    "queued_at": self.queued_at,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def subscribe(self, unified_msg_origin: str) -> bool:
        value = str(unified_msg_origin).strip()
        if not value or value in self.sessions:
            return False
        self.sessions.append(value)
        self._save_state()
        return True

    def unsubscribe(self, unified_msg_origin: str) -> bool:
        value = str(unified_msg_origin).strip()
        if value not in self.sessions:
            return False
        self.sessions.remove(value)
        for sessions in self.pending.values():
            if value in sessions:
                sessions.remove(value)
        self._save_state()
        return True

    def check_fingerprint(self, fingerprint: str) -> bool:
        """Check if the fingerprint differs from the last recorded one.

        The first call establishes a baseline without pushing history.
        Legacy fingerprints are rebaselined when the algorithm changes.
        """
        fp = str(fingerprint or "").strip()
        if not fp:
            return False
        if not self.last_fingerprint or (
            fp.startswith("v2:") and not self.last_fingerprint.startswith("v2:")
        ):
            logger.info(
                "[HD2] Companion news fingerprint baseline initialized: %s",
                fp[:12],
            )
            self.last_fingerprint = fp
            self._save_state()
            return False
        return fp != self.last_fingerprint

    def pending_image(self, fingerprint: str) -> Path:
        key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return self.data_dir / f"pending_{key}.png"

    async def enqueue(self, fingerprint: str, image: Path) -> None:
        """Snapshot the image before acknowledging this observed update."""
        self.prune_pending()
        if fingerprint in self.pending:
            return
        if not self.sessions or image.stat().st_size > MAX_PENDING_IMAGE_BYTES:
            if self.sessions:
                logger.warning("[HD2] pending news image exceeds retention byte limit")
            self.commit_fingerprint(fingerprint)
            return
        output = self.pending_image(fingerprint)
        temporary = output.with_suffix(".tmp")
        await asyncio.to_thread(shutil.copyfile, image, temporary)
        temporary.replace(output)
        self.pending[fingerprint] = list(self.sessions)
        self.queued_at[fingerprint] = time.time()
        self.last_fingerprint = fingerprint
        self.prune_pending()
        self._save_state()

    def prune_pending(self) -> None:
        """Drop oldest/expired deliveries and reclaim their snapshots."""
        now = time.time()
        sizes: dict[str, int] = {}
        removed = False
        for fp in list(self.pending):
            self.queued_at[fp] = queued_timestamp(self.queued_at.get(fp), now)
            try:
                sizes[fp] = self.pending_image(fp).stat().st_size
            except OSError:
                sizes[fp] = 0
            if (
                not self.pending[fp]
                or sizes[fp] == 0
                or now - self.queued_at[fp] >= PENDING_TTL_SECONDS
            ):
                if self.pending[fp]:
                    logger.warning("[HD2] dropped pending news (expired/missing image): %s", fp)
                self.pending.pop(fp)
                self.queued_at.pop(fp, None)
                sizes.pop(fp)
                removed = True
        total = sum(sizes.values())
        for fp in sorted(self.pending, key=lambda key: self.queued_at[key]):
            if len(self.pending) <= MAX_PENDING_ENTRIES and total <= MAX_PENDING_IMAGE_BYTES:
                break
            logger.warning("[HD2] dropped oldest pending news (retention limit): %s", fp)
            self.pending.pop(fp)
            self.queued_at.pop(fp, None)
            total -= sizes[fp]
            removed = True
        active = {self.pending_image(fp) for fp in self.pending}
        for path in self.data_dir.glob("pending_*.png"):
            if path not in active:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("[HD2] pending news image cleanup failed: %s", exc)
        if removed or self.pending:
            self._save_state()

    def mark_delivered(self, fingerprint: str, session: str) -> None:
        remaining = self.pending.get(fingerprint, [])
        if session in remaining:
            remaining.remove(session)
            self._save_state()

    def finish_pending(self, fingerprint: str) -> None:
        if self.pending.get(fingerprint):
            return
        self.pending.pop(fingerprint, None)
        self.queued_at.pop(fingerprint, None)
        self._save_state()
        try:
            self.pending_image(fingerprint).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[HD2] pending news image cleanup failed: %s", exc)

    def commit_fingerprint(self, fingerprint: str) -> None:
        """Record an intentionally skipped update without queuing recipients."""
        fp = str(fingerprint or "").strip()
        if fp and fp != self.last_fingerprint:
            self.last_fingerprint = fp
            self._save_state()
            logger.info(
                "[HD2] Companion news fingerprint committed: %s", fp[:12]
            )
