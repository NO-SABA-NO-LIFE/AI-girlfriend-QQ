"""Persistent push subscription helpers (OneBot-native port).

The AstrBot plugin parsed Unified Message Object strings via ``MessageSesion`` and
reconciled groups through AstrBot's ``context``. Atri stores sessions as plain
``group:<id>`` / ``private:<id>`` keys, so this module keeps the same public
helper surface (``normalize_sessions`` / ``group_session`` /
``has_group_subscription`` / ``subscription_targets`` /
``QQGroupSubscriptionChecker``) but resolves them against those simple keys.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .._logging import logger

# A group-list provider returns the currently joined group id strings. It must
# raise (or return None) when the QQ online status cannot be confirmed, so the
# checker retains subscriptions instead of pruning on uncertain data.
GroupListProvider = Callable[[], Awaitable[list[str]]]


def normalize_sessions(value: Any) -> list[str]:
    """Deduplicate a list of session strings, dropping blanks."""
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(x.strip() for x in value if isinstance(x, str) and x.strip())
    )


def group_session(origin: str) -> tuple[str, str] | None:
    """Resolve an Atri group key ``group:<id>`` into (kind, group id).

    Returns ``None`` for private sessions or anything that is not a well-formed
    group key. Mirrors the AstrBot version's tuple contract.
    """
    kind, separator, group_id = str(origin or "").partition(":")
    if not separator or kind != "group":
        return None
    group_id = group_id.strip()
    if not group_id.isascii() or not group_id.isdigit() or int(group_id) <= 0:
        return None
    return kind, group_id


def has_group_subscription(sessions: list[str], origin: str) -> bool:
    """Whether a group session is already subscribed.

    Atri uses one canonical ``group:<id>`` key per group, so this reduces to a
    membership test; the helper is retained for call-site compatibility.
    """
    key = group_session(origin)
    if origin in sessions:
        return True
    if key is None:
        return False
    return any(group_session(value) == key for value in sessions)


def subscription_targets(
    sessions: list[str], target: str, current_origin: str
) -> list[str]:
    """Match a full ``group:<id>`` key or a bare group number."""
    value = target.strip()
    if ":" in value:
        if group_session(value) is None:
            raise ValueError("请填写完整群会话标识（group:群号）。")
        return [value] if value in sessions else []
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise ValueError("请填写群号或完整群会话标识。")
    current = group_session(current_origin)
    if current is None:
        raise ValueError("无法识别当前平台，请使用完整群会话标识。")
    wanted = (current[0], value)
    return [
        origin for origin in sessions if group_session(origin) == wanted
    ]


class QQGroupSubscriptionChecker:
    """Reconcile groups only after fresh data and an online confirmation.

    The AstrBot version reached into ``context.get_platform_inst``; here the
    OneBot layer injects a ``group_list_provider`` coroutine that returns the
    joined group id strings. Any failure preserves every subscription.
    """

    def __init__(
        self,
        group_list_provider: GroupListProvider | None,
        timeout: float = 10.0,
    ) -> None:
        self.group_list_provider = group_list_provider
        self.timeout = timeout
        self._warned = False

    async def removed_sessions(self, sessions: list[str]) -> set[str]:
        if not callable(self.group_list_provider):
            return set()
        by_group: dict[str, list[str]] = {}
        for origin in sessions:
            parsed = group_session(origin)
            if parsed is not None:
                by_group.setdefault(parsed[1], []).append(origin)
        if not by_group:
            return set()

        try:
            raw = await asyncio.wait_for(
                self.group_list_provider(), timeout=self.timeout
            )
            if not isinstance(raw, list):
                raise TypeError("group list provider did not return a list")
            group_ids: set[str] = set()
            for item in raw:
                group_id = str(item).strip()
                if (
                    not group_id.isascii()
                    or not group_id.isdigit()
                    or int(group_id) <= 0
                ):
                    raise ValueError("group list contains an invalid group id")
                group_ids.add(group_id)
        except Exception as exc:  # noqa: BLE001 - retain subscriptions on uncertainty
            if not self._warned:
                logger.warning(
                    "[HD2] QQ 群列表查询失败，保留订阅: error=%s: %s",
                    type(exc).__name__,
                    exc,
                )
                self._warned = True
            return set()
        self._warned = False

        removed: set[str] = set()
        for group_id, origins in by_group.items():
            if group_id not in group_ids:
                removed.update(origins)
        return removed
