from __future__ import annotations

import time

from atri_qq_bot.config import BotConfig
from atri_qq_bot.persona import AtriReplyEngine

WINDOW = 1800


def _engine(tmp_path) -> AtriReplyEngine:
    config = BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="smart",
        openai_api_key="test-key",
        openai_base_url="https://example.test/v1",
        openai_model="test-model",
        temperature=0.4,
        max_tokens=350,
        memory_path=tmp_path / "memory.json",
        llm_tools_enabled=False,
    )
    return AtriReplyEngine(config)


def _set_history(engine: AtriReplyEngine, entries: list[dict]) -> None:
    item = engine.memory._conversation("private:1")
    item["history"] = entries


def _entry(age_seconds: float, role: str, text: str) -> dict:
    return {"at": time.time() - age_seconds, "role": role, "text": text}


def test_visual_window_drops_stale_pair_entirely(tmp_path) -> None:
    # 4 小时前已闭合的一对对话，视觉轮应整体剔除，而不是只删 assistant。
    engine = _engine(tmp_path)
    _set_history(
        engine,
        [
            _entry(14_400, "user", "没事了，你尽力了"),
            _entry(14_399, "assistant", "勉强接受你的原谅吧"),
        ],
    )

    messages = engine._recent_conversation_messages(
        "private:1", within_seconds=WINDOW
    )

    assert messages == []


def test_visual_window_keeps_recent_pair_with_assistant(tmp_path) -> None:
    # 窗口内的对话成对保留，assistant 也在，保证连贯、不产生断裂。
    engine = _engine(tmp_path)
    _set_history(
        engine,
        [
            _entry(600, "user", "这张图里写了什么"),
            _entry(590, "assistant", "我看看，写的是一句话"),
        ],
    )

    messages = engine._recent_conversation_messages(
        "private:1", within_seconds=WINDOW
    )

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "这张图里写了什么" in messages[0]["content"]


def test_visual_window_keeps_only_recent_when_mixed(tmp_path) -> None:
    engine = _engine(tmp_path)
    _set_history(
        engine,
        [
            _entry(14_400, "user", "没事了，你尽力了"),
            _entry(14_399, "assistant", "勉强接受你的原谅吧"),
            _entry(600, "user", "再帮我看这张"),
            _entry(590, "assistant", "好，这张是另一张图"),
        ],
    )

    messages = engine._recent_conversation_messages(
        "private:1", within_seconds=WINDOW
    )

    assert len(messages) == 2
    assert all("尽力了" not in m["content"] for m in messages)
    assert "再帮我看这张" in messages[0]["content"]


def test_non_visual_turn_keeps_stale_history(tmp_path) -> None:
    # 普通轮不传时间窗，旧行为保持不变：旧对话仍按条数带入。
    engine = _engine(tmp_path)
    _set_history(
        engine,
        [
            _entry(14_400, "user", "没事了，你尽力了"),
            _entry(14_399, "assistant", "勉强接受你的原谅吧"),
        ],
    )

    messages = engine._recent_conversation_messages("private:1")

    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_visual_window_boundary_is_inclusive(tmp_path, monkeypatch) -> None:
    fixed_now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: fixed_now)
    engine = _engine(tmp_path)
    _set_history(
        engine,
        [
            {"at": fixed_now - (WINDOW + 1), "role": "user", "text": "超出窗口一秒"},
            {"at": fixed_now - WINDOW, "role": "user", "text": "恰好在窗口边界"},
        ],
    )

    messages = engine._recent_conversation_messages(
        "private:1", within_seconds=WINDOW
    )

    # reversed 从新到旧：边界条目（age == window）保留，更旧的超窗条目触发 break。
    assert len(messages) == 1
    assert "恰好在窗口边界" in messages[0]["content"]


def test_visual_window_fail_closed_without_timestamp(tmp_path) -> None:
    engine = _engine(tmp_path)
    item = engine.memory._conversation("private:1")
    item["history"] = [{"role": "user", "text": "旧格式消息，没有时间戳"}]

    messages = engine._recent_conversation_messages(
        "private:1", within_seconds=WINDOW
    )

    assert messages == []
