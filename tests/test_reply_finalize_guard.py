"""回复收尾守卫：自然回复不能被误判成"失败兜底文案"。

历史事故：``FAILURE_TEMPLATE_PATTERNS`` 里含 "在呢，刚忙完呢"。模型回答
"在吗" 时经常这么说，于是整条回复被 ``_finalize_reply`` 丢弃，用户侧的表现
就是"发了消息完全没反应"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from atri_qq_bot.persona import (
    core as persona_core,
    engine_context,
    reply_fallbacks,
    reply_rules,
)


def _finalize(reply_text: str, *, user_text: str = "在吗") -> str:
    return engine_context.EngineContextMixin._finalize_reply(
        SimpleNamespace(),
        "private:900000001",
        user_text,
        reply_text,
        strict_quality=False,
    )


def test_natural_short_reply_is_not_rejected() -> None:
    # 这句曾经被当成失败兜底文案，导致"在吗"完全得不到回复。
    assert _finalize("在呢，刚忙完呢") == "在呢，刚忙完呢"


def test_real_failure_templates_are_still_rejected() -> None:
    for text in ("刚才没接住，再说一遍吧", "这张图这次没有稳定识别出来"):
        with pytest.raises(RuntimeError):
            _finalize(text)


def test_failure_pattern_list_keeps_only_fallback_only_phrases() -> None:
    assert "在呢，刚忙完呢" not in persona_core.FAILURE_TEMPLATE_PATTERNS
    assert "刚才没接住，再说一遍吧" in persona_core.FAILURE_TEMPLATE_PATTERNS


def test_relative_time_tag_at_start_is_stripped() -> None:
    # 句首标签（私聊/群聊都出现过），删掉后不应残留行首空格。
    assert reply_rules._normalize_reply("[34分钟前] 哼，问就问了，别催。") == "哼，问就问了，别催。"


def test_relative_time_tag_in_middle_is_stripped() -> None:
    # 标签夹在句中、紧贴中文句号：去掉标签并清理中文标点前的空格。
    assert reply_rules._strip_relative_time_tags(
        "@天心 (steam好友码:46109848) [27分钟前]。"
    ) == "@天心 (steam好友码:46109848)。"


def test_all_relative_time_label_forms_are_stripped() -> None:
    cases = {
        "[13分钟前] 光艾特不说话": "光艾特不说话",
        "[2小时前] 你才来呀": "你才来呀",
        "[一天前] 昨天的事": "昨天的事",
        "[3天前] 好久不见": "好久不见",
    }
    for raw, expected in cases.items():
        assert reply_rules._strip_relative_time_tags(raw) == expected


def test_non_time_brackets_are_preserved() -> None:
    # 不是相对时间标签的方括号（编号/索引等）必须原样保留，不能误伤。
    text = "战线排名：[3] 某星球，注意 [DONE] 标记。"
    assert reply_rules._strip_relative_time_tags(text) == text


def test_metadata_only_reply_has_no_substantive_content() -> None:
    # 剥掉 @称呼、圆括号、时间标签和标点后没有任何实质字符 → 空回复。
    assert reply_rules._has_substantive_content(
        "@天心 (steam好友码:46109848) [27分钟前]。"
    ) is False


def test_real_greeting_has_substantive_content() -> None:
    cases = (
        "早上好呀！",
        "@天心 早呀，今天也精神满满",
        "哼。",  # 单个语气词也算实质内容，不能误判。
    )
    for text in cases:
        assert reply_rules._has_substantive_content(text) is True


def test_current_speaker_boundary_prompt_names_speaker() -> None:
    prompt = reply_rules._current_speaker_boundary_prompt("友川纪夫", "依旧机器人大乱斗")
    # 必须点名当前说话人，并明确不要把别人的话安到他头上。
    assert "友川纪夫" in prompt
    assert "张冠李戴" in prompt


def test_textless_turn_prompt_keeps_other_speakers_words_apart() -> None:
    # 戳一戳无文字：服务器输入为占位符 + 回戳反馈。
    poke_input = "[用户未附加文字]\n\n【系统动作反馈】你刚刚已经回戳了对方一下。"
    assert reply_rules._is_textless_turn(poke_input) is True
    prompt = reply_rules._current_speaker_boundary_prompt("潇潇", poke_input)
    assert "潇潇" in prompt
    # 明确当前说话人本轮没发文字。
    assert "没有发送文字" in prompt
    # 同 #号（哪怕昵称变了、消息很多条）都是本人说的，不同 #号属于其他群友。
    assert "消息有很多条" in prompt
    assert "其他群友" in prompt
    assert "张冠李戴" in prompt


def test_mentioned_turn_prompt_tracks_addressee_not_only_speaker() -> None:
    # 被@回复：除了区分“谁在说话”，还要判断“对谁说”，防止把当前说话人对其他
    # 群友说的无@话当成对自己说的（张冠李戴复发场景）。
    prompt = reply_rules._current_speaker_boundary_prompt(
        "一生愛して希尔薇", "@亚托莉 我讨厌你"
    )
    # 第一层：说话人锚点仍在。
    assert "昵称#QQ号" in prompt
    # 第二层：明确要判断对话对象。
    assert "对谁说" in prompt
    # 无@的话可能在回应其他群友。
    assert "回应其他群友" in prompt
    # 代词不要默认成机器人自己。
    assert "不要默认成你自己" in prompt
    # 拿不准就不引用。
    assert "就不要引用" in prompt


def test_textless_turn_prompt_also_tracks_addressee() -> None:
    # 无文字分支共用 ownership，同样带 addressee 提醒。
    poke_input = "[用户未附加文字]\n\n【系统动作反馈】你刚刚已经回戳了对方一下。"
    prompt = reply_rules._current_speaker_boundary_prompt("潇潇", poke_input)
    assert "对谁说" in prompt
    assert "回应其他群友" in prompt


def test_mentioned_turn_prompt_keeps_action_object_consistent() -> None:
    # 被追问“配合谁/针对谁”时，必须回溯上下文和自己此前原话，与之前发言一致，
    # 不能为嘴硬把动作对象挪给别人（配合对象被错误指认事故）。
    prompt = reply_rules._current_speaker_boundary_prompt(
        "内谁", "@亚托莉 配合谁一下"
    )
    assert "守住前后一致" in prompt
    assert "你自己此前的原话" in prompt
    # 嘴硬只能找台阶，不能改写已说出口的话或动作对象。
    assert "不能改写已经说出口的话" in prompt
    assert "动作对象" in prompt
    # 记不清 fail-closed，不许现编。
    assert "记不清就如实说记不清" in prompt


def test_textless_turn_prompt_also_keeps_action_object_consistent() -> None:
    # 无文字分支同样带上第三层一致性规则。
    poke_input = "[用户未附加文字]\n\n【系统动作反馈】你刚刚已经回戳了对方一下。"
    prompt = reply_rules._current_speaker_boundary_prompt("潇潇", poke_input)
    assert "守住前后一致" in prompt
    assert "动作对象" in prompt


def test_group_speaker_label_uses_qq_anchor() -> None:
    assert reply_rules._group_speaker_label("友川纪夫", 904994489) == "友川纪夫#904994489"
    # 没有 actor_id 的旧消息 fail-safe 退化为纯昵称。
    assert reply_rules._group_speaker_label("友川纪夫", None) == "友川纪夫"
    # 纯数字昵称（无昵称）兜底为“群友”，但仍带 QQ 锚点。
    assert reply_rules._group_speaker_label("904994489", 904994489) == "群友#904994489"


def test_normalize_reply_strips_speaker_anchor() -> None:
    # 模型若把内部锚点抄进回复，统一清洗时必须剥除，且不影响正常文字。
    raw = "什么叫被我拐跑啊#904994489，明明是你自己想跑。"
    out = reply_rules._normalize_reply(raw)
    assert "#904994489" not in out
    assert "什么叫被我拐跑啊" in out
    assert "明明是你自己想跑" in out
