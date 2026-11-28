"""模型同轮自报情绪标签驱动表情的单元测试。

覆盖：
- 内部情绪标签的提取与剥离（含非法/缺失/neutral/大小写容错）；
- StickerManager.choose 优先使用标签，标签缺失或 neutral 时回退关键词检测；
- build_outgoing_messages 透传情绪标签；
- _finalize_reply 剥离标签并暂存，OneBot 层经 consume_emotion_label 取出。

本地无 .env、不直连中转站，不发起真实模型调用。
"""
from __future__ import annotations

from types import SimpleNamespace

from atri_qq_bot.config import BotConfig
from atri_qq_bot.message_plan import build_outgoing_messages
from atri_qq_bot.persona import AtriReplyEngine
from atri_qq_bot.persona.reply_rules import _extract_emotion_tag
from atri_qq_bot.stickers import StickerManager


# --- 纯函数：标签提取与剥离 ---


def test_extract_emotion_tag_returns_label_and_strips_marker() -> None:
    label, cleaned = _extract_emotion_tag("哼，谁笨了。\n[[emotion:pout]]")

    assert label == "pout"
    assert "[[emotion:" not in cleaned
    assert "pout" not in cleaned
    assert cleaned.strip() == "哼，谁笨了。"


def test_extract_emotion_tag_without_marker_returns_empty_label() -> None:
    label, cleaned = _extract_emotion_tag("普通一句话")

    assert label == ""
    assert cleaned == "普通一句话"


def test_extract_emotion_tag_ignores_unknown_label_but_still_strips() -> None:
    label, cleaned = _extract_emotion_tag("正文内容\n[[emotion:zzz]]")

    assert label == ""
    assert "[[emotion:" not in cleaned
    assert cleaned.strip() == "正文内容"


def test_extract_emotion_tag_accepts_neutral() -> None:
    label, cleaned = _extract_emotion_tag("平淡一句\n[[emotion:neutral]]")

    assert label == "neutral"
    assert "[[emotion:" not in cleaned


def test_extract_emotion_tag_tolerates_case_and_spaces() -> None:
    label, cleaned = _extract_emotion_tag("正文\n[[ EMOTION : Happy ]]")

    assert label == "happy"
    assert "[[emotion:" not in cleaned


# --- choose：标签优先于关键词 ---


def _manager(tmp_path) -> StickerManager:
    return StickerManager(tmp_path / "stickers")


def test_choose_suppresses_tag_contradicted_by_words(tmp_path) -> None:
    manager = _manager(tmp_path)

    # The user text carries strong happy evidence while the reply has no pout
    # evidence: the lazy pout tag is contradicted and the expression is
    # suppressed instead of sending a mismatched face.
    choice = manager.choose(
        "哈哈你好笨",
        "哼，谁笨了",
        chance=1.0,
        cooldown_seconds=0,
        emotion_override="pout",
    )

    assert choice is None


def test_choose_accepts_evidence_backed_tag(tmp_path) -> None:
    manager = _manager(tmp_path)

    choice = manager.choose(
        "哈哈你好笨",
        "哼哒，谁笨了",
        chance=1.0,
        cooldown_seconds=0,
        emotion_override="pout",
    )

    assert choice is not None
    # The reply itself contains the pout keyword "哼哒": the tag is supported
    # and wins over the user-side happy evidence.
    assert choice.emotion == "pout"


def test_choose_without_override_uses_reply_emotion(tmp_path) -> None:
    manager = _manager(tmp_path)

    # 用户说“哈哈你好笨”不代表亚托莉也在开心；她回“哼哒”才是这轮
    # 真正要配的表情，无标签兜底不能镜像用户情绪。
    choice = manager.choose(
        "哈哈你好笨",
        "哼哒，谁笨了",
        chance=1.0,
        cooldown_seconds=0,
    )

    assert choice is not None
    assert choice.emotion == "pout"


def test_choose_without_override_does_not_mirror_user_anger(tmp_path) -> None:
    manager = _manager(tmp_path)

    # 用户生气，但亚托莉在安抚和接活：这轮应配 comfort，而不是 angry。
    choice = manager.choose(
        "气死我了",
        "别急，我帮你看看怎么回事。",
        chance=1.0,
        cooldown_seconds=0,
    )

    assert choice is not None
    assert choice.emotion == "comfort"


