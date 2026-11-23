import json

from atri_qq_bot.persona.engine_tools import EngineToolsMixin
from atri_qq_bot.persona.stream_helpers import (
    apply_sse_event,
    completion_from_state,
    new_stream_state,
    tool_calls_match_schema,
)


def _chunk(content=None, tool_calls=None, finish_reason=None, model="gemini-3.8-flash"):
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"model": model, "choices": [{"index": 0, "delta": delta,
                                         "finish_reason": finish_reason}]}


def test_content_is_concatenated_and_finish_reason_kept():
    state = new_stream_state()
    apply_sse_event(state, _chunk("你好"))
    apply_sse_event(state, _chunk("，亚托莉"))
    apply_sse_event(state, _chunk(content="", finish_reason="stop"))
    assert state["content"] == "你好，亚托莉"
    assert state["finish_reason"] == "stop"
    assert state["model"] == "gemini-3.8-flash"


def test_tool_calls_are_reassembled_by_index():
    state = new_stream_state()
    apply_sse_event(
        state,
        _chunk(
            tool_calls=[
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": ""}}
            ]
        ),
    )
    apply_sse_event(
        state,
        _chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"city":'}}]),
    )
    apply_sse_event(
        state,
        _chunk(tool_calls=[{"index": 0, "function": {"arguments": '"上海"}'}}]),
    )
    apply_sse_event(state, _chunk(finish_reason="tool_calls"))

    completion = completion_from_state(state)
    message = completion["choices"][0]["message"]
    assert message["content"] == ""
    call = message["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "上海"}
    assert completion["choices"][0]["finish_reason"] == "tool_calls"


def test_completion_without_tool_calls_omits_them():
    state = new_stream_state()
    apply_sse_event(state, _chunk("嗨"))
    message = completion_from_state(state)["choices"][0]["message"]
    assert message["role"] == "assistant"
    assert message["content"] == "嗨"
    assert "tool_calls" not in message


def test_missing_finish_reason_is_preserved_not_faked_as_stop():
    # 流被干净切断、没有任何 finish 信号时，必须保留 None，
    # 让调用方识别为截断，而不是伪造 "stop" 把半句当成正常结束。
    state = new_stream_state()
    apply_sse_event(state, _chunk("哼，这种基础"))
    completion = completion_from_state(state)
    assert completion["choices"][0]["finish_reason"] is None
    assert completion["choices"][0]["message"]["content"] == "哼，这种基础"


def test_event_without_choices_is_ignored():
    state = new_stream_state()
    apply_sse_event(state, {"choices": []})
    apply_sse_event(state, {})
    assert state == new_stream_state()


def _completion_with_calls(calls):
    return {"choices": [{"message": {"content": "", "tool_calls": calls}}]}


def test_tool_call_match_passes_without_tool_calls():
    assert tool_calls_match_schema({"choices": [{"message": {"content": "嗨"}}]},
                                   ["query_helldivers"]) is True


def test_tool_call_match_passes_for_known_names_and_valid_args():
    completion = _completion_with_calls([
        {"function": {"name": "query_helldivers",
                      "arguments": json.dumps({"command": "hd2data",
                                               "argument": "扫荡者"})}}
    ])
    assert tool_calls_match_schema(completion, ["query_helldivers"]) is True


def test_tool_call_match_rejects_unknown_function_name():
    # 网关流式 bug：空参的真实函数 + 一个未请求过的通用包装函数。
    completion = _completion_with_calls([
        {"function": {"name": "query_helldivers", "arguments": "{}"}},
        {"function": {"name": "tool",
                      "arguments": json.dumps({"arguments": "{}"})}},
    ])
    assert tool_calls_match_schema(completion, ["query_helldivers"]) is False


def test_tool_call_match_rejects_unparseable_arguments():
    completion = _completion_with_calls([
        {"function": {"name": "query_helldivers", "arguments": "{not json"}},
    ])
    assert tool_calls_match_schema(completion, ["query_helldivers"]) is False


def test_tool_call_match_rejects_non_object_arguments():
    completion = _completion_with_calls([
        {"function": {"name": "query_helldivers", "arguments": json.dumps([1, 2])}},
    ])
    assert tool_calls_match_schema(completion, ["query_helldivers"]) is False


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, headers, json):
        return _FakeStream(self._lines)


def test_stream_transport_reassembles_sse():
    lines = [
        'data: ' + json.dumps(_chunk("哈喽")),
        'data: ' + json.dumps(_chunk("呀", finish_reason="stop")),
        'data: [DONE]',
        '',
    ]
    client = _FakeClient(lines)
    method = EngineToolsMixin._stream_chat_response
    result = method(object(), client, {"Authorization": "Bearer x"},
                    {"model": "m"}, "https://x/v1/chat/completions")
    import asyncio

    completion = asyncio.run(result)
    message = completion["choices"][0]["message"]
    assert message["content"] == "哈喽呀"
    assert completion["choices"][0]["finish_reason"] == "stop"
