import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.persona import AtriReplyEngine

PRIMARY = "gemini-3.8-flash"
FALLBACK1 = "gemini-2.5-flash"
FALLBACK2 = "gemini-3.6-flash"


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
        # 每个模型独立 30s 预算，验证单次请求封顶 30 而非被瓜分或超过。
        chat_request_timeout_seconds=30,
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


def _install_fake(monkeypatch, *, seen, failing, reply_text="备用回复"):
    ok_line = "data: " + json.dumps(
        {"choices": [{"index": 0,
                     "delta": {"content": reply_text},
                     "finish_reason": "stop"}]}
    )

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = SimpleNamespace(read=kwargs.get("timeout"))

        def stream(self, method, url, headers, json, timeout=None):
            model = json["model"]
            seen["models"].append(model)
            seen["timeouts"].append(timeout)
            if model in failing:
                return _FakeStream(fail=httpx.ReadTimeout(model + " timeout"))
            return _FakeStream([ok_line, "data: [DONE]"])

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient(timeout=25.0)


def test_fallback_model_used_after_primary_read_timeout(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(monkeypatch, seen=seen, failing={PRIMARY})
    engine = _engine(openai_fallback_models=(FALLBACK1,))
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    result = asyncio.run(
        engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
    )
    assert result["choices"][0]["message"]["content"] == "备用回复"
    # 主模型先做 2 次传输尝试（都超时），再切备用；所有尝试单次封顶 30s。
    assert seen["models"] == [PRIMARY, PRIMARY, FALLBACK1]
    assert len(seen["timeouts"]) == 3
    assert all(0.0 < t <= 30.0 for t in seen["timeouts"])
    assert seen["timeouts"] == sorted(seen["timeouts"])


def test_primary_success_does_not_touch_fallback(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(monkeypatch, seen=seen, failing=set())
    engine = _engine(openai_fallback_models=(FALLBACK1,))
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    asyncio.run(
        engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
    )
    assert seen["models"] == [PRIMARY]
    assert 0.0 < seen["timeouts"][0] <= 30.0


def test_without_fallback_transport_error_is_raised(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(monkeypatch, seen=seen, failing={PRIMARY})
    engine = _engine()  # 无备用模型
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(
            engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
        )
    assert seen["models"] == [PRIMARY, PRIMARY]


def test_fallback_gets_two_attempts_before_raising(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(
        monkeypatch, seen=seen, failing={PRIMARY, FALLBACK1}
    )
    engine = _engine(openai_fallback_models=(FALLBACK1,))
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(
            engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
        )
    # 主、备各 30s（名义总预算 60s），单次封顶 30；快速失败把剩余时间转移，
    # 所以最后一次仍能拿到 30。
    assert seen["models"] == [PRIMARY, PRIMARY, FALLBACK1, FALLBACK1]
    assert all(0.0 < t <= 30.0 for t in seen["timeouts"])
    assert seen["timeouts"][-1] == pytest.approx(30.0, abs=0.5)


def test_fast_primary_failure_rolls_remaining_time_to_fallback(monkeypatch):
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(
        monkeypatch, seen=seen, failing={PRIMARY, FALLBACK1}
    )
    engine = _engine(openai_fallback_models=(FALLBACK1,))
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(
            engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
        )
    fallback_timeouts = [
        t for m, t in zip(seen["models"], seen["timeouts"])
        if m == FALLBACK1
    ]
    assert fallback_timeouts[0] == pytest.approx(30.0, abs=0.5)


def test_second_fallback_in_chain_is_used_in_order(monkeypatch):
    # 主模型和第一个备用都失败，应继续沿链切到第二个备用并成功。
    seen = {"models": [], "timeouts": []}
    outer = _install_fake(
        monkeypatch, seen=seen, failing={PRIMARY, FALLBACK1}
    )
    engine = _engine(openai_fallback_models=(FALLBACK1, FALLBACK2))
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    result = asyncio.run(
        engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
    )
    assert result["choices"][0]["message"]["content"] == "备用回复"
    # 主(2) → 备用1(2) → 备用2 首次成功；每个单次请求都不超过 30s。
    assert seen["models"] == [
        PRIMARY, PRIMARY, FALLBACK1, FALLBACK1, FALLBACK2
    ]
    assert all(0.0 < t <= 30.0 for t in seen["timeouts"])


def test_fallback_fields_default_empty():
    engine = _engine()
    assert engine.config.openai_fallback_models == ()
    assert engine.config.openai_fallback_base_url == ""


def _truncated_lines(mode: str) -> list[str]:
    if mode == "length":
        # 上游明确以 length 结束：内容被 token 上限切断。
        return [
            "data: " + json.dumps(
                {"choices": [{"index": 0, "delta": {"content": "哼，这种基础"},
                              "finish_reason": None}]}
            ),
            "data: " + json.dumps(
                {"choices": [{"index": 0, "delta": {},
                              "finish_reason": "length"}]}
            ),
            "data: [DONE]",
        ]
    # 干净切断：吐出部分内容后流结束，没有任何 finish 信号。
    return [
        "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {"content": "哼，这种基础"},
                          "finish_reason": None}]}
        ),
        "data: [DONE]",
    ]


