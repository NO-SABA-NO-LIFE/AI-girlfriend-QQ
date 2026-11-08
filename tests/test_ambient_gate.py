"""群聊接话判断（ambient gate）的单元测试。

验证：
- 模型回答“接/不接”能被正确解析；
- 网络或模型异常时保守返回 False（默认不接）；
- _maybe_ambient_group_reply 的前置过滤、冷却和模型判断协同正确。
本地没有 .env，无法直连真实中转站，因此用 monkeypatch 替换 httpx.AsyncClient。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from atri_qq_bot.onebot.server import OneBotServer


def _make_server() -> OneBotServer:
    server = object.__new__(OneBotServer)
    server.config = SimpleNamespace(
        bot_qq=100000001,
        openai_model="claude-opus-4-7",
        openai_api_key="sk-test",
        openai_base_url="https://api.higanai.top/v1",
        chat_request_timeout_seconds=25.0,
        group_ambient_reply_enabled=True,
        group_ambient_reply_cooldown_seconds=120,
    )
    server._last_ambient_group_reply_at = {}
    return server


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    def __init__(self, content: str | None = None, exc: Exception | None = None) -> None:
        self._content = content
        self._exc = exc

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url: str, headers: dict | None = None, json: dict | None = None):
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(str(self._content))


def _base_event() -> dict:
    return {
        "message_type": "group",
        "group_id": 20001,
        "message": [{"type": "text", "data": {"text": "有人在吗"}}],
    }


# --- 接话判断解析 ---

def test_gate_true_when_model_says_jie(monkeypatch) -> None:
    server = _make_server()
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: _FakeClient(content="接"),
    )
    assert asyncio.run(server._llm_ambient_gate_should_reply("有人在吗")) is True


def test_gate_false_when_model_says_bujie(monkeypatch) -> None:
    server = _make_server()
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: _FakeClient(content="不接"),
    )
    assert asyncio.run(server._llm_ambient_gate_should_reply("今天天气不错")) is False


def test_gate_fails_closed_on_network_error(monkeypatch) -> None:
    server = _make_server()
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: _FakeClient(exc=RuntimeError("boom")),
    )
    assert asyncio.run(server._llm_ambient_gate_should_reply("x")) is False


# --- _maybe_ambient_group_reply 协同 ---

def test_ambient_reply_triggers_when_gate_true() -> None:
    server = _make_server()

    async def fake_gate(text: str, group_context: str = "") -> bool:
        return True

    server._llm_ambient_gate_should_reply = fake_gate
    server._format_ambient_group_context = lambda event: ""
    event = _base_event()
    assert asyncio.run(server._maybe_ambient_group_reply(event, "有人在吗")) is True


def test_ambient_reply_respects_cooldown() -> None:
    server = _make_server()
    gate_calls: list[str] = []

    async def fake_gate(text: str, group_context: str = "") -> bool:
        gate_calls.append(text)
        return True

    server._llm_ambient_gate_should_reply = fake_gate
    server._format_ambient_group_context = lambda event: ""
    event = _base_event()
    first = asyncio.run(server._maybe_ambient_group_reply(event, "第一条"))
    second = asyncio.run(server._maybe_ambient_group_reply(event, "第二条"))
    assert first is True
    # 冷却未到，第二条直接被拦，不会再调用模型判断。
    assert second is False
    assert gate_calls == ["第一条"]


def test_ambient_reply_skips_message_at_other() -> None:
    server = _make_server()

    async def fake_gate(text: str) -> bool:
        raise AssertionError("不应调用接话判断")

    server._llm_ambient_gate_should_reply = fake_gate
    event = _base_event()
    event["message"] = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "你好"}},
    ]
    assert asyncio.run(server._maybe_ambient_group_reply(event, "你好")) is False


def test_ambient_reply_skips_placeholder_only() -> None:
    server = _make_server()

    async def fake_gate(text: str) -> bool:
        raise AssertionError("不应调用接话判断")

    server._llm_ambient_gate_should_reply = fake_gate
    event = _base_event()
    event["message"] = [{"type": "image", "data": {"url": "http://x/a.png"}}]
    assert asyncio.run(server._maybe_ambient_group_reply(event, "[图片]")) is False


def test_ambient_reply_disabled_switch() -> None:
    server = _make_server()
    server.config.group_ambient_reply_enabled = False
    event = _base_event()
    assert asyncio.run(server._maybe_ambient_group_reply(event, "有人在吗")) is False
