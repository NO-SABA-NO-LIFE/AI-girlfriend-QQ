from __future__ import annotations

import json
from typing import Any


def new_stream_state() -> dict[str, Any]:
    """累加 SSE delta 用的内部状态。"""

    return {"content": "", "tool_calls": {}, "finish_reason": None, "model": ""}


def apply_sse_event(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """把一条 OpenAI 流式 chunk（已解析的 dict）合并进累加状态。

    纯函数式累积：content 直接拼接；tool_calls 按 index 归位，id/name 取首个
    非空值、arguments 分片拼接；finish_reason 取最后一个非空值。
    """

    choices = event.get("choices") or []
    if not choices:
        return state
    choice = choices[0]
    delta = choice.get("delta") or {}

    piece = delta.get("content")
    if piece:
        state["content"] += piece

    for tool_delta in delta.get("tool_calls") or []:
        index = int(tool_delta.get("index", 0))
        slot = state["tool_calls"].setdefault(
            index,
            {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if tool_delta.get("id"):
            slot["id"] = tool_delta["id"]
        if tool_delta.get("type"):
            slot["type"] = tool_delta["type"]
        function_delta = tool_delta.get("function") or {}
        if function_delta.get("name"):
            slot["function"]["name"] = function_delta["name"]
        if function_delta.get("arguments"):
            slot["function"]["arguments"] += function_delta["arguments"]

    finish_reason = choice.get("finish_reason")
    if finish_reason:
        state["finish_reason"] = finish_reason
    if event.get("model"):
        state["model"] = event["model"]
    return state


def completion_from_state(state: dict[str, Any]) -> dict[str, Any]:
    """把累加状态重组成与非流式 response.json() 同构的 chat completion。

    finish_reason 如实返回：上游明确给出时为 "stop"/"length"/"tool_calls" 等；
    若整条流没有任何 finish 信号（连接被干净切断），这里保留 None，由调用方
    判定为截断并重试，而不是伪装成正常的 "stop"。
    """

    tool_calls = [state["tool_calls"][key] for key in sorted(state["tool_calls"])]
    message: dict[str, Any] = {
        "role": "assistant",
        "content": state["content"] or "",
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-stream",
        "object": "chat.completion",
        "model": state["model"],
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": state["finish_reason"],
            }
        ],
    }


def tool_calls_match_schema(
    completion: dict[str, Any], valid_tool_names: Any
) -> bool:
    """校验流式重组出的工具调用是否都对应本轮实际下发的函数。

    没有工具调用时直接通过。否则每个工具调用都必须：函数名出现在
    ``valid_tool_names`` 中，且非空 arguments 能解析成 JSON 对象。网关在
    流式序列化异常时可能吐出我们从未请求的函数名（例如把一次函数调用拆成
    空参调用加一个通用包装），此时返回 False，由调用方改用非流式重发，
    而不是把坏调用喂给模型或静默降级。
    """

    choice = (completion.get("choices") or [{}])[0]
    tool_calls = ((choice.get("message") or {}).get("tool_calls")) or []
    if not tool_calls:
        return True
    allowed = {str(name) for name in (valid_tool_names or [])}
    for call in tool_calls:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        if name not in allowed:
            return False
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
            except ValueError:
                return False
            if not isinstance(parsed, dict):
                return False
    return True
