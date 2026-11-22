"""Durable per-recipient news delivery with legacy queue migration."""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.persistence import finish_disk_call, write_json_atomic
from ..core.push import has_group_subscription, normalize_sessions
from ..core.push_policy import DeliveryState, PushPolicy, backup_push_state, read_push_state

from .._logging import logger


@dataclass
class PendingCompanionNews(DeliveryState):
    fingerprint: str
    sessions: list[str]


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
    def __init__(self, data_dir: Path, *, policy: PushPolicy | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.outbox_dir = self.data_dir / "outbox"
        self.policy = policy or PushPolicy()
        self.sessions: list[str] = []
        self.last_fingerprint = ""
        self.pending_news: dict[str, PendingCompanionNews] = {}
        self._state_error = ""
        self._delivery_lock = asyncio.Lock()
        self._load_state()

    def _ensure_writable(self) -> None:
        if self._state_error:
            raise RuntimeError(f"Companion 推送状态受保护，未覆盖原文件: {self._state_error}")

    def _load_state(self) -> None:
        try:
            raw, migrate = read_push_state(self.state_path, "pending_news")
            self.sessions = normalize_sessions(raw.get("sessions"))
            self.last_fingerprint = str(raw.get("last_fingerprint") or "")
            pending = raw.get("pending_news", [])
            if "pending" in raw:
                times = raw.get("queued_at", {})
                if not isinstance(times, dict):
                    raise ValueError("invalid legacy queue timestamps")
                pending = [{"fingerprint": fp, "sessions": sessions,
                            "queued_at": times.get(fp)} for fp, sessions in raw["pending"].items()]
            copies = []
            for row in pending:
                if not isinstance(row, dict) or not isinstance(row.get("sessions"), list):
                    raise ValueError("invalid pending news record")
                fp = row.get("fingerprint")
                if not isinstance(fp, str) or not fp:
                    raise ValueError("missing news fingerprint")
                targets = [value for value in normalize_sessions(row["sessions"]) if value in self.sessions]
                if not targets:
                    continue
                output = self.pending_image_path(fp)
                source = self.data_dir / f"pending_{hashlib.sha256(fp.encode()).hexdigest()}.png"
                if "pending" in raw:
                    if not source.is_file() or source.stat().st_size == 0:
                        raise ValueError(f"legacy news screenshot missing: {source.name}")
                    copies.append((source, output))
                else:
                    source = output
                item = PendingCompanionNews(fp, targets)
                fallback = source.stat().st_mtime if source.is_file() else time.time()
                item.load_delivery({**row, "queued_at": row.get("queued_at") or fallback}, fallback)
                if fp in self.pending_news:
                    raise ValueError("duplicate news fingerprint")
                self.pending_news[fp] = item
            if migrate:
                backup_push_state(self.state_path)
                for source, output in copies:
                    self._copy_image(source, output)
                self._save_state()
            self.prune_pending()
        except Exception as exc:
            self._state_error = str(exc)
            self.sessions = []
            self.pending_news = {}
            logger.error("[HD2] Companion state protected; push disabled: %s", exc)

    def _save_state(self) -> None:
        self._ensure_writable()
        write_json_atomic(self.state_path, {
            "state_version": 3, "sessions": self.sessions,
            "last_fingerprint": self.last_fingerprint,
            "pending_news": [{"fingerprint": item.fingerprint, "sessions": item.sessions,
                              **item.delivery_json()} for item in self.pending_news.values()],
        })

    def subscribe(self, unified_msg_origin: str) -> bool:
        self._ensure_writable()
        value = str(unified_msg_origin).strip()
        if not value or has_group_subscription(self.sessions, value):
            return False
        self.sessions.append(value)
        try:
            self._save_state()
        except OSError:
            self.sessions.remove(value)
            raise
        return True

    def unsubscribe(self, unified_msg_origin: str) -> bool:
        self._ensure_writable()
        value = str(unified_msg_origin).strip()
        if value not in self.sessions:
            return False
        previous_sessions = list(self.sessions)
        previous_pending = dict(self.pending_news)
        previous_delivery = {
            key: (list(item.sessions), dict(item.retries))
            for key, item in previous_pending.items()
        }
        self.sessions.remove(value)
        completed = []
        for fp, item in list(self.pending_news.items()):
            item.sessions = [origin for origin in item.sessions if origin != value]
            item.retries.pop(value, None)
            if not item.sessions:
                del self.pending_news[fp]
                completed.append(fp)
        try:
            self._save_state()
        except OSError:
            self.sessions[:] = previous_sessions
            self.pending_news = previous_pending
            for key, item in previous_pending.items():
                item.sessions, item.retries = previous_delivery[key]
            raise
        for fp in completed:
            self._discard_image(fp)
        return True

    def pending_image_path(self, fingerprint: str) -> Path:
        return self.outbox_dir / f"{hashlib.sha256(fingerprint.encode()).hexdigest()}.png"

    def _discard_image(self, fingerprint: str) -> None:
        try:
            self.pending_image_path(fingerprint).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[HD2] Companion pending image cleanup failed: %s", exc)

    @staticmethod
    def _copy_image(source: Path, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        try:
            shutil.copyfile(source, temporary)
            if temporary.stat().st_size == 0:
                raise OSError("新闻截图为空")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_image(content: bytes, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        try:
            if not content:
                raise OSError("新闻截图为空")
            temporary.write_bytes(content)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    def prune_pending(self) -> None:
        self._ensure_writable()
        removed = {}
        sizes = {}
        now = time.time()
        for fp, item in self.pending_news.items():
            path = self.pending_image_path(fp)
            if not item.sessions or now - item.queued_at >= self.policy.ttl_seconds:
                removed[fp] = "expired/no recipients"
            elif not path.is_file() or path.stat().st_size == 0:
                removed[fp] = "missing screenshot"
            else:
                sizes[fp] = path.stat().st_size
        total = sum(sizes.values())
        count = len(sizes)
        for fp in sorted(sizes, key=lambda key: self.pending_news[key].queued_at):
            if count <= self.policy.max_entries and total <= self.policy.max_image_bytes:
                break
            removed[fp] = "retention count/byte limit"
            count -= 1
            total -= sizes[fp]
        if removed:
            previous = dict(self.pending_news)
            for fp in removed:
                self.pending_news.pop(fp, None)
            try:
                self._save_state()
            except OSError:
                self.pending_news = previous
                raise
            for fp, reason in removed.items():
                logger.warning("[HD2] Companion pending discarded: %s reason=%s", fp, reason)
                self._discard_image(fp)
        # Only current outbox files: retain legacy screenshots alongside the migration backup.
        active = {self.pending_image_path(fp).name for fp in self.pending_news}
        for path in self.outbox_dir.glob("*.png"):
            if path.name not in active:
                path.unlink(missing_ok=True)

    async def queue_news(self, fingerprint: str, image: Path | bytes) -> bool:
        async with self._delivery_lock:
            self._ensure_writable()
            self.prune_pending()
            if not self.check_fingerprint(fingerprint):
                return False
            if fingerprint in self.pending_news:
                self.commit_fingerprint(fingerprint)
                return False
            if not self.sessions:
                self.commit_fingerprint(fingerprint)
                return False
            size = len(image) if isinstance(image, bytes) else image.stat().st_size
            if size > self.policy.max_image_bytes:
                logger.warning("[HD2] Companion snapshot exceeds retention byte limit; skipped: %s", fingerprint)
                self.commit_fingerprint(fingerprint)
                return False
            targets = list(self.sessions)
            writer = self._write_image if isinstance(image, bytes) else self._copy_image
            await finish_disk_call(writer, image, self.pending_image_path(fingerprint))
            targets = [origin for origin in targets if origin in self.sessions]
            if targets:
                self.pending_news[fingerprint] = PendingCompanionNews(fingerprint, targets)
            previous_fingerprint = self.last_fingerprint
            self.last_fingerprint = fingerprint
            try:
                self._save_state()
            except OSError:
                self.pending_news.pop(fingerprint, None)
                self.last_fingerprint = previous_fingerprint
                self._discard_image(fingerprint)
                raise
            self.prune_pending()
            return fingerprint in self.pending_news

    async def retry_pending(self, send_news: Callable[[Path, list[str]], Awaitable[int]],
                            fingerprints: list[str] | None = None) -> int:
        async with self._delivery_lock:
            self._ensure_writable()
            self.prune_pending()
            delivered = 0
            for fp in list(self.pending_news) if fingerprints is None else fingerprints:
                item = self.pending_news.get(fp)
                if item is None:
                    continue
                for origin in list(item.sessions):
                    if origin not in self.sessions or origin not in item.sessions or not item.due(origin):
                        continue
                    try:
                        count = await send_news(self.pending_image_path(fp), [origin])
                    except Exception as exc:
                        logger.warning("[HD2] Companion pending push failed: session=%s error=%s", origin, exc)
                        count = 0
                    if not count:
                        item.failed(origin, self.policy)
                        self._save_state()
                        continue
                    delivered += 1
                    if origin in item.sessions:
                        item.sessions.remove(origin)
                    item.retries.pop(origin, None)
                    if not item.sessions:
                        self.pending_news.pop(fp, None)
                    self._save_state()
                    if not item.sessions:
                        self._discard_image(fp)
            return delivered

    def check_fingerprint(self, fingerprint: str) -> bool:
        self._ensure_writable()
        fp = str(fingerprint or "").strip()
        if not fp:
            return False
        # Migrate only the old SHA1 format, leaving existing queued recipients intact.
        if not self.last_fingerprint or (fp.startswith("v2:") and re.fullmatch(r"[0-9a-f]{40}", self.last_fingerprint)):
            self.commit_fingerprint(fp)
            return False
        return fp != self.last_fingerprint

    def commit_fingerprint(self, fingerprint: str) -> None:
        self._ensure_writable()
        fp = str(fingerprint or "").strip()
        if fp and fp != self.last_fingerprint:
            self.last_fingerprint = fp
            self._save_state()
