"""Persist and attach short-term Helldivers campaign trends.

The module intentionally accepts normalized mapping rows instead of depending on the
plugin's API models.  The most useful public entry point is
:func:`attach_campaign_trends`; :class:`CampaignTrendStore` is available when a caller
wants to keep one configured store instance.

Recognised normalized fields include:

* planet identity: ``planet_index``, ``planet_id``, ``index``, ``planet_name`` or
  ``name`` (nested ``planet`` mappings are also accepted)
* activity identity: ``event_id``/``event``, then ``campaign_id``/``campaign``
* progress: ``progress``, ``liberation``, ``liberation_pct`` or ``progress_pct``
* deadline: ``deadline``, ``ends``, ``end_time`` or ``endTime``
* defense marker: ``is_defense`` or a ``mode``/``campaign_type`` containing
  ``defense``

Attached rows receive ``recent_rate_pph`` (net percentage-points/hour),
``recent_rate_window_minutes`` and ``estimated_result`` plus machine-readable
estimate fields.  Invalid rows are copied through unchanged and are never recorded.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_PROGRESS_KEYS = (
    "progress",
    "liberation",
    "liberation_pct",
    "progress_pct",
    "percentage",
)


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _clean_token(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    text = str(value).strip()
    return text.casefold() if text else ""


def _nested_mapping(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = row.get(key)
    return value if isinstance(value, Mapping) else {}


def _first_token(*values: Any) -> str:
    for value in values:
        token = _clean_token(value)
        if token:
            return token
    return ""


def _planet_token(row: Mapping[str, Any]) -> str:
    planet = _nested_mapping(row, "planet")
    index = _first_token(
        row.get("planet_index"),
        row.get("planet_id"),
        row.get("index"),
        planet.get("index"),
        planet.get("planetIndex"),
        planet.get("id"),
    )
    if index:
        return f"id:{index}"
    name = _first_token(
        row.get("planet_name"),
        row.get("name"),
        planet.get("name"),
        planet.get("planetName"),
    )
    return f"name:{name}" if name else ""


def _activity_token(row: Mapping[str, Any]) -> str:
    raw_event = row.get("event")
    raw_campaign = row.get("campaign")
    event = _nested_mapping(row, "event")
    campaign = _nested_mapping(row, "campaign")
    event_id = _first_token(
        row.get("event_id"),
        row.get("eventId"),
        raw_event if not isinstance(raw_event, Mapping) else None,
        event.get("id"),
        event.get("eventId"),
        event.get("event_id"),
    )
    if event_id:
        return f"event:{event_id}"

    campaign_id = _first_token(
        row.get("campaign_id"),
        row.get("campaignId"),
        raw_campaign if not isinstance(raw_campaign, Mapping) else None,
        campaign.get("id"),
        campaign.get("campaignId"),
        campaign.get("campaign_id"),
    )
    if campaign_id:
        return f"campaign:{campaign_id}"

    # A changing deadline is a useful event discriminator when an API omits IDs.
    deadline = _first_token(
        row.get("deadline"),
        row.get("ends"),
        row.get("end_time"),
        row.get("endTime"),
        event.get("endTime"),
        event.get("expireTime"),
    )
    if event or _is_defense(row):
        event_type = _first_token(
            row.get("event_type"), event.get("eventType"), event.get("type")
        )
        return f"event:{event_type or 'unknown'}:{deadline or 'no-deadline'}"

    started = _first_token(
        row.get("campaign_start"),
        row.get("start_time"),
        row.get("startTime"),
        campaign.get("startTime"),
    )
    campaign_type = _first_token(
        row.get("campaign_type"), row.get("mode"), campaign.get("type")
    )
    faction = _first_token(row.get("faction"), row.get("owner"))
    # This fallback remains stable for an ordinary campaign while still changing
    # when integration can supply a start marker, campaign type, or faction.
    return f"campaign:{campaign_type or 'ordinary'}:{faction or 'unknown'}:{started}"


def campaign_identity(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return ``(planet_key, full_identity)`` or ``None`` for an unusable row."""

    if not isinstance(row, Mapping):
        return None
    planet = _planet_token(row)
    if not planet:
        return None
    activity = _activity_token(row)
    return planet, f"{planet}|{activity}"