def _install_truncating_fake(monkeypatch, *, seen, mode):
    ok_line = "data: " + json.dumps(
        {"choices": [{"index": 0, "delta": {"content": "备用回复"},
                      "finish_reason": "stop"}]}
    )

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = SimpleNamespace(read=kwargs.get("timeout"))

        def stream(self, method, url, headers, json, timeout=None):
            model = json["model"]
            seen["models"].append(model)
            if model == PRIMARY:
                return _FakeStream(_truncated_lines(mode))
            return _FakeStream([ok_line, "data: [DONE]"])

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient(timeout=25.0)


@pytest.mark.parametrize("mode", ["length", "cut"])
def test_truncated_primary_falls_back_to_next_model(monkeypatch, mode):
    seen = {"models": []}
    outer = _install_truncating_fake(monkeypatch, seen=seen, mode=mode)
    engine = _engine(openai_fallback_models=(FALLBACK1,))
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "x"}]}

    result = asyncio.run(
        engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
    )
    # 主模型两次截断尝试都不被接受，随后切到备用模型拿到完整回复。
    assert result["choices"][0]["message"]["content"] == "备用回复"
    assert seen["models"] == [PRIMARY, PRIMARY, FALLBACK1]


def _malformed_streaming_tool_lines() -> list[str]:
    # 复刻网关流式 bug：空参 query_helldivers + 未请求过的 "tool" 包装。
    def chunk(tool_calls=None, finish=None):
        delta = {"tool_calls": tool_calls} if tool_calls is not None else {}
        return "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        )

    return [
        chunk([{"index": 0, "id": "c0", "type": "function",
                "function": {"name": "query_helldivers", "arguments": ""}}]),
        chunk([{"index": 0, "function": {"arguments": "{}"}}]),
        chunk([{"index": 1, "id": "c1", "type": "function",
                "function": {"name": "tool", "arguments": ""}}]),
        chunk([{"index": 1, "function": {
            "arguments": json.dumps({"arguments": json.dumps(
                {"command": "hd2data", "argument": "扫荡者"})})}}]),
        chunk(finish="tool_calls"),
        "data: [DONE]",
    ]


def test_malformed_streaming_tool_call_retries_non_stream(monkeypatch):
    from atri_qq_bot.llm_tools.schema import _hd2_query_schema

    seen = {"stream": 0, "post": 0}

    class _FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    correct = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c0", "type": "function",
                            "function": {
                                "name": "query_helldivers",
                                "arguments": json.dumps(
                                    {"command": "hd2data", "argument": "扫荡者"}
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = SimpleNamespace(read=kwargs.get("timeout"))

        def stream(self, method, url, headers, json, timeout=None):
            seen["stream"] += 1
            return _FakeStream(_malformed_streaming_tool_lines())

        async def post(self, url, headers, json, timeout=None):
            seen["post"] += 1
            # 兜底必须显式关闭流式，重发同一请求。
            assert json.get("stream") is False
            return _FakeResponse(correct)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    outer = FakeAsyncClient(timeout=25.0)
    engine = _engine()
    payload = {
        "model": PRIMARY,
        "messages": [{"role": "user", "content": "帮我查扫荡者"}],
        "tools": [_hd2_query_schema()],
    }

    result = asyncio.run(
        engine._post_chat_completion(outer, {"Authorization": "Bearer k"}, payload)
    )
    call = result["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "query_helldivers"
    assert json.loads(call["function"]["arguments"]) == {
        "command": "hd2data", "argument": "扫荡者"
    }
    # 一次流式尝试被判定损坏，随后一次非流式重发成功。
    assert seen == {"stream": 1, "post": 1}
