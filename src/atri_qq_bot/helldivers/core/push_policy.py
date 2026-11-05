"""Bounded retention for durable news delivery queues."""

import math

MAX_PENDING_ENTRIES = 100
PENDING_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_PENDING_IMAGE_BYTES = 128 * 1024 * 1024


def queued_timestamp(value: object, now: float) -> float:
    """Give legacy/invalid timestamps a finite migration window."""
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return now
    return timestamp if math.isfinite(timestamp) and 0 < timestamp <= now else now
