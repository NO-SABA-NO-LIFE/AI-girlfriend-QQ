"""主聊天 deadline 均分。

历史问题：``reply()`` 一轮里主生成、历史审校、现实/证据修复和重写是串行的，
而每个 httpx 子调用都按完整的 ``chat_request_timeout_seconds``（默认 25s）发起。
模型一慢，前面的调用吃光真实时间，后面的调用仍以完整预算发起，最终整轮
ReadTimeout，用户侧只看到“想太久”的降级文案。

这里守住两层行为：
1. 纯函数把剩余预算在尚未执行的 attempt 间均分，deadline 耗尽返回 0；
2. 管线在拿到 0 时 fail-closed，不再发起注定超时的请求。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.persona import AtriReplyEngine
from atri_qq_bot.persona.input_helpers import (
    _chat_http_timeout,
    chat_attempt_timeout,
    new_chat_deadline,
)


def _config(timeout: float = 25.0) -> SimpleNamespace:
    return SimpleNamespace(chat_request_timeout_seconds=timeout)


def _engine(*, chat_quality_retries: int = 2) -> AtriReplyEngine:
    config = BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="mention",
        openai_api_key="test-key",
        openai_base_url="https://example.com/v1",
        openai_model="test-model",
        temperature=0.8,
        max_tokens=350,
        chat_quality_retries=chat_quality_retries,
    )
    return AtriReplyEngine(config)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_new_chat_deadline_sits_one_budget_in_the_future() -> None:
    config = _config(25.0)
    deadline = new_chat_deadline(config)
    assert 24.0 < deadline - time.monotonic() <= 25.0


def test_attempt_timeout_without_deadline_keeps_full_budget() -> None:
    # 主动消息等独立调用没有本轮 deadline，必须保持旧的固定预算行为。
    config = _config(25.0)
    assert (
        chat_attempt_timeout(config, None, attempt_index=0, total_attempts=3)
        == 25.0
    )


def test_attempt_timeout_splits_remaining_budget_evenly() -> None:
    config = _config(25.0)
    deadline = time.monotonic() + 24.0
    # 第 1 次 attempt：剩余在 3 个 attempt 间均分，约 8s 而不是独占 25s。
    first = chat_attempt_timeout(
        config, deadline, attempt_index=0, total_attempts=3
    )
    assert 7.0 <= first <= 8.5
    # 最后一次 attempt（attempts_left=1）可使用当前全部剩余，但仍受预算上限钳制。
    last = chat_attempt_timeout(
        config, deadline, attempt_index=2, total_attempts=3
    )
    assert 23.0 <= last <= 24.0


def test_attempt_timeout_returns_zero_when_deadline_spent() -> None:
    config = _config(25.0)
    spent_deadline = time.monotonic() - 5.0
    assert (
        chat_attempt_timeout(
            config, spent_deadline, attempt_index=0, total_attempts=1
        )
        == 0.0
    )


def test_attempt_timeout_never_exceeds_configured_budget() -> None:
    config = _config(10.0)
    # deadline 给得很宽，但单次请求仍不能超过配置的 10s 预算。
    deadline = time.monotonic() + 50.0
    assert (
        chat_attempt_timeout(
            config, deadline, attempt_index=0, total_attempts=1
        )
        == 10.0
    )
    assert _chat_http_timeout(config) == 10.0


# ---------------------------------------------------------------------------
# _reply_with_guarded_api 集成
# ---------------------------------------------------------------------------

def _install_fake_client(monkeypatch, *, timeouts: list[Any], posts: dict[str, int]) -> None:
    class FakeResponse:
        def __init__(self, content: str) -> None:
            self._content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": self._content}}
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            timeouts.append(kwargs.get("timeout"))

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
            posts["n"] += 1
            # 前两次返回空内容触发 guarded api 重试，第三次才给合格回复。
            content = "" if posts["n"] < 3 else "好呀，我在听。"
            return FakeResponse(content)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


def test_guarded_api_splits_deadline_across_retries(monkeypatch) -> None:
    timeouts: list[Any] = []
    posts = {"n": 0}
    _install_fake_client(monkeypatch, timeouts=timeouts, posts=posts)
    engine = _engine(chat_quality_retries=2)
    config = engine.config

    reply = asyncio.run(
        engine._reply_with_guarded_api(
            "private:10001",
            "在吗",
            None,
            chat_deadline=new_chat_deadline(config),
        )
    )

    assert reply == "好呀，我在听。"
    assert len(timeouts) == 3
    # 第 1 次不再独占完整 25s，而是约 1/3，给后两次重试留出时间。
    assert isinstance(timeouts[0], float) and timeouts[0] < 10.0
    assert all(isinstance(t, float) and 0.0 < t <= 25.0 for t in timeouts)


def test_guarded_api_makes_no_request_when_deadline_already_spent(monkeypatch) -> None:
    timeouts: list[Any] = []
    posts = {"n": 0}
    _install_fake_client(monkeypatch, timeouts=timeouts, posts=posts)
    engine = _engine(chat_quality_retries=2)
    spent_deadline = time.monotonic() - 10.0

    with pytest.raises(RuntimeError):
        asyncio.run(
            engine._reply_with_guarded_api(
                "private:10001",
                "在吗",
                None,
                chat_deadline=spent_deadline,
            )
        )

    # fail-closed：deadline 已过期时一个 httpx client 都不应建立。
    assert timeouts == []
    assert posts["n"] == 0


def test_review_history_grounding_skips_when_deadline_spent(monkeypatch) -> None:
    constructed: list[Any] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append(kwargs.get("timeout"))

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    engine = _engine()
    spent_deadline = time.monotonic() - 5.0

    result = asyncio.run(
        engine._review_history_grounding(
            "在吗",
            "候选回复",
            [{"role": "user", "content": "在吗"}],
            chat_deadline=spent_deadline,
        )
    )

    assert result == ""
    assert constructed == []