def test_choose_neutral_override_falls_back_to_reply_emotion(tmp_path) -> None:
    manager = _manager(tmp_path)

    choice = manager.choose(
        "哈哈你好笨",
        "哼哒，谁笨了",
        chance=1.0,
        cooldown_seconds=0,
        emotion_override="neutral",
    )

    assert choice is not None
    assert choice.emotion == "pout"


def test_choose_invalid_override_falls_back_to_reply_emotion(tmp_path) -> None:
    manager = _manager(tmp_path)

    choice = manager.choose(
        "他们又欺负你了？",
        "气死我了，我站你这边",
        chance=1.0,
        cooldown_seconds=0,
        emotion_override="not-a-real-emotion",
    )

    assert choice is not None
    assert choice.emotion == "angry"


def test_choose_rejects_angry_tag_for_neutral_reply(tmp_path) -> None:
    manager = _manager(tmp_path)

    # 分享网站、确认需求时，回复本身没有任何生气证据；模型即使误报 angry
    # 也不能直接拿去 angry 目录随机抽图。
    choice = manager.choose(
        "https://example.com 这个网站你可以看看",
        "这个网站我认得，你发这个是打算让我帮你查点什么？",
        chance=1.0,
        cooldown_seconds=0,
        emotion_override="angry",
    )

    assert choice is None
    assert manager.validate_emotion_tag(
        "angry",
        "这个网站你可以看看",
        "这个网站我认得，你发这个是打算让我帮你查点什么？",
    ) == "neutral"


def test_choose_does_not_mirror_user_anger(tmp_path) -> None:
    manager = _manager(tmp_path)

    # 用户生气不代表亚托莉也在生气；她这句是在安抚和接活。
    choice = manager.choose(
        "气死我了",
        "别急，我帮你看看怎么回事。",
        chance=1.0,
        cooldown_seconds=0,
        emotion_override="angry",
    )

    assert choice is None


def test_choose_accepts_angry_tag_with_reply_evidence(tmp_path) -> None:
    manager = _manager(tmp_path)

    choice = manager.choose(
        "他们又欺负你了？",
        "气死我了，我帮你骂回去。",
        chance=1.0,
        cooldown_seconds=0,
        emotion_override="angry",
    )

    assert choice is not None
    assert choice.emotion == "angry"


def test_validate_strong_tag_uses_reply_text_not_user_text(tmp_path) -> None:
    manager = _manager(tmp_path)

    assert manager.validate_emotion_tag(
        "angry", "气死我了", "我帮你看看怎么回事。"
    ) == "neutral"
    assert manager.validate_emotion_tag(
        "angry", "他们又欺负你了？", "气死我了，我帮你骂回去。"
    ) == "angry"


# --- build_outgoing_messages：透传标签 ---


def _builder_config() -> SimpleNamespace:
    return SimpleNamespace(
        message_split_max_parts=3,
        message_split_max_chars=120,
        sticker_chance=1.0,
        sticker_cooldown_seconds=0,
    )


def test_build_outgoing_messages_passes_emotion_label(tmp_path) -> None:
    manager = _manager(tmp_path)
    config = _builder_config()

    messages = build_outgoing_messages(
        "哼，谁笨了",
        "哈哈你好笨",
        manager,
        config,
        None,
        emotion_label="pout",
    )

    # 文本气泡里不应残留内部标签。
    text = "".join(part.content for part in messages if part.kind == "text")
    assert "[[emotion:" not in text


# --- _finalize_reply：剥离标签并暂存，供 OneBot 消费 ---


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


def test_finalize_reply_strips_tag_and_stores_pending_emotion(tmp_path) -> None:
    engine = _engine(tmp_path)

    final = engine._finalize_reply(
        "private:1",
        "你好笨",
        "哼，谁笨了。\n[[emotion:pout]]",
        strict_quality=False,
    )

    assert "[[emotion:" not in final
    assert engine.consume_emotion_label("private:1") == "pout"
    # 消费后即清空，不残留。
    assert engine.consume_emotion_label("private:1") == ""


def test_finalize_reply_without_tag_leaves_empty_pending(tmp_path) -> None:
    engine = _engine(tmp_path)

    final = engine._finalize_reply(
        "private:1",
        "在吗",
        "我在，有什么事直接说。",
        strict_quality=False,
    )

    assert "[[emotion:" not in final
    assert engine.consume_emotion_label("private:1") == ""
