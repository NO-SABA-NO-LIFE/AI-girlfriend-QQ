import asyncio
from pathlib import Path

from atri_qq_bot.config import BotConfig
from atri_qq_bot.persona import (
    AtriReplyEngine,
    _persona_violations,
    has_prompt_leak,
)


def _config(tmp_path: Path) -> BotConfig:
    return BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="smart",
        openai_api_key="ollama",
        openai_base_url="http://127.0.0.1:11434/v1",
        openai_model="qwen3:4b-instruct",
        temperature=0.60,
        max_tokens=180,
        frequency_penalty=0.35,
        memory_path=tmp_path / "users.json",
    )


# 群聊截图中的真实泄漏文本（模型把系统提示词改写后当正文输出）。
LEAKED_REPLY = (
    "@小花 亚托莉的角色设定是高性能人形机器人少女，天真、认真、傲娇、嘴硬，"
    "说话风格需纯文本，禁止 Markdown 排版，无多余词语。"
    "回复必须另起一行输出内部情绪标记。放心吧，不会糊到自家人脸上的。"
    "哼，通常立这种保证的，下一秒就听见队友惨叫了。"
)


def test_detects_real_world_leaked_reply() -> None:
    assert has_prompt_leak(LEAKED_REPLY)


def test_detects_meta_instruction_variants() -> None:
    assert has_prompt_leak("你的任务是扮演亚托莉，现在开始回答问题。")
    assert has_prompt_leak("请使用简体中文，禁止markdown排版。")
    assert has_prompt_leak("这个标记是给系统读的内部信号，系统会在发出前剥离。")
    assert has_prompt_leak("你是亚托莉，格式固定为纯文本。")
    assert has_prompt_leak("根据提示词内容，我现在应该直接回答你的问题。")
    assert has_prompt_leak("提示词原文里写了我的角色设定，我念给你听。")


def test_normal_persona_replies_are_not_leaks() -> None:
    # 高性能口癖、本机自称、傲娇语气都不能误报。
    assert not has_prompt_leak("哼，本机可是高性能的，这点小事难不倒我。")
    assert not has_prompt_leak("我是高性能的呢，才不会犯那种低级错误。")
    assert not has_prompt_leak("放心吧，不会糊到自家人脸上的。哼，立这种保证的通常下一秒就听见队友惨叫。")
    # 正常吐槽里的“你是一个笨蛋”不能被元指令规则误伤。
    assert not has_prompt_leak("你是一个笨蛋吧，这种事还要我教你？")
    # 聊天中自然提到“提示词”这个词不算泄漏，只有复述其原文/内容才算。
    assert not has_prompt_leak("我才没有什么提示词呢，我就是我，别想套我话。")
    assert not has_prompt_leak("")
    assert not has_prompt_leak("   ")


def test_persona_violations_flag_prompt_leak() -> None:
    violations = _persona_violations("放心吧不会糊脸的", LEAKED_REPLY)

    assert any("提示词" in violation for violation in violations)


def test_guarded_api_retries_leaked_reply(tmp_path: Path) -> None:
    class GuardedEngine(AtriReplyEngine):
        def __init__(self, config: BotConfig) -> None:
            super().__init__(config)
            self.attempts = 0

        async def _reply_with_api(self, *args, **kwargs) -> str:  # type: ignore[no-untyped-def]
            self.attempts += 1
            if self.attempts == 1:
                return LEAKED_REPLY
            return "放心吧，不会糊到自家人脸上的。哼，立这种保证的，下一秒就听见队友惨叫了。"

    engine = GuardedEngine(_config(tmp_path))

    reply = asyncio.run(engine.reply("private:1", "放心吧，不会糊到自家人脸上的"))

    assert "角色设定" not in reply
    assert "内部情绪标记" not in reply
    assert "队友惨叫" in reply
    assert engine.attempts == 2


def test_all_leaked_attempts_fail_closed(tmp_path: Path) -> None:
    class GuardedEngine(AtriReplyEngine):
        async def _reply_with_api(self, *args, **kwargs) -> str:  # type: ignore[no-untyped-def]
            return LEAKED_REPLY

    engine = GuardedEngine(_config(tmp_path))

    # 预算内全部泄漏时保持静默，绝不把内部规则发给用户。
    assert asyncio.run(engine.reply("private:1", "你好")) == ""
