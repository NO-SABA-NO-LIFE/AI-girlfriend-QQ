import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.persona import AtriReplyEngine

PRIMARY = "gemini-3.8-flash"
FALLBACK = "gemini-2.5-flash"


def _engine(**over) -> AtriReplyEngine:
    fields = dict(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="mention",
        openai_api_key="test-key",
        openai_base_url="https://relay.example/v1",
        openai_model=PRIMARY,
        temperature=0.8,
        max_tokens=350,
        # 明确整轮预算，验证每个候选的每次请求都拿到完整 50s，而非被瓜分。
        chat_request_timeout_seconds=50,
    )
    fields.update(over)
    return AtriReplyEngine(BotConfig(**fields))


class _FakeStream:
    def __init__(self, lines=None, fail=None):
        self._lines = lines or []
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        if self._fail is not None:
            raise self._fail
        for line in self._lines:
            yield line


def _install_fake(
    monkeypatch, *, seen, primary_fail, fallback_fail=False,
    fallback_text="备用回复",
):
    ok_line = "data: " + json.dumps(
        {"choices": [{"index": 0,
                     "delta": {"content": fallback_text},
                     "finish_reason": "stop"}]}
    )

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = SimpleNamespace(read=kwargs.get("timeout"))

        def stream(self, method, url, headers, json, timeout=None):
            model = json["model"]
            seen["models"].append(model)
            seen["timeouts"].append(timeout)
            if model == PRIMARY and primary_fail:
                return _FakeStream(fail=httpx.ReadTimeout("primary read timeout"))
            if model == FALLBACK and fallback_fail:
                return _FakeStream(fail=httpx.ReadTimeout("fallback read timeout"))
            return _FakeStream([ok_line, "data: [DONE]"])

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient(timeout=25.0)


def test_fallback_model_used_after_primary_read_timeout(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(monkeypatch, seen=seen, primary_fail=True)
    engine = _engine(openai_fallback_model=FALLBACK)
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    result = asyncio.run(
        engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
    )
    assert result["choices"][0]["message"]["content"] == "备用回复"
    # 主模型先做 2 次传输尝试（都超时），再切备用；所有尝试共享同一个 50s 上限。
    assert seen["models"] == [PRIMARY, PRIMARY, FALLBACK]
    assert len(seen["timeouts"]) == 3
    assert all(0.0 < t <= 50.0 for t in seen["timeouts"])
    assert seen["timeouts"] == sorted(seen["timeouts"])  # 剩余预算均分，逐次递增


def test_primary_success_does_not_touch_fallback(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(monkeypatch, seen=seen, primary_fail=False)
    engine = _engine(openai_fallback_model=FALLBACK)
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    result = asyncio.run(
        engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
    )
    assert seen["models"] == [PRIMARY]
    assert len(seen["timeouts"]) == 1
    assert 0.0 < seen["timeouts"][0] <= 50.0


def test_without_fallback_transport_error_is_raised(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(monkeypatch, seen=seen, primary_fail=True)
    engine = _engine()  # 无备用模型
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(
            engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
        )
    # 无备用：主模型独自做满 2 次传输尝试。
    assert seen["models"] == [PRIMARY, PRIMARY]


def test_fallback_also_gets_two_attempts_before_raising(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(
        monkeypatch, seen=seen, primary_fail=True, fallback_fail=True
    )
    engine = _engine(openai_fallback_model=FALLBACK)
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(
            engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
        )
    # 主、备各 50s（名义总预算 100s），单次请求封顶 50；快速失败把剩余时间
    # 转移给后续模型，所以最后一次仍能拿到 50。
    assert seen["models"] == [PRIMARY, PRIMARY, FALLBACK, FALLBACK]
    assert len(seen["timeouts"]) == 4
    assert all(0.0 < t <= 50.0 for t in seen["timeouts"])
    assert seen["timeouts"] == sorted(seen["timeouts"])
    assert seen["timeouts"][-1] == pytest.approx(50.0, abs=0.5)


def test_fast_primary_failure_rolls_remaining_time_to_fallback(monkeypatch):
    # 主模型立即失败（不耗时间），备用模型应能拿到转移过来的时间，
    # 其单次请求达到 50s 封顶，而不是被限制在自己初始的 25s。
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(
        monkeypatch, seen=seen, primary_fail=True, fallback_fail=True
    )
    engine = _engine(openai_fallback_model=FALLBACK)
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(
            engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
        )
    fallback_timeouts = [
        t for m, t in zip(seen["models"], seen["timeouts"]) if m == FALLBACK
    ]
    assert fallback_timeouts[0] == pytest.approx(50.0, abs=0.5)


def test_fallback_fields_default_empty():
    engine = _engine()
    assert engine.config.openai_fallback_model == ""
    assert engine.config.openai_fallback_base_url == ""
