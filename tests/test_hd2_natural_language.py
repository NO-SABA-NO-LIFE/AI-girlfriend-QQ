"""自然语言触发 Helldivers（query_helldivers 工具）的单元测试。

覆盖四层：
1. hd2_intent：参数归一、校验、斜杠重建；
2. schema：工具仅在触发门 + 配置开关同时满足时提供；
3. 引擎 _execute_context_tool：路由到注入执行器及各失败分支；
4. server _hd2_nl_executor：重建斜杠、暂存情报、异常兜底。

异步用例统一用 asyncio.run 包裹（本项目无 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.llm_tools.hd2_intent import (
    HD2_ARGUMENT_COMMANDS,
    HD2_CANONICAL_COMMANDS,
    HD2_FRESHNESS_VALUES,
    HD2IntentError,
    build_hd2_slash,
    detect_hd2_command_hint,
    has_hd2_capability_contradiction,
    normalize_hd2_arguments,
    normalize_hd2_freshness,
)
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.onebot.server_parts.server_connection import OneBotConnectionMixin
from atri_qq_bot.persona import AtriReplyEngine


# ---------------------------------------------------------------------------
# 1. 意图归一
# ---------------------------------------------------------------------------


def test_normalize_accepts_valid_command_with_argument() -> None:
    assert normalize_hd2_arguments(
        {"command": " Planet ", "argument": " 富源 "}
    ) == ("planet", "富源")


def test_normalize_command_is_case_insensitive() -> None:
    assert normalize_hd2_arguments({"command": "DASHBOARD"}) == ("dashboard", "")


def test_normalize_argument_commands_require_argument() -> None:
    for command in HD2_ARGUMENT_COMMANDS:
        with pytest.raises(HD2IntentError):
            normalize_hd2_arguments({"command": command})


def test_normalize_non_argument_commands_discard_argument() -> None:
    # 无参功能即便带了实体也静默清空，不报错、不把闲聊拼进命令。
    assert normalize_hd2_arguments(
        {"command": "hd2ping", "argument": "随便"}
    ) == ("hd2ping", "")


def test_normalize_rejects_unknown_command() -> None:
    with pytest.raises(HD2IntentError):
        normalize_hd2_arguments({"command": "not_a_command"})


def test_normalize_rejects_non_dict_and_missing_command() -> None:
    with pytest.raises(HD2IntentError):
        normalize_hd2_arguments("oops")  # type: ignore[arg-type]
    with pytest.raises(HD2IntentError):
        normalize_hd2_arguments({})


def test_normalize_rejects_overlong_argument() -> None:
    with pytest.raises(HD2IntentError):
        normalize_hd2_arguments({"command": "planet", "argument": "x" * 250})


def test_build_hd2_slash_with_and_without_argument() -> None:
    assert build_hd2_slash("planet", "富源") == "/planet 富源"
    assert build_hd2_slash("dashboard") == "/dashboard"


def test_canonical_commands_count_and_unique() -> None:
    assert len(HD2_CANONICAL_COMMANDS) == 22
    assert len(set(HD2_CANONICAL_COMMANDS)) == 22


# ---------------------------------------------------------------------------
# 1b. 矛盾文本检测与命令提示
# ---------------------------------------------------------------------------


def test_capability_contradiction_detects_incapability_claims() -> None:
    assert has_hd2_capability_contradiction("我手头又没连着面板，查不到 DSS 在哪")
    assert has_hd2_capability_contradiction("这个我做不到哦")
    assert has_hd2_capability_contradiction("无法连接实时数据")


def test_capability_contradiction_ignores_normal_guide() -> None:
    assert not has_hd2_capability_contradiction("卡片来啦，自己看一眼吧")
    assert not has_hd2_capability_contradiction("")


def test_detect_hint_maps_english_abbreviation_with_word_boundary() -> None:
    assert detect_hd2_command_hint("看看 dss 的状态") == "dss"
    assert detect_hd2_command_hint("DSS now") == "dss"
    # 词边界：dss 不应命中包含它的更长单词。
    assert detect_hd2_command_hint("discuss this") is None


def test_detect_hint_maps_chinese_aliases_with_specific_first() -> None:
    assert detect_hd2_command_hint("民主空间站现在什么情况") == "dss"
    assert detect_hd2_command_hint("帮我看看战况总览") == "dashboard"
    assert detect_hd2_command_hint("军需簿里有啥") == "hd2data"


def test_detect_hint_returns_none_for_unrelated_text() -> None:
    assert detect_hd2_command_hint("今天天气真好") is None
    assert detect_hd2_command_hint("") is None


# ---------------------------------------------------------------------------
# 2. schema 门控
# ---------------------------------------------------------------------------


def _base_config(**overrides: Any) -> SimpleNamespace:
    values = dict(
        llm_tools_enabled=True,
        web_search_enabled=False,
        voice_tts_enabled=False,
        llm_agent_protocol_enabled=False,
        hd2_natural_language_enabled=True,
        max_tokens=1200,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_schema_exposes_tool_only_when_gate_open() -> None:
    names_closed = {
        t["function"]["name"]
        for t in available_tool_schemas(_base_config(), hd2_nl_allowed=False)
    }
    names_open = {
        t["function"]["name"]
        for t in available_tool_schemas(_base_config(), hd2_nl_allowed=True)
    }
    assert "query_helldivers" not in names_closed
    assert "query_helldivers" in names_open


def test_schema_hidden_when_config_disabled() -> None:
    names = {
        t["function"]["name"]
        for t in available_tool_schemas(
            _base_config(hd2_natural_language_enabled=False),
            hd2_nl_allowed=True,
        )
    }
    assert "query_helldivers" not in names


def test_schema_enum_matches_canonical_commands() -> None:
    tool = next(
        t
        for t in available_tool_schemas(_base_config(), hd2_nl_allowed=True)
        if t["function"]["name"] == "query_helldivers"
    )
    params = tool["function"]["parameters"]
    assert params["properties"]["command"]["enum"] == list(HD2_CANONICAL_COMMANDS)
    assert params["required"] == ["command"]


# ---------------------------------------------------------------------------
# 3. 引擎上下文工具路由
# ---------------------------------------------------------------------------


def _engine() -> AtriReplyEngine:
    config = BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="mention",
        openai_api_key="test-key",
        openai_base_url="https://example.com/v1",
        openai_model="test-model",
        temperature=0.6,
        max_tokens=350,
        chat_stream_enabled=False,
    )
    return AtriReplyEngine(config)


def test_engine_routes_query_to_injected_executor() -> None:
    engine = _engine()
    seen: list[tuple[str, str, str, str]] = []

    async def executor(
        conversation_id: str,
        command: str,
        argument: str,
        freshness: str = "auto",
    ) -> str:
        seen.append((conversation_id, command, argument, freshness))
        return "情报已就绪。"

    engine.set_hd2_tool_executor(executor)
    result = asyncio.run(
        engine._execute_context_tool(
            "private:1",
            "query_helldivers",
            {"command": "planet", "argument": "富源"},
            engine.config,
        )
    )
    assert result == "情报已就绪。"
    assert seen == [("private:1", "planet", "富源", "auto")]


def test_engine_reports_when_executor_missing() -> None:
    engine = _engine()  # 默认未注入执行器
    result = asyncio.run(
        engine._execute_context_tool(
            "private:1", "query_helldivers", {"command": "dashboard"}, engine.config
        )
    )
    assert "未启用" in result


def test_engine_reports_invalid_arguments() -> None:
    engine = _engine()

    async def executor(*_: Any) -> str:
        raise AssertionError("invalid args must not reach executor")

    engine.set_hd2_tool_executor(executor)
    result = asyncio.run(
        engine._execute_context_tool(
            "private:1", "query_helldivers", {"command": "planet"}, engine.config
        )
    )
    assert "参数无效" in result


def test_engine_reports_executor_exception() -> None:
    engine = _engine()

    async def executor(*_: Any) -> str:
        raise RuntimeError("boom")

    engine.set_hd2_tool_executor(executor)
    result = asyncio.run(
        engine._execute_context_tool(
            "private:1",
            "query_helldivers",
            {"command": "dashboard"},
            engine.config,
        )
    )
    assert "获取失败" in result


def test_contradiction_text_after_ready_card_is_rejected(monkeypatch) -> None:
    import httpx

    posts: list[dict[str, Any]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    def _tool_call_response() -> FakeResponse:
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-hd2",
                                    "type": "function",
                                    "function": {
                                        "name": "query_helldivers",
                                        "arguments": json.dumps({"command": "dss"}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )

    def _text_response(text: str) -> FakeResponse:
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self, url: str, headers: dict[str, str], json: dict[str, Any],
            timeout: Any = None,
        ) -> FakeResponse:
            posts.append(json)
            if len(posts) == 1:
                return _tool_call_response()
            if len(posts) == 2:
                return _text_response("查不到 DSS 现在停在哪")
            return _text_response("卡片来啦，自己看一眼")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    engine = _engine()

    async def executor(*_: Any) -> str:
        return "Helldivers 情报已就绪，会随本轮回复一起发出。"

    engine.set_hd2_tool_executor(executor)
    reply = asyncio.run(
        engine._reply_with_api("private:1", "看看 dss 的状态", None)
    )

    # 第 2 次的"查不到"被拒，第 3 次的正确引导才作为最终回复。
    assert reply == "卡片来啦，自己看一眼"
    assert len(posts) == 3


def test_metadata_only_reply_is_regenerated_with_real_content(monkeypatch) -> None:
    import httpx

    posts: list[dict[str, Any]] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self._text = text

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": self._text},
                        "finish_reason": "stop",
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self, url: str, headers: dict[str, str], json: dict[str, Any],
            timeout: Any = None,
        ) -> FakeResponse:
            posts.append(json)
            # 第 1 次：只有 @称呼/括号/时间标签的空壳回复；第 2 次：真正的问候。
            if len(posts) == 1:
                return FakeResponse("@天心 (steam好友码:46109848) [27分钟前]。")
            return FakeResponse("早呀，今天也要元气满满")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    engine = _engine()
    # 空内容的重试发生在 guarded_api 层（而非 runner 内部），故驱动该方法。
    reply = asyncio.run(engine._reply_with_guarded_api("private:1", "早呀", None))

    # 第 1 次空壳回复被拒，第 2 次重新生成出真实问候。
    assert reply == "早呀，今天也要元气满满"
    assert len(posts) == 2


# ---------------------------------------------------------------------------
# 4. server 执行器
# ---------------------------------------------------------------------------


def _server_harness(messages: Any) -> SimpleNamespace:
    async def handle(text: str, session_key: str) -> Any:
        handle.called_with = (text, session_key)  # type: ignore[attr-defined]
        return messages

    return SimpleNamespace(
        helldivers=SimpleNamespace(handle=handle),
        _pending_hd2_nl={},
    )


def test_server_executor_stores_intel_and_returns_guidance() -> None:
    outgoing = [SimpleNamespace(kind="image", content="/tmp/a.png")]
    harness = _server_harness(outgoing)
    result = asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "planet", "富源"
        )
    )
    # 斜杠命令被正确重建并传给 bridge。
    assert harness.helldivers.handle.called_with == (  # type: ignore[attr-defined]
        "/planet 富源",
        "private:1",
    )
    assert harness._pending_hd2_nl == {"private:1": outgoing}
    assert "情报已就绪" in result


def test_server_executor_handles_empty_result() -> None:
    harness = _server_harness(None)
    result = asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "dashboard", ""
        )
    )
    assert harness._pending_hd2_nl == {}
    assert "没有返回情报" in result


def test_server_executor_handles_bridge_exception() -> None:
    async def raise_handle(text: str, session_key: str) -> Any:
        raise RuntimeError("network down")

    harness = SimpleNamespace(
        helldivers=SimpleNamespace(handle=raise_handle),
        _pending_hd2_nl={},
    )
    result = asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "dashboard", ""
        )
    )
    assert harness._pending_hd2_nl == {}
    assert "获取失败" in result


# ---------------------------------------------------------------------------
# 5. freshness：默认复用、explicit latest 重抓
# ---------------------------------------------------------------------------


def test_normalize_hd2_freshness_falls_back_to_auto() -> None:
    assert normalize_hd2_freshness({"command": "warfront"}) == "auto"
    assert normalize_hd2_freshness({"freshness": ""}) == "auto"
    assert normalize_hd2_freshness({"freshness": "LATEST"}) == "latest"
    assert normalize_hd2_freshness({"freshness": 1}) == "auto"
    assert normalize_hd2_freshness({"freshness": "whatever"}) == "auto"
    assert "latest" in HD2_FRESHNESS_VALUES


def _reuse_harness(messages: Any) -> SimpleNamespace:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def handle(text: str, session_key: str, *, realtime: bool = False) -> Any:
        calls.append((text, session_key, {"realtime": realtime}))
        return messages

    harness = SimpleNamespace(
        helldivers=SimpleNamespace(handle=handle),
        _pending_hd2_nl={},
        _hd2_intel_cache={},
        config=SimpleNamespace(hd2_intel_reuse_seconds=300),
    )
    harness.handle_calls = calls  # type: ignore[attr-defined]
    return harness


def test_executor_reuses_recent_result_without_refetch(tmp_path) -> None:
    image = tmp_path / "warfront.png"
    image.write_bytes(b"png")
    outgoing = [SimpleNamespace(kind="image", content=str(image))]
    harness = _reuse_harness(outgoing)

    first = asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "warfront", "终结族", "auto"
        )
    )
    assert "情报已就绪" in first
    assert len(harness.handle_calls) == 1  # type: ignore[attr-defined]

    second = asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "warfront", "终结族", "auto"
        )
    )
    # 复用：不重新抓取，但依旧把同一份结果挂到待发队列，用户看得到图。
    assert len(harness.handle_calls) == 1  # type: ignore[attr-defined]
    assert "已复用" in second
    assert harness._pending_hd2_nl == {"private:1": outgoing}


def test_executor_latest_forces_refetch_with_realtime(tmp_path) -> None:
    image = tmp_path / "warfront.png"
    image.write_bytes(b"png")
    outgoing = [SimpleNamespace(kind="image", content=str(image))]
    harness = _reuse_harness(outgoing)

    asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "warfront", "终结族", "auto"
        )
    )
    asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "warfront", "终结族", "latest"
        )
    )
    assert len(harness.handle_calls) == 2  # type: ignore[attr-defined]
    assert harness.handle_calls[-1][2] == {"realtime": True}  # type: ignore[attr-defined]


def test_executor_skips_reuse_when_image_file_is_gone(tmp_path) -> None:
    image = tmp_path / "warfront.png"
    image.write_bytes(b"png")
    outgoing = [SimpleNamespace(kind="image", content=str(image))]
    harness = _reuse_harness(outgoing)

    asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "warfront", "终结族", "auto"
        )
    )
    image.unlink()
    result = asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "warfront", "终结族", "auto"
        )
    )
    # 文件没了就不能复用，必须重新抓一次。
    assert len(harness.handle_calls) == 2  # type: ignore[attr-defined]
    assert "情报已就绪" in result


def test_executor_reuse_expires_after_window(tmp_path, monkeypatch) -> None:
    image = tmp_path / "warfront.png"
    image.write_bytes(b"png")
    outgoing = [SimpleNamespace(kind="image", content=str(image))]
    harness = _reuse_harness(outgoing)

    asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "warfront", "终结族", "auto"
        )
    )
    # 把缓存时间戳挪到窗口之外。
    key = ("private:1", "warfront", "终结族")
    harness._hd2_intel_cache[key] = (time.time() - 400, outgoing)
    asyncio.run(
        OneBotConnectionMixin._hd2_nl_executor(
            harness, "private:1", "warfront", "终结族", "auto"
        )
    )
    assert len(harness.handle_calls) == 2  # type: ignore[attr-defined]


def test_schema_exposes_freshness_parameter() -> None:
    config = BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="mention",
        openai_api_key="test-key",
        openai_base_url="https://example.com/v1",
        openai_model="test-model",
        temperature=0.6,
        max_tokens=350,
        chat_stream_enabled=False,
    )
    schemas = available_tool_schemas(config, hd2_nl_allowed=True)
    by_name = {item["function"]["name"]: item for item in schemas}
    params = by_name["query_helldivers"]["function"]["parameters"]["properties"]
    assert params["freshness"]["enum"] == ["auto", "latest"]
    assert "freshness" not in by_name["query_helldivers"]["function"]["parameters"]["required"]