def campaign_progress(row: Mapping[str, Any]) -> float | None:
    """Extract a safe 0..100 campaign progress percentage from a normalized row."""

    for key in _PROGRESS_KEYS:
        value = _finite_float(row.get(key))
        if value is not None:
            return max(0.0, min(100.0, value))

    event = _nested_mapping(row, "event")
    planet = _nested_mapping(row, "planet")
    source = event if event else planet
    health = _finite_float(source.get("health"))
    maximum = _finite_float(source.get("maxHealth") or source.get("max_health"))
    if health is None or maximum is None or maximum <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - health / maximum) * 100.0))


def _is_successful_row(row: Mapping[str, Any]) -> bool:
    for key in ("fetch_success", "request_success", "valid", "success"):
        if key not in row:
            continue
        marker = row.get(key)
        if marker is False or marker == 0:
            return False
        if isinstance(marker, str) and marker.strip().casefold() in {
            "false",
            "failed",
            "error",
            "invalid",
            "no",
        }:
            return False
    return campaign_identity(row) is not None and campaign_progress(row) is not None


def _is_defense(row: Mapping[str, Any]) -> bool:
    marker = row.get("is_defense")
    if isinstance(marker, bool):
        return marker
    if isinstance(marker, (int, float)):
        return bool(marker)
    if isinstance(marker, str) and marker.strip().casefold() in {"true", "yes", "1"}:
        return True
    for key in ("mode", "kind", "campaign_type", "event_type"):
        value = _clean_token(row.get(key))
        if "defen" in value or "防御" in value:
            return True
    owner = _clean_token(
        row.get("owner") or _nested_mapping(row, "planet").get("owner")
    )
    return bool(_nested_mapping(row, "event")) and owner in {
        "humans",
        "human",
        "super earth",
        "人类",
        "超级地球",
    }


def _parse_timestamp(value: Any) -> float | None:
    number = _finite_float(value)
    if number is not None:
        # Be permissive with millisecond Unix timestamps.
        if number > 100_000_000_000:
            number /= 1000.0
        return number if number > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    timestamp = parsed.timestamp()
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else None


def campaign_deadline(row: Mapping[str, Any]) -> float | None:
    """Extract a Unix deadline from common normalized or nested event fields."""

    event = _nested_mapping(row, "event")
    for value in (
        row.get("deadline"),
        row.get("ends"),
        row.get("end_time"),
        row.get("endTime"),
        row.get("expires_at"),
        event.get("endTime"),
        event.get("expireTime"),
        event.get("deadline"),
    ):
        timestamp = _parse_timestamp(value)
        if timestamp is not None:
            return timestamp
    return None


