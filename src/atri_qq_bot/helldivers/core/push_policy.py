"""Delivery retention, retry scheduling and guarded state migration."""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PushPolicy:
    max_entries: int = 100
    ttl_seconds: float = 7 * 86400
    max_image_bytes: int = 128 * 1024 * 1024
    retry_base_seconds: float = 60


def queued_timestamp(value: object, now: float) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return now
    return timestamp if math.isfinite(timestamp) and 0 < timestamp <= now else now


@dataclass(kw_only=True)
class DeliveryState:
    queued_at: float = field(default_factory=time.time)
    retries: dict[str, dict] = field(default_factory=dict)

    def load_delivery(self, raw: dict, fallback: float) -> None:
        self.queued_at = queued_timestamp(raw.get("queued_at", fallback), time.time())
        retries = raw.get("retries", {})
        if not isinstance(retries, dict):
            raise ValueError("invalid delivery retries")
        for origin, value in retries.items():
            if not isinstance(origin, str) or not isinstance(value, dict):
                raise ValueError("invalid delivery retry record")
            failures, retry_at = value.get("failures", 0), value.get("retry_at", 0)
            if not isinstance(failures, int) or not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at):
                raise ValueError("invalid delivery retry schedule")
            self.retries[origin] = {"failures": max(0, min(failures, 32)), "retry_at": min(retry_at, time.time() + 3600)}

    def due(self, origin: str) -> bool:
        return time.time() >= self.retries.get(origin, {}).get("retry_at", 0)

    def failed(self, origin: str, policy: PushPolicy) -> None:
        failures = min(32, self.retries.get(origin, {}).get("failures", 0) + 1)
        # First retry occurs on the next poll; repeated failures back off.
        delay = 0 if failures == 1 else min(3600, policy.retry_base_seconds * 2 ** min(10, failures - 2))
        self.retries[origin] = {"failures": failures, "retry_at": time.time() + delay}

    def delivery_json(self) -> dict:
        return {"queued_at": self.queued_at, "retries": self.retries}


def read_push_state(path: Path, field_name: str) -> tuple[dict, bool]:
    if not path.exists():
        return {}, False
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("state_version") not in {None, 1, 2, 3}:
        raise ValueError("unknown push state version")
    if not isinstance(raw.get("sessions", []), list):
        raise ValueError("invalid push subscriptions")
    if "pending" in raw and field_name in raw:
        raise ValueError("ambiguous push queue format")
    if "pending" in raw and not isinstance(raw["pending"], dict):
        raise ValueError("invalid legacy push queue")
    if field_name in raw and not isinstance(raw[field_name], list):
        raise ValueError("invalid push queue")
    if raw.get("state_version") == 3 and (field_name not in raw or "pending" in raw):
        raise ValueError("invalid version 3 push queue")
    return raw, raw.get("state_version") != 3


def backup_push_state(path: Path) -> Path:
    data = path.read_bytes()
    backup = path.with_name(f"state.before-v3.{hashlib.sha256(data).hexdigest()[:12]}.json")
    if not backup.exists():
        with backup.open("xb") as handle:
            handle.write(data)
    return backup
