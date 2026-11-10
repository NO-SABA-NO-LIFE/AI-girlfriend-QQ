from __future__ import annotations

import re
from typing import Any

from .message_parser import extract_plain_text


# 身份/称呼关键词：群里提到这些，等于在说机器人本人，需要结合上下文回应。
SMART_IDENTITY_KEYWORDS = (
    "机器人",
    "人工智能",
    "萝卜子",
    "高性能",
)

# 明确要机器人做事的指令词；这些话本身就是对机器人说的。
SMART_GROUP_TRIGGERS = (
    "哼哒",
    "给我忘掉",
    "不准涩涩",
    "涩涩",
    "帮我看看",
    "帮忙看看",
    "分析一下",
    "分析这个",
    "总结一下",
    "总结这个",
    "评价一下",
    "评价这个",
    "锐评",
    "解读一下",
    "识图",
    "看看这个",
    "看看这张",
    "看这个视频",
    "抽象",
)

# 英文 “ai” 只在作为独立单词时才算；前面不能是 @、.、/ 或单词字符，
# 避免误伤 email（test@ai.com）、网址（x.ai）和 aim 等。
_AI_EN_RE = re.compile(r"(?<![@./\w])ai\b", re.IGNORECASE)


def is_bot_mentioned(event: dict[str, Any], bot_qq: int, plain_text: str) -> bool:
    message = event.get("message")
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            qq = str((segment.get("data") or {}).get("qq", ""))
            if qq in {str(bot_qq), "all"}:
                return True

    lowered = plain_text.lower()
    return f"@{bot_qq}" in plain_text or "亚托莉" in plain_text or "atri" in lowered


def _at_segment_targets(
    event: dict[str, Any], bot_qq: int
) -> tuple[bool, bool]:
    """读取消息里的 at 段，返回 (是否@机器人, 是否@其他成员)。

    @全体成员（all）算作 @机器人；at 段的判断比纯文本兜底更权威，
    用来区分“@机器人”和“@别人”。
    """
    at_bot = False
    at_other = False
    message = event.get("message")
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            qq = str((segment.get("data") or {}).get("qq", ""))
            if qq == "all" or qq == str(bot_qq):
                at_bot = True
            elif qq:
                at_other = True
    return at_bot, at_other


def _has_reply_segment(event: dict[str, Any]) -> bool:
    """消息里是否带引用回复（reply 段）。"""
    message = event.get("message")
    if not isinstance(message, list):
        return False
    return any(
        isinstance(segment, dict)
        and str(segment.get("type") or "").lower() == "reply"
        for segment in message
    )


def _quoted_sender_is_bot(event: dict[str, Any], bot_qq: int) -> bool | None:
    """判断被引用消息的发送者是不是机器人。

    NapCat 通常在事件顶层附带 ``reply`` 对象（含 sender）。无法确认时返回
    None，而不是猜测，由调用方按“引用是强互动信号”保守处理。
    """
    reply = event.get("reply")
    if not isinstance(reply, dict):
        return None
    sender = reply.get("sender")
    if not isinstance(sender, dict) or sender.get("user_id") in (None, ""):
        return None
    return str(sender.get("user_id")) == str(bot_qq)


def should_reply(
    event: dict[str, Any],
    bot_qq: int,
    reply_mode: str,
    owner_qqs: tuple[int, ...] = (),
) -> bool:
    if event.get("post_type") != "message":
        return False

    if _as_int(event.get("self_id")) not in {None, bot_qq}:
        return False

    if _as_int(event.get("user_id")) == bot_qq:
        return False

    message_type = event.get("message_type")
    if message_type == "private":
        return reply_mode in {"private", "mention", "smart", "all"}

    if message_type != "group":
        return False

    if reply_mode == "all":
        return True
    plain_text = extract_plain_text(event.get("message"))
    if reply_mode == "mention":
        at_bot, at_other = _at_segment_targets(event, bot_qq)
        if at_other and not at_bot:
            return False
        return is_bot_mentioned(event, bot_qq, plain_text)
    if reply_mode == "smart":
        return _should_reply_smart_group(event, bot_qq, plain_text, owner_qqs)
    return False


def _should_reply_smart_group(
    event: dict[str, Any],
    bot_qq: int,
    plain_text: str,
    owner_qqs: tuple[int, ...] = (),
) -> bool:
    at_bot, at_other = _at_segment_targets(event, bot_qq)
    # 最高优先级：消息明确 @ 了其他成员、却没 @ 机器人，说明这话是对别人
    # 说的，即使含触发词或机器人名字也不抢答。
    if at_other and not at_bot:
        return False

    # 1) 明确 @ 机器人。
    if at_bot:
        return True

    # 2) 引用/回复：只在能确认引用的是机器人时触发。进入这里前已尽力解析
    #    被引用者（event.reply 或 get_msg）；若仍无法确认，默认不抢话，避免
    #    把别人之间的引用对话误当成对自己说话。
    if _has_reply_segment(event):
        return _quoted_sender_is_bot(event, bot_qq) is True

    # 3) 称呼/身份关键词：亚托莉、atri（is_bot_mentioned 文本兜底）、
    #    机器人、人工智能、萝卜子、高性能，以及独立单词 ai。
    if is_bot_mentioned(event, bot_qq, plain_text):
        return True
    if _AI_EN_RE.search(plain_text):
        return True
    if any(word in plain_text for word in SMART_IDENTITY_KEYWORDS):
        return True

    # 4) 明确要机器人做事的指令词。
    return any(trigger in plain_text for trigger in SMART_GROUP_TRIGGERS)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# “戳一下/戳一戳/戳他/戳她”等戳人指令。
_POKE_COMMAND_RE = re.compile(r"戳(?:一?[下戳]|[他她它])")


def poke_command_target(
    event: dict[str, Any], bot_qq: int
) -> str | None:
    """识别对机器人下达的“戳一下 @某人”指令，返回目标 QQ；否则返回 None。

    fail-closed：群聊里只有明确 @ 机器人（或文本点名“亚托莉/atri”）才算指令，
    仅 @ 别人、没 @ 机器人的消息是群友之间的对话，不触发。目标取“戳一下”
    动词之后出现的第一个有效 @，跳过动词之前作为呼语的 @，避免戳错对象。
    """
    message = event.get("message")
    if not isinstance(message, list):
        return None
    plain = extract_plain_text(message)
    if not _POKE_COMMAND_RE.search(plain):
        return None

    if event.get("message_type") != "private":
        at_bot, _at_other = _at_segment_targets(event, bot_qq)
        # 群聊必须明确呼叫机器人；只 @ 别人、没 @ 机器人也没点名时不触发。
        if not (at_bot or "亚托莉" in plain or "atri" in plain.lower()):
            return None

    # 按段顺序：先看到“戳一下”动词，再取其后第一个有效 @ 作为目标。
    seen_verb = False
    for segment in message:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "")
        if seg_type == "text":
            text = str((segment.get("data") or {}).get("text") or "")
            if _POKE_COMMAND_RE.search(text):
                seen_verb = True
        elif seg_type == "at" and seen_verb:
            qq = str((segment.get("data") or {}).get("qq") or "")
            if qq and qq != "all" and qq != str(bot_qq):
                return qq
    return None
