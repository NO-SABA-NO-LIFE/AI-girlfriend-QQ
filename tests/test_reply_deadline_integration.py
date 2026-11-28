import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.persona import engine_tools
from atri_qq_bot.persona.engine_tools import EngineToolsMixin
from atri_qq_bot.persona.input_helpers import ChatRequestBudget, new_chat_deadline
from atri_qq_bot.toolbox import ToolAnalyzer
from atri_qq_bot.toolbox_parts.analysis_result import _VisionModelResponse


def _chat_engine(**overrides) -> EngineToolsMixin:
    config = {
        "openai_model": "main",
        "openai_base_url": "https://chat.example/v1",
        "openai_fallback_models": ("fallback",),
        "chat_request_timeout_seconds": 30.0,
        "chat_quality_retries": 0,
        "chat_min_attempt_seconds": 0.005,
        "chat_stream_enabled": True,
    }
    engine = EngineToolsMixin()
    engine.config = SimpleNamespace(**(config | overrides))
    return engine


def _delta(content: str, finish: str | None = None) -> str:
    return "data: " + json.dumps({
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]
    })


class _ChatStream:
    def __init__(self, *, never_finishes: bool = False) -> None:
        self.never_finishes = never_finishes
        self.closed = False
        self.chunks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        if self.never_finishes:
            while True:
                await asyncio.sleep(0.002)
                self.chunks += 1
                yield _delta("partial")
        else:
            yield _delta("fallback worked", "stop")
            yield "data: [DONE]"


def test_chat_continuous_sse_is_cancelled_before_fallback():
    engine = _chat_engine()
    primary = _ChatStream(never_finishes=True)
    fallback = _ChatStream()
    called_models = []

    class Client:
        def stream(self, method, url, *, headers, json, timeout):
            called_models.append(json["model"])
            if json["model"] == "main":
                return primary
            assert primary.closed
            return fallback

    async def run():
        loop = asyncio.get_running_loop()
        budget = ChatRequestBudget(loop.time() + 0.12)
        return await asyncio.wait_for(
            engine._post_chat_completion(Client(), {}, {}, request_budget=budget),
            timeout=0.75,
        )

    completion = asyncio.run(run())
    assert completion["choices"][0]["message"]["content"] == "fallback worked"
    assert called_models == ["main", "fallback"]
    assert primary.chunks >= 2
    assert fallback.closed


def test_chat_uses_turn_remainder_and_caps_its_own_stage():
    async def run():
        loop = asyncio.get_running_loop()
        now = loop.time()
        config = SimpleNamespace(chat_request_timeout_seconds=30)
        assert new_chat_deadline(config, now + 70) == now + 70
        assert new_chat_deadline(config, now + 120) == pytest.approx(now + 90, abs=0.1)

    asyncio.run(run())


def test_chat_model_usage_is_shared_by_subsequent_requests(monkeypatch):
    monkeypatch.setattr(engine_tools, "_chat_http_timeout", lambda _config: 0.08)
    engine = _chat_engine(chat_stream_enabled=False, openai_fallback_models=())
    requested_timeouts = []

    class Client:
        async def post(self, url, *, headers, json, timeout):
            requested_timeouts.append(timeout)
            await asyncio.sleep(0.044)
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
            })

    async def run():
        budget = ChatRequestBudget(asyncio.get_running_loop().time() + 0.5)
        await engine._post_chat_completion(Client(), {}, {}, request_budget=budget)
        with pytest.raises(TimeoutError):
            await engine._post_chat_completion(Client(), {}, {}, request_budget=budget)

    asyncio.run(run())
    assert len(requested_timeouts) == 2
    assert requested_timeouts[1] < requested_timeouts[0] - 0.03


def test_local_chat_can_switch_while_model_cleanup_keeps_lease(monkeypatch):
    engine = _chat_engine(
        chat_stream_enabled=False,
        openai_base_url="http://127.0.0.1:11434/v1",
        openai_fallback_base_url="https://chat.example/v1",
    )

    async def run():
        cleanup_ready = asyncio.Event()
        cleanup_allowed = asyncio.Event()
        lease_released = asyncio.Event()

        @asynccontextmanager
        async def lease(*args, **kwargs):
            try:
                yield
            finally:
                lease_released.set()

        async def ready(*, timeout_seconds):
            return True

        async def release(model, base_url):
            cleanup_ready.set()
            await cleanup_allowed.wait()

        class Client:
            async def post(self, url, *, headers, json, timeout):
                if json["model"] == "main":
                    await asyncio.Future()
                return httpx.Response(200, request=httpx.Request("POST", url), json={
                    "choices": [{"message": {"content": "cloud"}, "finish_reason": "stop"}]
                })

        monkeypatch.setattr(engine_tools, "inference_resource_lease", lease)
        monkeypatch.setattr(engine_tools, "ensure_ollama_service", ready)
        monkeypatch.setattr(engine_tools, "release_ollama_model", release)
        budget = ChatRequestBudget(asyncio.get_running_loop().time() + 0.12)
        completion = await asyncio.wait_for(
            engine._post_chat_completion(Client(), {}, {}, request_budget=budget),
            timeout=0.75,
        )
        assert completion["choices"][0]["message"]["content"] == "cloud"
        await asyncio.wait_for(cleanup_ready.wait(), timeout=0.4)
        assert not lease_released.is_set()
        cleanup_allowed.set()
        await asyncio.wait_for(lease_released.wait(), timeout=0.4)

    asyncio.run(run())


def test_visual_attempt_timeout_leaves_time_for_fallback(monkeypatch):
    analyzer = ToolAnalyzer(BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="smart",
        openai_api_key=None,
        openai_base_url="https://chat.example/v1",
        openai_model="chat",
        temperature=0.7,
        max_tokens=350,
        toolbox_vision_enabled=True,
        toolbox_vision_model="vision-main",
        toolbox_vision_base_url="https://vision.example/v1",
        toolbox_vision_fallback_model="vision-fallback",
        toolbox_vision_fallback_base_url="https://fallback.example/v1",
        toolbox_vision_retry_count=1,
    ))
    calls = []

    async def analyze(*args, model, request_timeout, **kwargs):
        calls.append((model, request_timeout))
        if model == "vision-main":
            await asyncio.Future()
        return _VisionModelResponse(
            "直接回答：这是一张测试卡片。图中文字：无明显文字；"
            "人物/主体：卡片；身份/出处：未确认；表情/情绪：中性；"
            "表达含义/社交意图：展示测试；依据：画面；置信度：高；"
            "需要联网核验：否；检索关键词：",
            {"finish_reason": "stop"},
        )

    async def run():
        deadline = asyncio.get_running_loop().time() + 0.12
        return await asyncio.wait_for(analyzer._analyze_image_with_vision(
            b"image", "test.png", prepared_images=[b"image"], deadline=deadline
        ), timeout=0.75)

    monkeypatch.setattr(analyzer, "_analyze_image_with_openai_compatible", analyze)
    monkeypatch.setattr(analyzer, "_log_vision_event", lambda **kwargs: None)
    text, limitation = asyncio.run(run())
    assert text and not limitation
    assert [model for model, _ in calls] == ["vision-main", "vision-fallback"]
    assert all(0 < timeout < 0.1 for _, timeout in calls)
