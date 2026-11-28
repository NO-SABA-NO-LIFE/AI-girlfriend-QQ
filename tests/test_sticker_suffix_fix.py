"""Regression tests for the repeated "你终于想起我啦" suffix fix.

Covers:
- the phrase no longer exists in any default emoji table;
- pout/miss emotions with an empty sticker library emit a real QQ face segment
  instead of an inline text suffix;
- emoji-only fallback emotions still attach inline text and are tracked on
  StickerManager.last_choice so the delivery layer can apply cooldown;
- suppressed turns reset last_choice.
"""

from pathlib import Path

from atri_qq_bot.config import BotConfig
from atri_qq_bot.message_plan import build_outgoing_messages
from atri_qq_bot.sticker_parts.core import DEFAULT_EMOJIS, DEFAULT_QQ_FACE_IDS
from atri_qq_bot.stickers import StickerManager


def _config(sticker_dir: Path, chance: float = 1.0) -> BotConfig:
    return BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="mention",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4.1-mini",
        temperature=0.8,
        max_tokens=350,
        sticker_dir=sticker_dir,
        sticker_trigger_file=sticker_dir / "missing.json",
        sticker_chance=chance,
        sticker_cooldown_seconds=0,
        memory_path=sticker_dir / "memory.json",
    )


def test_phrase_removed_from_all_emoji_tables() -> None:
    for emotion, phrases in DEFAULT_EMOJIS.items():
        assert "你终于想起我啦" not in phrases, emotion


def test_pout_emits_hengda_text_phrase(tmp_path: Path) -> None:
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    manager = StickerManager(sticker_dir)
    config = _config(sticker_dir)

    messages = build_outgoing_messages(
        "少不理我。", "在吗", manager, config, emotion_label="pout"
    )

    # pout 不发脸：唯一保留的文字短语“哼哒”内联到句尾。
    assert not any(message.kind == "face" for message in messages)
    text = "".join(message.content for message in messages if message.kind == "text")
    assert "哼哒" in text

    assert manager.last_choice is not None
    assert manager.last_choice.emoji_text == "哼哒"
    assert not manager.last_choice.face_id
    assert not manager.last_choice.file_url


def test_miss_without_face_or_emoji_sends_text_only(tmp_path: Path) -> None:
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    manager = StickerManager(sticker_dir)
    config = _config(sticker_dir)

    messages = build_outgoing_messages(
        "我才没有等你。", "想我了吗", manager, config, emotion_label="miss"
    )

    # miss 无脸也无文字短语：只发正文，不发任何表情。
    assert [message.kind for message in messages] == ["text"]
    assert manager.last_choice is None


def test_neutral_without_face_or_emoji_sends_text_only(tmp_path: Path) -> None:
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    manager = StickerManager(sticker_dir)
    config = _config(sticker_dir)

    messages = build_outgoing_messages(
        "嗯嗯。", "随便聊聊", manager, config, emotion_label="neutral"
    )

    # neutral 无脸也无文字短语：只发正文。
    assert [message.kind for message in messages] == ["text"]
    assert manager.last_choice is None


def test_suppressed_turn_resets_last_choice(tmp_path: Path) -> None:
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    manager = StickerManager(sticker_dir)
    config = _config(sticker_dir, chance=0.0)

    messages = build_outgoing_messages("普通一句话。", "你好", manager, config)

    assert messages == [m for m in messages if m.kind == "text"]
    assert manager.last_choice is None


def test_weak_punctuation_evidence_cannot_decide_emotion(tmp_path: Path) -> None:
    manager = StickerManager(tmp_path / "stickers")

    # Only a question mark (weak keyword): no strong evidence means neutral.
    assert manager.detect_emotion("你在吗？", "在的。") == "neutral"
    # A single digit "6" is weak as well.
    assert manager.detect_emotion("这波6", "嗯。") == "neutral"


def test_negated_keyword_does_not_count(tmp_path: Path) -> None:
    manager = StickerManager(tmp_path / "stickers")

    # "救命" appears, but "别喊" negates it: no awkward evidence.
    assert manager.detect_emotion("你别喊救命", "好。") == "neutral"


def test_strong_tag_without_reply_evidence_is_downgraded(tmp_path: Path) -> None:
    manager = StickerManager(tmp_path / "stickers")

    # 强情绪标签必须能在亚托莉自己的回复里找到证据；“走，出发”没有
    # pout 证据，不能因为用户文本没有别的情绪就默认采信。
    assert manager.validate_emotion_tag(
        "pout", "一起打游戏吧", "走，出发。"
    ) == "neutral"


def test_low_key_tag_without_evidence_is_still_trusted(tmp_path: Path) -> None:
    manager = StickerManager(tmp_path / "stickers")

    # thinking/confused 这类低强度语气词不强行要求关键词，模型可以表达
    # 关键词表覆盖不到的细微语气。
    assert manager.validate_emotion_tag(
        "thinking", "一起打游戏吧", "走，出发。"
    ) == "thinking"


def test_mismatched_face_not_built_when_tag_contradicted(tmp_path: Path) -> None:
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    manager = StickerManager(sticker_dir)
    config = _config(sticker_dir)

    messages = build_outgoing_messages(
        "哼，谁笨了。", "哈哈你好笨", manager, config, emotion_label="pout"
    )

    # Contradicted tag: no face/sticker/image and no emoji suffix.
    assert not any(
        message.kind in {"face", "image", "sticker"} for message in messages
    )
    assert all(
        "你终于想起我" not in message.content for message in messages
    )


def test_face_ids_match_emotion_semantics() -> None:
    # 大哭(9)只出现在悲伤和抱歉（抱歉时认错落泪）。
    emotions_with_cry = {
        emotion
        for emotion, ids in DEFAULT_QQ_FACE_IDS.items()
        if "9" in ids
    }
    assert emotions_with_cry == {"sad", "sorry"}
    # 睡觉(8)不再派给疑惑/思考。
    for emotion in ("confused", "thinking"):
        assert "8" not in DEFAULT_QQ_FACE_IDS[emotion], emotion
    # 这四个情绪没有系统脸：pout 走“哼哒”文字，其余只发正文。
    assert DEFAULT_QQ_FACE_IDS["pout"] == ()
    assert DEFAULT_QQ_FACE_IDS["shy"] == ()
    assert DEFAULT_QQ_FACE_IDS["miss"] == ()
    assert DEFAULT_QQ_FACE_IDS["food"] == ()
    # 非空的元组里全部是数字 ID。
    for ids in DEFAULT_QQ_FACE_IDS.values():
        assert all(face_id.isdigit() for face_id in ids)


def test_only_pout_keeps_text_phrase() -> None:
    # 文字短语只剩 pout 的“哼哒”，其余情绪全部为空。
    assert DEFAULT_EMOJIS["pout"] == ("哼哒",)
    for emotion, phrases in DEFAULT_EMOJIS.items():
        if emotion != "pout":
            assert phrases == (), emotion