def _duration_text(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分钟"
    return f"{max(1, minutes)}分钟"


def estimate_campaign_result(
    row: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Estimate an ordinary liberation ETA or a deadline-defense outcome.

    The returned mapping is safe to merge into a normalized row.  A non-positive or
    missing rate never causes division by zero and produces a stalled/unknown result.
    """

    current_time = _parse_timestamp(now) if now is not None else time.time()
    if current_time is None:
        current_time = time.time()
    progress = campaign_progress(row)
    rate = _finite_float(row.get("recent_rate_pph"))
    defense = _is_defense(row)
    deadline = campaign_deadline(row)

    result: dict[str, Any] = {
        "estimated_result": "未知",
        "estimated_result_status": "unknown",
        "estimated_completion_at": None,
        "estimated_progress_at_deadline": None,
    }
    if progress is None:
        return result

    if defense and deadline is None:
        result.update(
            estimated_result="防御截止时间未知",
            estimated_result_status="unknown",
        )
        return result

    if defense and deadline is not None:
        seconds_left = deadline - current_time
        if progress >= 100.0:
            result.update(
                estimated_result="防御成功",
                estimated_result_status="success",
                estimated_progress_at_deadline=100.0,
            )
            return result
        if seconds_left <= 0:
            result.update(
                estimated_result="防御失败 · 已超过截止时间",
                estimated_result_status="failure",
                estimated_progress_at_deadline=progress,
            )
            return result
        projected = progress
        if rate is not None:
            projected = max(0.0, min(100.0, progress + rate * seconds_left / 3600.0))
        result["estimated_progress_at_deadline"] = projected
        if rate is not None and rate > 0:
            completion = current_time + (100.0 - progress) / rate * 3600.0
            result["estimated_completion_at"] = completion
            if completion <= deadline:
                result.update(
                    estimated_result=f"防御可守住 · {_duration_text(completion - current_time)}",
                    estimated_result_status="success",
                )
            else:
                result.update(
                    estimated_result=f"防御将失败 · 截止时预计 {projected:.1f}%",
                    estimated_result_status="failure",
                )
        elif rate is not None:
            result.update(
                estimated_result=f"防御将失败 · 截止时预计 {projected:.1f}%",
                estimated_result_status="failure",
            )
        else:
            result.update(
                estimated_result=f"等待防御趋势 · 还剩 {_duration_text(seconds_left)}",
                estimated_result_status="unknown",
            )
        return result

    if progress >= 100.0:
        result.update(
            estimated_result="已解放",
            estimated_result_status="success",
            estimated_completion_at=current_time,
        )
    elif rate is None:
        result.update(
            estimated_result="等待趋势", estimated_result_status="unknown"
        )
    elif rate > 0:
        completion = current_time + (100.0 - progress) / rate * 3600.0
        result.update(
            estimated_result=f"预计 {_duration_text(completion - current_time)} 后解放",
            estimated_result_status="advancing",
            estimated_completion_at=completion,
        )
    elif rate < 0:
        result.update(
            estimated_result="正在失守", estimated_result_status="losing"
        )
    else:
        result.update(estimated_result="进度停滞", estimated_result_status="stalled")
    return result


def estimate_liberation_result(
    progress: Any,
    recent_rate_pph: Any,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Estimate the result of an ordinary, non-deadline liberation campaign."""

    return estimate_campaign_result(
        {"progress": progress, "recent_rate_pph": recent_rate_pph},
        now=now,
    )


def estimate_defense_result(
    progress: Any,
    recent_rate_pph: Any,
    deadline: Any,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Estimate whether a defense reaches 100% before its deadline."""

    return estimate_campaign_result(
        {
            "progress": progress,
            "recent_rate_pph": recent_rate_pph,
            "deadline": deadline,
            "is_defense": True,
        },
        now=now,
    )


class CampaignTrendStore:
    """Bounded, atomic JSON store for short campaign progress histories."""

    def __init__(
        self,
        path: str | Path,
        *,
        window_minutes: float = 20,
        prune_hours: float = 2,
        max_samples_per_planet: int = 240,
        max_planets: int = 256,
        min_sample_age_seconds: float = 60,
    ) -> None:
        self.path = Path(path)
        self.window_minutes = max(10.0, min(30.0, float(window_minutes)))
        self.prune_hours = max(1.0, min(3.0, float(prune_hours)))
        self.max_samples_per_planet = max(2, int(max_samples_per_planet))
        self.max_planets = max(1, int(max_planets))
        self.min_sample_age_seconds = max(1.0, float(min_sample_age_seconds))

    def _empty(self) -> dict[str, Any]:
        return {"version": _SCHEMA_VERSION, "planets": {}}

    def load(self) -> dict[str, Any]:
        """Load history; malformed, missing, or incompatible JSON becomes empty."""

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return self._empty()
        if not isinstance(raw, dict) or not isinstance(raw.get("planets"), dict):
            return self._empty()

        clean = self._empty()
        planets: dict[str, Any] = clean["planets"]
        for planet_key, entry in raw["planets"].items():
            if not isinstance(planet_key, str) or not isinstance(entry, dict):
                continue
            identity = entry.get("identity")
            samples = entry.get("samples")
            if not isinstance(identity, str) or not isinstance(samples, list):
                continue
            valid_samples: list[list[float]] = []
            for sample in samples:
                if not isinstance(sample, (list, tuple)) or len(sample) != 2:
                    continue
                timestamp = _finite_float(sample[0])
                progress = _finite_float(sample[1])
                if timestamp is None or timestamp <= 0 or progress is None:
                    continue
                valid_samples.append([timestamp, max(0.0, min(100.0, progress))])
            valid_samples.sort(key=lambda item: item[0])
            if valid_samples:
                planets[planet_key] = {
                    "identity": identity,
                    "samples": valid_samples[-self.max_samples_per_planet :],
                }
        return clean

    def _prune(self, data: dict[str, Any], now: float) -> None:
        cutoff = now - self.prune_hours * 3600.0
        planets = data.get("planets")
        if not isinstance(planets, dict):
            data["planets"] = {}
            return
        for key in list(planets):
            entry = planets.get(key)
            if not isinstance(entry, dict):
                planets.pop(key, None)
                continue
            samples = entry.get("samples")
            if not isinstance(samples, list):
                planets.pop(key, None)
                continue
            kept = [
                sample
                for sample in samples
                if isinstance(sample, list)
                and len(sample) == 2
                and (_finite_float(sample[0]) or 0) >= cutoff
            ][-self.max_samples_per_planet :]
            if kept:
                entry["samples"] = kept
            else:
                planets.pop(key, None)

        if len(planets) > self.max_planets:
            ordered = sorted(
                planets,
                key=lambda key: (
                    _finite_float(planets[key]["samples"][-1][0]) or 0,
                    key,
                ),
                reverse=True,
            )
            keep = set(ordered[: self.max_planets])
            for key in list(planets):
                if key not in keep:
                    planets.pop(key, None)

    def save(self, data: Mapping[str, Any]) -> None:
        """Atomically save JSON via a same-directory temporary file and ``replace``."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            temp_name = None
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def record_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        timestamp: float | None = None,
    ) -> int:
        """Record valid campaign rows at the actual call time and return the count.

        ``timestamp`` exists for deterministic tests/backfills.  Normal callers should
        omit it so each fetch is stamped with ``time.time()`` rather than an API or
        cache timestamp.
        """

        now = _parse_timestamp(timestamp) if timestamp is not None else time.time()
        if now is None:
            now = time.time()
        data = self.load()
        planets = data["planets"]
        recorded = 0
        for row in rows:
            if not isinstance(row, Mapping) or not _is_successful_row(row):
                continue
            identity_parts = campaign_identity(row)
            progress = campaign_progress(row)
            if identity_parts is None or progress is None:
                continue
            planet_key, identity = identity_parts
            entry = planets.get(planet_key)
            if not isinstance(entry, dict) or entry.get("identity") != identity:
                # A new event/campaign on the same planet must never inherit its rate.
                entry = {"identity": identity, "samples": []}
                planets[planet_key] = entry
            samples = entry["samples"]
            if samples and abs(float(samples[-1][0]) - now) < 0.001:
                samples[-1] = [now, progress]
            else:
                samples.append([now, progress])
            samples.sort(key=lambda sample: sample[0])
            entry["samples"] = samples[-self.max_samples_per_planet :]
            recorded += 1
        self._prune(data, now)
        self.save(data)
        return recorded

    def recent_rate(
        self,
        row: Mapping[str, Any],
        *,
        now: float | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> tuple[float | None, float | None]:
        """Return ``(net_pph, actual_window_minutes)`` for one row."""

        current_time = _parse_timestamp(now) if now is not None else time.time()
        if current_time is None:
            current_time = time.time()
        identity_parts = campaign_identity(row)
        progress = campaign_progress(row)
        if identity_parts is None or progress is None:
            return None, None
        planet_key, identity = identity_parts
        history = data if isinstance(data, Mapping) else self.load()
        planets = history.get("planets")
        if not isinstance(planets, Mapping):
            return None, None
        entry = planets.get(planet_key)
        if not isinstance(entry, Mapping) or entry.get("identity") != identity:
            return None, None
        samples = entry.get("samples")
        if not isinstance(samples, list):
            return None, None

        candidates: list[tuple[float, float]] = []
        for sample in samples:
            if not isinstance(sample, (list, tuple)) or len(sample) != 2:
                continue
            sample_time = _finite_float(sample[0])
            sample_progress = _finite_float(sample[1])
            if (
                sample_time is None
                or sample_progress is None
                or sample_time >= current_time - 0.001
            ):
                continue
            age = current_time - sample_time
            max_window_age = min(
                self.prune_hours * 3600.0,
                self.window_minutes * 60.0 * 1.5,
            )
            if age <= max_window_age:
                candidates.append((sample_time, sample_progress))
        if not candidates:
            return None, None

        candidates.sort()
        target = current_time - self.window_minutes * 60.0
        baseline = candidates[0]
        for sample in candidates:
            if abs(sample[0] - target) < abs(baseline[0] - target):
                baseline = sample
        elapsed = current_time - baseline[0]
        if elapsed < self.min_sample_age_seconds:
            return None, None
        rate = (progress - baseline[1]) * 3600.0 / elapsed
        if not math.isfinite(rate):
            return None, None
        return rate, elapsed / 60.0

    def attach(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        timestamp: float | None = None,
        record: bool = True,
    ) -> list[dict[str, Any]]:
        """Copy rows and attach recent rate and estimated-result fields.

        By default the current valid rows are recorded first.  Set ``record=False``
        for a read-only projection from existing history.
        """

        now = _parse_timestamp(timestamp) if timestamp is not None else time.time()
        if now is None:
            now = time.time()
        materialized = [dict(row) for row in rows if isinstance(row, Mapping)]
        if record:
            self.record_rows(materialized, timestamp=now)
        data = self.load()
        attached: list[dict[str, Any]] = []
        for row in materialized:
            rate, window = self.recent_rate(row, now=now, data=data)
            row["recent_rate_pph"] = rate
            row["recent_rate"] = rate
            row["net_rate_pph"] = rate
            row["recent_rate_window_minutes"] = window
            row.update(estimate_campaign_result(row, now=now))
            attached.append(row)
        return attached


def record_campaign_rows(
    history_path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    timestamp: float | None = None,
    window_minutes: float = 20,
    prune_hours: float = 2,
) -> int:
    """Convenience wrapper for :meth:`CampaignTrendStore.record_rows`."""

    return CampaignTrendStore(
        history_path,
        window_minutes=window_minutes,
        prune_hours=prune_hours,
    ).record_rows(rows, timestamp=timestamp)


def attach_campaign_trends(
    rows: Iterable[Mapping[str, Any]],
    history_path: str | Path,
    *,
    timestamp: float | None = None,
    window_minutes: float = 20,
    prune_hours: float = 2,
    record: bool = True,
) -> list[dict[str, Any]]:
    """Attach recent net rate and campaign estimates to normalized rows."""

    store = CampaignTrendStore(
        history_path,
        window_minutes=window_minutes,
        prune_hours=prune_hours,
    )
    return store.attach(rows, timestamp=timestamp, record=record)


def attach_recent_net_rates(
    rows: Iterable[Mapping[str, Any]],
    history_path: str | Path,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Alias emphasizing the percentage-points/hour enrichment operation."""

    return attach_campaign_trends(rows, history_path, **kwargs)


__all__ = [
    "CampaignTrendStore",
    "attach_campaign_trends",
    "attach_recent_net_rates",
    "campaign_deadline",
    "campaign_identity",
    "campaign_progress",
    "estimate_campaign_result",
    "estimate_defense_result",
    "estimate_liberation_result",
    "record_campaign_rows",
]
