"""Regression tests for the HD2 v1.10.3 -> v2.1.0 sync.

The repo has no pytest-asyncio, so every async case is driven through
``asyncio.run`` inside an ordinary synchronous test.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from atri_qq_bot.helldivers.core.arsenal_merge import (
    equipment_key,
    merge_equipment,
)
from atri_qq_bot.helldivers.core.persistence import write_json_atomic
from atri_qq_bot.helldivers.core.planet_effects import resolve_planet_effects
from atri_qq_bot.helldivers.core.push import (
    QQGroupSubscriptionChecker,
    group_session,
    has_group_subscription,
    normalize_sessions,
    subscription_targets,
)
from atri_qq_bot.helldivers.core.push_policy import DeliveryState, PushPolicy


# --------------------------------------------------------------------------
# Planet effects
# --------------------------------------------------------------------------

def test_planet_effect_catalog_is_bundled():
    # A catalog id resolves to a non-empty name; an unknown id keeps its number.
    known = resolve_planet_effects([{"galacticEffectId": 1430}])
    assert len(known) == 1
    assert known[0]["id"] == 1430
    assert known[0]["name"].strip()
    assert known[0]["name_zh"] == "腐蚀变种"

    unknown = resolve_planet_effects([{"galacticEffectId": 999999}])
    assert unknown[0]["name"] == "未收录效果（999999）"


def test_planet_effects_deduplicate_enemies_variant():
    rows = [
        {"galacticEffectId": 1430},
        {"galacticEffectId": 1430},
    ]
    assert len(resolve_planet_effects(rows)) == 1


def test_planet_effects_skip_invalid_ids():
    assert resolve_planet_effects([{"galacticEffectId": -1}, {}]) == []


# --------------------------------------------------------------------------
# push session helpers
# --------------------------------------------------------------------------

def test_normalize_sessions():
    assert normalize_sessions(
        ["group:1", " group:1 ", "", "private:2", None]
    ) == ["group:1", "private:2"]
    assert normalize_sessions("group:1") == []
    assert normalize_sessions(None) == []


def test_group_session():
    assert group_session("group:123") == ("group", "123")
    assert group_session("private:123") is None
    assert group_session("group:abc") is None
    assert group_session("group:0") is None
    assert group_session("") is None


def test_has_group_subscription():
    assert has_group_subscription(["group:123"], "group:123")
    assert not has_group_subscription(["group:123"], "group:456")
    assert not has_group_subscription(["group:123"], "private:1")


def test_subscription_targets():
    sessions = ["group:123", "group:456"]
    assert subscription_targets(sessions, "group:123", "group:999") == ["group:123"]
    assert subscription_targets(sessions, "123", "group:999") == ["group:123"]
    assert subscription_targets(sessions, "789", "group:999") == []
    with pytest.raises(ValueError):
        subscription_targets(sessions, "group:abc", "group:999")
    with pytest.raises(ValueError):
        subscription_targets(sessions, "123", "private:1")


# --------------------------------------------------------------------------
# QQ group subscription checker
# --------------------------------------------------------------------------

def test_checker_removes_left_groups():
    async def scenario():
        async def provider():
            return ["123"]  # group 456 is no longer joined

        checker = QQGroupSubscriptionChecker(provider)
        removed = await checker.removed_sessions(
            ["group:123", "group:456"]
        )
        return removed

    assert asyncio.run(scenario()) == {"group:456"}


def test_checker_retains_subscriptions_on_provider_failure():
    async def scenario():
        async def boom():
            raise RuntimeError("napcat offline")

        checker = QQGroupSubscriptionChecker(boom)
        return await checker.removed_sessions(["group:123"])

    assert asyncio.run(scenario()) == set()


def test_checker_retains_subscriptions_on_non_list():
    async def scenario():
        async def bad():
            return None

        checker = QQGroupSubscriptionChecker(bad)
        return await checker.removed_sessions(["group:123"])

    assert asyncio.run(scenario()) == set()


def test_checker_without_provider_keeps_all():
    checker = QQGroupSubscriptionChecker(None)
    assert asyncio.run(checker.removed_sessions(["group:123"])) == set()


# --------------------------------------------------------------------------
# Delivery retry backoff
# --------------------------------------------------------------------------

def test_delivery_state_backoff_schedule():
    policy = PushPolicy(retry_base_seconds=60)
    state = DeliveryState()
    now = time.time()

    state.failed("group:1", policy)
    assert state.retries["group:1"]["failures"] == 1
    # First failure: next poll, no added delay.
    assert state.retries["group:1"]["retry_at"] <= now + 1

    state.failed("group:1", policy)
    second = state.retries["group:1"]["retry_at"]
    assert second > now  # repeated failures back off into the future
    assert state.due("group:1") is False


# --------------------------------------------------------------------------
# Atomic JSON writes
# --------------------------------------------------------------------------

def test_write_json_atomic(tmp_path):
    target = tmp_path / "nested" / "state.json"
    payload = {"a": 1, "b": "中文"}
    write_json_atomic(target, payload)
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    # No temporary files left behind.
    assert not list(tmp_path.rglob("*.tmp"))


# --------------------------------------------------------------------------
# Arsenal equipment merge
# --------------------------------------------------------------------------

def test_equipment_key_strips_dionatha_prefix():
    assert equipment_key(
        {"productKind": "weapons", "slug": "dionatha:railgun"}
    ) == ("weapons", "railgun")


def test_merge_equipment_merges_identical_and_appends_new():
    primary = [
        {"productKind": "weapons", "slug": "railgun", "description": "old"}
    ]
    supplement = [
        {
            "productKind": "weapons",
            "slug": "railgun",
            "description": "new",
            "_data_version": "2026-11-01",
        },
        {
            "productKind": "weapons",
            "slug": "solo-item",
            "_data_version": "2026-11-01",
        },
    ]
    merged = merge_equipment(primary, supplement, "2026-10-01")
    # One merged record + one unmatched supplement record.
    assert len(merged) == 2
    top = merged[0]
    assert top["_source"] == "merged"
    assert top["_source_versions"]["dionatha"] == "2026-11-01"
    assert merged[1]["slug"] == "solo-item"


def test_merge_equipment_ambiguous_identity_stays_separate():
    primary = [{"productKind": "weapons", "slug": "dup"}]
    supplement = [
        {"productKind": "weapons", "slug": "dup"},
        {"productKind": "weapons", "slug": "dup"},
    ]
    merged = merge_equipment(primary, supplement)
    # Two supplement candidates for one key -> no confident merge, all kept.
    assert len(merged) == 3
    assert "_source" not in merged[0]
