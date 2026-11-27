import asyncio
import json
import time
from pathlib import Path

from atri_qq_bot.config import BotConfig
from atri_qq_bot.group_reply_focus import (
    END_CONVERSATION_SUPPRESS_SECONDS,
    GroupReplyFocus,
    GroupReplyFocusStore,
    _looks_like_end_conversation,
    relative_time_label,
)
from atri_qq_bot.onebot.server import OneBotServer
from atri_qq_bot.persona import AtriReplyEngine


BOT_QQ = 100000001
GROUP_ID = 900001002


def _group_event(user_id: int, *segments: dict) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": GROUP_ID,
        "user_id": user_id,
        "message": list(segments),
    }


def _text(value: str) -> dict:
    return {"type": "text", "data": {"text": value}}


def _at_bot() -> dict:
    return {"type": "at", "data": {"qq": str(BOT_QQ)}}


def test_bare_mention_prioritizes_triggering_members_latest_message() -> None:
    store = GroupReplyFocusStore(ttl_seconds=120)
    member_a = _group_event(10001, _text("A刚发的最新内容"))
    member_b = _group_event(10002, _text("B刚问了一个更长、更显眼的问题"))

    store.remember(member_a, "A刚发的最新内容", addressed_to_bot=False, now=1)
    store.remember(member_b, "B刚问了一个更长、更显眼的问题", addressed_to_bot=False, now=2)
    focus = store.resolve(
        _group_event(10001, _at_bot()),
        "@群友",
        nickname="A",
        addressed_to_bot=True,
        now=3,
    )

    assert focus is not None
    assert focus.actor_id == "10001"
    assert focus.focus_text == "A刚发的最新内容"
    assert focus.source == "previous_same_sender"
    assert "B刚问了一个更长" not in focus.prompt_context()


def test_share_is_bound_to_following_mention_from_same_member() -> None:
    store = GroupReplyFocusStore(ttl_seconds=120)
    share = _group_event(
        10001,
        {
            "type": "share",
            "data": {"title": "中华亚", "url": "https://example.test/share/1"},
        },
    )
    store.remember(
        share,
        "[分享:中华亚 https://example.test/china-atri]",
        addressed_to_bot=False,
        now=10,
    )

    focus = store.resolve(
        _group_event(10001, _at_bot()),
        "@群友",
        nickname="A",
        addressed_to_bot=True,
        now=12,
    )

    assert focus is not None
    assert focus.focus_text.startswith("[分享:中华亚")
    assert [segment["type"] for segment in focus.analysis_event["message"]] == [
        "share",
        "at",
    ]


def test_substantive_current_message_wins_over_pending_message() -> None:
    store = GroupReplyFocusStore(ttl_seconds=120)
    store.remember(
        _group_event(10001, _text("旧话题")),
        "旧话题",
        addressed_to_bot=False,
        now=1,
    )

    current = _group_event(10001, _at_bot(), _text("今天吃什么？"))
    focus = store.resolve(
        current,
        "@群友 今天吃什么？",
        nickname="A",
        addressed_to_bot=True,
        now=2,
    )

    assert focus is not None
    assert focus.focus_text == "今天吃什么？"
    assert focus.source == "current_message"
    assert focus.analysis_event is current


def test_mention_opens_one_minute_same_member_session() -> None:
    store = GroupReplyFocusStore()
    mention = _group_event(10001, _at_bot(), _text("你觉得这个怎么样？"))
    store.open_session(mention, nickname="A", now=10)

    # 开窗第一段 1 分钟：69s（开窗后 59s）仍在窗口内，命中续聊。
    assert store.is_active_continuation(
        _group_event(10001, _text("那你为什么这么想？")),
        "那你为什么这么想？",
        now=69,
    )
    assert not store.is_active_continuation(
        _group_event(10002, _text("那你为什么这么想？")),
        "那你为什么这么想？",
        now=69,
    )


def test_active_session_expires_and_does_not_intercept_other_mentions() -> None:
    store = GroupReplyFocusStore()
    mention = _group_event(10001, _at_bot(), _text("聊聊这个"))
    store.open_session(mention, nickname="A", now=10)

    assert not store.is_active_continuation(
        _group_event(
            10001,
            {"type": "at", "data": {"qq": "10002"}},
            _text("你看一下"),
        ),
        "@群友 你看一下",
        now=20,
    )
    # 开窗第一段 1 分钟，且 191s 已超过开窗起 180s 的硬上限，不再接话。
    assert not store.is_active_continuation(
        _group_event(10001, _text("你还在吗？")),
        "你还在吗？",
        now=191,
    )


def test_continuation_quoting_other_member_is_not_bot_dialogue() -> None:
    store = GroupReplyFocusStore()
    # 该成员此前和机器人有过对话，会话仍在有效期内。
    mention = _group_event(10001, _at_bot(), _text("在吗"))
    store.open_session(mention, nickname="A", now=10)

    # 新消息引用了别人、并 @ 别人，是在和别人说话，不能当成对机器人的续聊。
    quoting_other = _group_event(
        10001,
        {"type": "reply", "data": {"id": "555"}},
        {"type": "at", "data": {"qq": "10002"}},
        _text("把你的给我"),
    )
    quoting_other["reply"] = {"sender": {"user_id": 10002}}
    assert not store.is_active_continuation(
        quoting_other,
        "@群友 把你的给我",
        now=20,
    )

    # 对照：引用机器人时仍应触发续聊。
    quoting_bot = _group_event(
        10001,
        {"type": "reply", "data": {"id": "556"}},
        _text("那你说呢"),
    )
    quoting_bot["reply"] = {"sender": {"user_id": BOT_QQ}}
    assert store.is_active_continuation(
        quoting_bot,
        "那你说呢",
        now=21,
    )


def test_continuation_renewal_uses_uniform_one_minute_stages() -> None:
    store = GroupReplyFocusStore()
    store.open_session(
        _group_event(10001, _at_bot(), _text("在吗")), nickname="A", now=0
    )

    def session():
        return store._active[f"group:{GROUP_ID}:user:10001"]

    # 开窗第一段 1 分钟。
    assert session().stage == 0
    assert session().expires_at == 60.0

    # 每次续期都统一延长 1 分钟，不再递减。
    assert store.is_active_continuation(
        _group_event(10001, _text("所以呢")), "所以呢", now=50
    )
    assert session().stage == 1
    assert session().expires_at == 110.0  # 50 + 60

    assert store.is_active_continuation(
        _group_event(10001, _text("然后呢")), "然后呢", now=100
    )
    assert session().stage == 2
    assert session().expires_at == 160.0  # 100 + 60

    # 第 3 次续期：150 + 60 = 210，被开窗起 180s 的硬上限钳制。
    assert store.is_active_continuation(
        _group_event(10001, _text("嗯")), "嗯", now=150
    )
    assert session().stage == 3
    assert session().expires_at == 180.0


def test_renewal_never_exceeds_three_minute_hard_cap() -> None:
    store = GroupReplyFocusStore()
    store.open_session(
        _group_event(10001, _at_bot(), _text("在吗")), nickname="A", now=0
    )

    # 每段都在过期前续期，持续走到 3 分钟硬上限。
    for now in (50, 100, 150):
        assert store.is_active_continuation(
            _group_event(10001, _text("嗯")), "嗯", now=now
        )

    sess = store._active[f"group:{GROUP_ID}:user:10001"]
    # 150 + 60 = 210，被开窗起 180s 的绝对硬上限钳制。
    assert sess.expires_at == 180.0

    # 到 180s 硬上限，会话结束、不再接话。
    assert not store.is_active_continuation(
        _group_event(10001, _text("嗯")), "嗯", now=180
    )
    assert f"group:{GROUP_ID}:user:10001" not in store._active


def test_custom_initial_session_ttl_is_honored() -> None:
    store = GroupReplyFocusStore(active_session_ttl_seconds=90)
    store.open_session(
        _group_event(10001, _at_bot(), _text("在吗")), nickname="A", now=0
    )
    sess = store._active[f"group:{GROUP_ID}:user:10001"]
    assert sess.expires_at == 90.0
    # 自定义初始 TTL 内仍可续期；硬上限仍以开窗起 180s 为准，不受初始 TTL 影响。
    assert store.is_active_continuation(
        _group_event(10001, _text("嗯")), "嗯", now=80
    )


def test_group_prompt_marks_same_sender_focus_as_mandatory(monkeypatch, tmp_path) -> None:
    config = BotConfig(
        bot_qq=BOT_QQ,
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
    engine = AtriReplyEngine(config)
    captured: dict[str, object] = {}

    async def fake_post(client, headers, payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "我先回应A刚才的分享。"}}]}

    monkeypatch.setattr(engine, "_post_chat_completion", fake_post)
    focus = GroupReplyFocus(
        actor_id="10001",
        nickname="A",
        trigger_text="@群友",
        focus_text="[分享:中华亚 https://example.test/china-atri]",
        source="previous_same_sender",
        analysis_event=_group_event(10001, _at_bot()),
    )

    reply = asyncio.run(
        engine._reply_with_api(
            f"group:{GROUP_ID}",
            "@群友",
            "A",
            profile_id=f"group:{GROUP_ID}:user:10001",
            profile={},
            context_profile={},
            reply_focus=focus,
            allow_reply_voice=False,
        )
    )

    contents = [str(message.get("content") or "") for message in captured["messages"]]
    focus_prompt = next(content for content in contents if "本轮强制回复焦点" in content)
    assert reply == "我先回应A刚才的分享。"
    assert "回复对象：A" in focus_prompt
    assert "[分享:中华亚" in focus_prompt
    assert "其他群友消息只能作为背景" in focus_prompt


def test_group_reply_injects_current_speaker_boundary(monkeypatch, tmp_path) -> None:
    config = BotConfig(
        bot_qq=BOT_QQ,
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
    engine = AtriReplyEngine(config)
    captured: dict[str, object] = {}

    async def fake_post(client, headers, payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "大乱斗我肯定赢。"}}]}

    monkeypatch.setattr(engine, "_post_chat_completion", fake_post)

    reply = asyncio.run(
        engine._reply_with_api(
            f"group:{GROUP_ID}",
            "依旧机器人大乱斗",
            "友川纪夫",
            profile_id=f"group:{GROUP_ID}:user:10002",
            allow_reply_voice=False,
        )
    )

    messages = captured["messages"]
    contents = [str(m.get("content") or "") for m in messages]
    # 边界提示点名当前说话人，且紧贴最后一条 user 消息。
    boundary_idx = next(
        i for i, c in enumerate(contents) if "当前说话人“友川纪夫”" in c
    )
    assert boundary_idx == len(messages) - 2
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "友川纪夫#10002：依旧机器人大乱斗"
    assert reply == "大乱斗我肯定赢。"


def test_group_poke_textless_turn_warns_history_belongs_to_others(
    monkeypatch, tmp_path
) -> None:
    config = BotConfig(
        bot_qq=BOT_QQ,
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
    engine = AtriReplyEngine(config)
    captured: dict[str, object] = {}

    async def fake_post(client, headers, payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "戳我干嘛啦，无聊。"}}]}

    monkeypatch.setattr(engine, "_post_chat_completion", fake_post)

    # 戳一戳无文字：服务器输入为占位符 + 回戳反馈。
    poke_input = "[用户未附加文字]\n\n【系统动作反馈】你刚刚已经回戳了对方一下。"
    reply = asyncio.run(
        engine._reply_with_api(
            f"group:{GROUP_ID}",
            poke_input,
            "潇潇",
            profile_id=f"group:{GROUP_ID}:user:10003",
            allow_reply_voice=False,
        )
    )

    messages = captured["messages"]
    contents = [str(m.get("content") or "") for m in messages]
    boundary = next(c for c in contents if "当前说话人“潇潇”" in c)
    # 必须告知模型潇潇本轮没发文字，并按 QQ 号锚点区分本人（可多条）/他人的历史。
    assert "没有发送文字" in boundary
    assert "消息有很多条" in boundary
    assert "其他群友" in boundary
    assert reply == "戳我干嘛啦，无聊。"


def test_remember_anchors_in_memory_history_with_qq(monkeypatch, tmp_path) -> None:
    config = BotConfig(
        bot_qq=BOT_QQ,
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
    engine = AtriReplyEngine(config)
    engine._remember(
        f"group:{GROUP_ID}",
        "依旧机器人大乱斗",
        "大乱斗我赢",
        "友川纪夫",
        actor_id=10002,
    )
    history = engine._history[f"group:{GROUP_ID}"]
    user_content = next(m["content"] for m in history if m["role"] == "user")
    # 注入到 line 322 的内存历史必须带 QQ 锚点，不能再是无锚点的平行副本。
    assert user_content == "友川纪夫#10002：依旧机器人大乱斗"


def test_server_routes_previous_share_and_focus_to_same_reply(monkeypatch, tmp_path) -> None:
    config = BotConfig(
        bot_qq=BOT_QQ,
        host="127.0.0.1",
        port=8765,
        reply_mode="mention",
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_model="test-model",
        temperature=0.4,
        max_tokens=350,
        memory_path=tmp_path / "memory.json",
        voice_tts_enabled=False,
    )
    engine = AtriReplyEngine(config)
    server = OneBotServer(config, engine)
    server._event_log = tmp_path / "onebot-events.log"
    server._reply_event_log = tmp_path / "reply-events.jsonl"
    server._voice_event_log = tmp_path / "voice-events.log"
    captured: dict[str, object] = {}

    async def fake_analyze(event, plain_text, call_action, *, allow_vision=True):
        del plain_text, call_action, allow_vision
        captured["analysis_event"] = event
        return None

    async def fake_reply(*args, **kwargs):
        captured["reply_focus"] = kwargs.get("reply_focus")
        return "收到"

    async def fake_send_reply(*args, **kwargs):
        return "收到"

    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)

    share = _group_event(
        10001,
        {
            "type": "share",
            "data": {"title": "中华亚", "url": "https://example.test/share/2"},
        },
    )
    share.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    mention = _group_event(10001, _at_bot())
    mention.update({"self_id": BOT_QQ, "sender": {"card": "A"}})

    asyncio.run(server._handle_event(object(), share))
    asyncio.run(server._handle_event(object(), mention))

    focus = captured["reply_focus"]
    analysis_event = captured["analysis_event"]
    assert isinstance(focus, GroupReplyFocus)
    assert focus.focus_text.startswith("[分享:中华亚")
    assert [segment["type"] for segment in analysis_event["message"]] == [
        "share",
        "at",
    ]


# --- 触发前被引用者解析（_ensure_quoted_sender_id）---

def _bare_server():
    import sys as _sys

    server = object.__new__(OneBotServer)
    return server


def test_ensure_quoted_sender_uses_inline_reply_without_get_msg() -> None:
    server = _bare_server()

    async def boom(target, action, params, timeout):
        raise AssertionError("已有顶层 reply，不应调用 get_msg")

    server.call_action_and_wait = boom
    event = _group_event(
        10001,
        {"type": "reply", "data": {"id": "555"}},
        _text("在吗"),
    )
    event["reply"] = {"sender": {"user_id": 10002}}
    result = asyncio.run(server._ensure_quoted_sender_id(object(), event))
    assert result == "10002"


def test_ensure_quoted_sender_fetches_and_backfills_via_get_msg() -> None:
    server = _bare_server()
    fetched: list[dict] = []

    async def fake_call(target, action, params, timeout):
        assert action == "get_msg"
        fetched.append(dict(params))
        return {
            "data": {
                "sender": {"user_id": 10002},
                "message": [_text("原本的话")],
            }
        }

    server.call_action_and_wait = fake_call
    event = _group_event(
        10001,
        {"type": "reply", "data": {"id": "777"}},
        _text("叫我干嘛"),
    )
    result = asyncio.run(server._ensure_quoted_sender_id(object(), event))
    assert result == "10002"
    assert fetched == [{"message_id": 777}]
    # 回填后现有判断函数能读到确定的 sender。
    assert event["reply"]["sender"]["user_id"] == 10002


def test_ensure_quoted_sender_returns_none_when_get_msg_fails() -> None:
    server = _bare_server()

    async def fake_call(target, action, params, timeout):
        raise RuntimeError("napcat unavailable")

    server.call_action_and_wait = fake_call
    event = _group_event(
        10001,
        {"type": "reply", "data": {"id": "777"}},
        _text("叫我干嘛"),
    )
    result = asyncio.run(server._ensure_quoted_sender_id(object(), event))
    assert result is None
    assert "reply" not in event


# --- 戳一戳动作已完成、但配套文字生成超时的兜底话术 ---

def _fallback_server(tmp_path, reply_mode: str = "mention"):
    config = BotConfig(
        bot_qq=BOT_QQ,
        host="127.0.0.1",
        port=8765,
        reply_mode=reply_mode,
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_model="test-model",
        temperature=0.4,
        max_tokens=350,
        memory_path=tmp_path / "memory.json",
        voice_tts_enabled=False,
    )
    engine = AtriReplyEngine(config)
    server = OneBotServer(config, engine)
    server._event_log = tmp_path / "onebot-events.log"
    server._reply_event_log = tmp_path / "reply-events.jsonl"
    server._voice_event_log = tmp_path / "voice-events.log"
    return engine, server


def test_onebot_records_ordinary_group_message_with_one_replace(monkeypatch, tmp_path):
    engine, server = _fallback_server(tmp_path)
    monkeypatch.setattr(engine.memory._memory_worker, "enabled", False)
    path = engine.memory.path
    replacements = []
    replace = Path.replace

    def count_replace(source, target):
        result = replace(source, target)
        if result == path:
            replacements.append(result)
        return result

    monkeypatch.setattr(Path, "replace", count_replace)
    event = _group_event(10001, _text("我到家了"))
    asyncio.run(server._handle_event(object(), event))

    assert replacements == [path]
    conversations = json.loads(path.read_bytes())["conversations"]
    group = conversations[f"group:{GROUP_ID}"]
    member = conversations[f"group:{GROUP_ID}:user:10001"]
    assert group["target"] == {"message_type": "group", "group_id": GROUP_ID}
    assert member["target"] == {
        "message_type": "group", "group_id": GROUP_ID, "user_id": 10001,
    }
    assert group["history"][-1]["text"] == member["history"][-1]["text"] == "我到家了"

    replacements.clear()
    command = _group_event(10002, _text("/affection"))
    asyncio.run(server._handle_event(object(), command))
    assert replacements == [path]
    conversations = json.loads(path.read_bytes())["conversations"]
    assert conversations[f"group:{GROUP_ID}"]["message_count"] == 1
    assert conversations[f"group:{GROUP_ID}:user:10002"] == {
        "target": {"message_type": "group", "group_id": GROUP_ID, "user_id": 10002},
    }
    assert "private:10002" not in conversations


def test_poke_command_timeout_sends_poke_done_fallback(monkeypatch, tmp_path) -> None:
    from atri_qq_bot.onebot.server_parts import server_events

    engine, server = _fallback_server(tmp_path)

    async def fake_send_group_poke(*args, **kwargs):
        return True

    async def fake_reply(*args, **kwargs):
        raise RuntimeError("ReadTimeout")

    async def fake_analyze(event, plain_text, call_action, *, allow_vision=True):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(server, "_send_group_poke", fake_send_group_poke)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)

    event = _group_event(
        10001,
        _at_bot(),
        _text(" 戳一下 "),
        {"type": "at", "data": {"qq": "10002"}},
    )
    event.update({"self_id": BOT_QQ, "sender": {"card": "A"}})

    asyncio.run(server._handle_event(object(), event))

    assert len(sent) == 1
    assert sent[0] in server_events._POKE_DONE_FALLBACK_REPLIES


def test_non_poke_timeout_still_sends_generic_fallback(monkeypatch, tmp_path) -> None:
    engine, server = _fallback_server(tmp_path)

    async def fake_reply(*args, **kwargs):
        raise RuntimeError("ReadTimeout")

    async def fake_analyze(event, plain_text, call_action, *, allow_vision=True):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)

    event = _group_event(10001, _at_bot(), _text("你好呀"))
    event.update({"self_id": BOT_QQ, "sender": {"card": "A"}})

    asyncio.run(server._handle_event(object(), event))

    assert sent == ["唔……这轮我想太久，脑袋卡了一下。你再说一遍？"]


def test_looks_like_end_conversation_detects_explicit_cues() -> None:
    # 明确终止表达命中。
    assert _looks_like_end_conversation("不聊了")
    assert _looks_like_end_conversation("我先去忙了")
    assert _looks_like_end_conversation("好，到此为止吧")
    assert _looks_like_end_conversation("你别说了")
    # 正常对话、玩笑或含“不聊”但不是终止的句子不误判。
    assert not _looks_like_end_conversation("你吃饭了吗")
    assert not _looks_like_end_conversation("不聊八卦的话我们聊点别的")
    assert not _looks_like_end_conversation("")


def test_end_conversation_closes_session_and_replies_without_model(
    monkeypatch, tmp_path
) -> None:
    from atri_qq_bot.onebot.server_parts import server_events

    engine, server = _fallback_server(tmp_path)
    # 先手动打开一个续聊会话，模拟此前被 @ 过。
    opener = _group_event(10001, _at_bot(), _text("在吗"))
    server._group_reply_focus.open_session(opener, nickname="A", now=100)
    key = f"group:{GROUP_ID}:user:10001"
    assert key in server._group_reply_focus._active

    model_calls: list[bool] = []

    async def fake_reply(*args, **kwargs):
        model_calls.append(True)
        return "不该出现"

    async def fake_analyze(*args, **kwargs):
        model_calls.append(True)
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)

    event = _group_event(10001, _text("不聊了"))
    event.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), event))

    # 回一句确定性俏皮话，且完全没调模型/工具分析。
    assert len(sent) == 1
    assert sent[0] in server_events._END_CONVERSATION_FALLBACK_REPLIES
    assert model_calls == []
    # 会话已关闭：之后该成员的消息需要重新 @ 才会回。
    assert key not in server._group_reply_focus._active


def test_end_conversation_without_active_session_still_replies(
    monkeypatch, tmp_path
) -> None:
    from atri_qq_bot.onebot.server_parts import server_events

    engine, server = _fallback_server(tmp_path)

    async def fake_reply(*args, **kwargs):
        raise AssertionError("不应调用模型")

    async def fake_analyze(*args, **kwargs):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)

    # 没有活跃会话、也没 @，明确说“不聊了”仍回一句再收。
    event = _group_event(10001, _text("我去忙了，别再说了"))
    event.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), event))

    assert len(sent) == 1
    assert sent[0] in server_events._END_CONVERSATION_FALLBACK_REPLIES


def test_suppression_store_records_and_expires() -> None:
    store = GroupReplyFocusStore(bot_qq=BOT_QQ)
    event = _group_event(10001, _text("不聊了"))

    assert not store.is_suppressed(event, now=100)
    store.suppress_after_end(event, now=100)
    # 休眠期内命中。
    assert store.is_suppressed(event, now=100 + END_CONVERSATION_SUPPRESS_SECONDS - 1)
    # 到点自动解除并清理。
    assert not store.is_suppressed(event, now=100 + END_CONVERSATION_SUPPRESS_SECONDS)
    assert store._suppressed == {}


def test_suppressed_member_only_replies_to_mention_or_quote(
    monkeypatch, tmp_path
) -> None:
    from atri_qq_bot.onebot.server_parts import server_events

    engine, server = _fallback_server(tmp_path, reply_mode="smart")

    async def fake_reply(*args, **kwargs):
        return "模型回复"

    async def fake_analyze(*args, **kwargs):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        # 正常流程传入 OutgoingMessage 列表；降级分支传入字符串。统一提取文本。
        if isinstance(messages, str):
            sent.append(messages)
            return
        for message in messages:
            if getattr(message, "kind", "") == "text":
                sent.append(str(message.content))

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)

    # 1) 明确说“不聊了”：回一句俏皮话并进入休眠。
    end = _group_event(10001, _at_bot(), _text("不聊了"))
    end.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), end))
    assert len(sent) == 1
    assert sent[0] in server_events._END_CONVERSATION_FALLBACK_REPLIES
    assert server._group_reply_focus.is_suppressed(end)

    # 2) 休眠期内只发身份关键词（不带 @）：不回。
    keyword = _group_event(10001, _text("亚托莉，高性能机器人"))
    keyword.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), keyword))
    assert len(sent) == 1

    # 3) 休眠期内普通消息：不回，随机搭话也被压制。
    normal = _group_event(10001, _text("今天天气真不错"))
    normal.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), normal))
    assert len(sent) == 1

    # 4) 休眠期内 @ 机器人：回。
    mention = _group_event(10001, _at_bot(), _text("在吗"))
    mention.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), mention))
    assert len(sent) == 2
    assert sent[1].startswith("模型回复")

    # 5) 休眠期内引用机器人：回。
    quote = _group_event(
        10001,
        {"type": "reply", "data": {"id": "909"}},
        _text("那这个呢"),
    )
    quote["reply"] = {"sender": {"user_id": BOT_QQ}}
    quote.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), quote))
    assert len(sent) == 3
    assert sent[2].startswith("模型回复")


def test_repeated_mention_keeps_first_opened_at_and_hard_cap() -> None:
    store = GroupReplyFocusStore()
    key = f"group:{GROUP_ID}:user:10001"
    store.open_session(
        _group_event(10001, _at_bot(), _text("在吗")), nickname="A", now=0
    )
    assert store._active[key].opened_at == 0

    # 100 秒后再次 @：硬上限不重置，只延长本段且不突破 180。
    store.open_session(
        _group_event(10001, _at_bot(), _text("继续")), nickname="A", now=100
    )
    sess = store._active[key]
    assert sess.opened_at == 0
    # 100 + 60 = 160，未触及 180 硬上限，本段仅顺延 60 秒。
    assert sess.expires_at == 160.0


def test_open_after_hard_cap_suppresses_instead_of_new_window() -> None:
    store = GroupReplyFocusStore()
    key = f"group:{GROUP_ID}:user:10001"
    store.open_session(
        _group_event(10001, _at_bot(), _text("在吗")), nickname="A", now=0
    )
    late = _group_event(10001, _at_bot(), _text("还在吗"))
    store.open_session(late, nickname="A", now=180)
    assert key not in store._active
    assert store.is_suppressed(late, now=180)
    assert store._suppressed[key] == 180 + END_CONVERSATION_SUPPRESS_SECONDS


def test_continuation_hard_cap_auto_suppresses() -> None:
    store = GroupReplyFocusStore()
    store.open_session(
        _group_event(10001, _at_bot(), _text("在吗")), nickname="A", now=0
    )
    event = _group_event(10001, _text("嗯"))
    assert not store.is_active_continuation(event, "嗯", now=180)
    assert store.is_suppressed(event, now=180)


def test_idle_expiry_within_hard_cap_does_not_suppress() -> None:
    store = GroupReplyFocusStore()
    store.open_session(
        _group_event(10001, _at_bot(), _text("在吗")), nickname="A", now=0
    )
    event = _group_event(10001, _text("嗯"))
    assert not store.is_active_continuation(event, "嗯", now=70)
    assert not store.is_suppressed(event, now=70)


def test_hard_cap_suppression_blocks_ambient_in_same_message(
    monkeypatch, tmp_path
) -> None:
    engine, server = _fallback_server(tmp_path, reply_mode="smart")

    async def fake_reply(*args, **kwargs):
        return "模型回复"

    async def fake_analyze(*args, **kwargs):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        if isinstance(messages, str):
            sent.append(messages)
            return
        for message in messages:
            if getattr(message, "kind", "") == "text":
                sent.append(str(message.content))

    ambient_calls: list[str] = []

    async def fake_ambient(event, text):
        ambient_calls.append(text)
        return True

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)
    monkeypatch.setattr(server, "_maybe_ambient_group_reply", fake_ambient)

    # 让 180 秒硬上限恰好落在当前时刻：处理消息时即触发冷却，
    # 且冷却截止时间在真实未来，is_suppressed 复查才会返回 True。
    import time

    opener = _group_event(10001, _at_bot(), _text("在吗"))
    server._group_reply_focus.open_session(
        opener, nickname="A", now=time.time() - 180
    )

    normal = _group_event(10001, _text("今天天气真不错"))
    normal.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), normal))

    # 同条消息内 continuation 刚触发硬上限冷却，ambient 不得再被调用，也不发送。
    assert ambient_calls == []
    assert sent == []
    assert server._group_reply_focus.is_suppressed(normal)


def test_suppressed_member_message_is_recorded_but_not_sent(
    monkeypatch, tmp_path
) -> None:
    engine, server = _fallback_server(tmp_path, reply_mode="smart")

    async def fake_reply(*args, **kwargs):
        return "模型回复"

    async def fake_analyze(*args, **kwargs):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        if isinstance(messages, str):
            sent.append(messages)
            return
        for message in messages:
            if getattr(message, "kind", "") == "text":
                sent.append(str(message.content))

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)

    # 先进入冷却（明确说“不聊了”）。
    end = _group_event(10001, _at_bot(), _text("不聊了"))
    end.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), end))
    sent.clear()

    # 冷却期发一条普通消息：不 @、不引用。
    normal = _group_event(10001, _text("我刚和别人聊到周末计划"))
    normal.update({"self_id": BOT_QQ, "sender": {"card": "A"}})
    asyncio.run(server._handle_event(object(), normal))

    # 不回复……
    assert sent == []
    # ……但已写进成员维度历史，冷却结束后可被引用。
    member_id = f"group:{GROUP_ID}:user:10001"
    history = engine.memory.recent_history(member_id, limit=10)
    assert any(
        entry.get("role") == "user" and "周末计划" in entry.get("text", "")
        for entry in history
    )


# --- 活跃续聊：候选判断与续期分离，最终由模型上下文门决定 ---

def test_continuation_candidate_does_not_mutate_session() -> None:
    store = GroupReplyFocusStore()
    store.open_session(
        _group_event(10001, _at_bot(), _text("在吗")), nickname="A", now=10
    )
    session = store._active[f"group:{GROUP_ID}:user:10001"]
    before_stage, before_expires = session.stage, session.expires_at

    # 候选判断只返回 True/False，不滑动续期。
    assert store.continuation_candidate(
        _group_event(10001, _text("那你为什么这么想？")),
        "那你为什么这么想？",
        now=50,
    )
    assert session.stage == before_stage
    assert session.expires_at == before_expires

    # 显式续期后才进入下一段。
    assert store.renew_continuation(
        _group_event(10001, _text("那你为什么这么想？")), now=50
    )
    assert session.stage == before_stage + 1
    assert session.expires_at == 110.0


def test_active_continuation_gate_rejects_human_to_human(
    monkeypatch, tmp_path
) -> None:
    engine, server = _fallback_server(tmp_path, reply_mode="smart")

    async def fake_reply(*args, **kwargs):
        return "模型回复"

    async def fake_analyze(*args, **kwargs):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        if isinstance(messages, str):
            sent.append(messages)
            return
        for message in messages:
            if getattr(message, "kind", "") == "text":
                sent.append(str(message))

    gate_calls: list[tuple[str, str]] = []
    ambient_calls: list[str] = []

    async def fake_gate(plain_text, group_context, speaker_name=""):
        gate_calls.append((plain_text, group_context))
        return False  # 模型结合上下文判断：这是别人之间的对话，不接

    async def fake_ambient(event, text):
        # 无活跃会话的真人消息（小花那条）才走这里；目标消息由续聊门裁决，不应进入。
        ambient_calls.append(text)
        return False

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)
    monkeypatch.setattr(server, "_llm_ambient_gate_should_reply", fake_gate)
    monkeypatch.setattr(server, "_maybe_ambient_group_reply", fake_ambient)

    # 1) 小鱿鱼先 @ 机器人，打开活跃会话。
    opener = _group_event(10001, _at_bot(), _text("在吗"))
    opener.update({"self_id": BOT_QQ, "sender": {"card": "小鱿鱼"}})
    asyncio.run(server._handle_event(object(), opener))
    key = f"group:{GROUP_ID}:user:10001"
    assert key in server._group_reply_focus._active

    # 2) 另一个真人小花发言，成为群里最近一条。
    other = _group_event(10002, _text("行啊，小鱿鱼来聊，我在这儿。"))
    other.update({"self_id": BOT_QQ, "sender": {"card": "小花"}})
    asyncio.run(server._handle_event(object(), other))

    sent.clear()

    # 3) 模型门判断小鱿鱼在回小花、不是对机器人说：不抢答，也不续期。
    target = _group_event(10001, _text("你说话太规整了 谁教的"))
    target.update({"self_id": BOT_QQ, "sender": {"card": "小鱿鱼"}})
    asyncio.run(server._handle_event(object(), target))
    assert len(gate_calls) == 1
    # 目标消息由续聊门裁决，没有再走随机搭话（只有小花那条进入过 ambient）。
    assert "你说话太规整了" not in " ".join(ambient_calls)
    assert sent == []
    session = server._group_reply_focus._active.get(key)
    assert session is not None and session.stage == 0


def test_active_continuation_gate_accepts_and_renews(
    monkeypatch, tmp_path
) -> None:
    engine, server = _fallback_server(tmp_path, reply_mode="smart")

    async def fake_reply(*args, **kwargs):
        return "模型回复"

    async def fake_analyze(*args, **kwargs):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        if isinstance(messages, str):
            sent.append(messages)
            return
        for message in messages:
            if getattr(message, "kind", "") == "text":
                sent.append(str(message))

    async def fake_gate(plain_text, group_context, speaker_name=""):
        return True  # 模型结合上下文判断：这句话确实在对机器人说，接

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)
    monkeypatch.setattr(server, "_llm_ambient_gate_should_reply", fake_gate)

    opener = _group_event(10001, _at_bot(), _text("在吗"))
    opener.update({"self_id": BOT_QQ, "sender": {"card": "小鱿鱼"}})
    asyncio.run(server._handle_event(object(), opener))
    key = f"group:{GROUP_ID}:user:10001"
    sent.clear()

    target = _group_event(10001, _text("那你觉得呢"))
    target.update({"self_id": BOT_QQ, "sender": {"card": "小鱿鱼"}})
    asyncio.run(server._handle_event(object(), target))

    # 模型判断接话：回复一条，且会话续期到下一段。
    assert len(sent) == 1
    assert server._group_reply_focus._active[key].stage == 1


def test_proactive_ambient_prompt_requires_addressee_resolution() -> None:
    focus = GroupReplyFocus(
        actor_id="10002",
        nickname="小鱿鱼",
        trigger_text="你说话太规整了 谁教的",
        focus_text="你说话太规整了 谁教的",
        source="proactive_ambient",
        analysis_event={},
    )
    prompt = focus.prompt_context()
    # 主动插话语境：先分清谁对谁说话，不把别人之间的问题当成问自己。
    assert "主动插话" in prompt
    assert "谁教的" in prompt
    assert "在对谁说话" in prompt
    # 不再使用"强制回复焦点 / 第一句必须先回应"那套直接应答的措辞。
    assert "强制回复焦点" not in prompt
    assert "第一句必须先回应" not in prompt


def test_proactive_ambient_focus_frames_chime_in(monkeypatch, tmp_path) -> None:
    engine, server = _fallback_server(tmp_path, reply_mode="smart")

    captured: dict = {}

    async def fake_reply(*args, **kwargs):
        captured["reply_focus"] = kwargs.get("reply_focus")
        return "模型回复"

    async def fake_analyze(*args, **kwargs):
        return None

    sent: list[str] = []

    gate_calls: list[dict] = []

    async def fake_gate(plain_text, group_context, speaker_name=""):
        gate_calls.append(
            {"text": plain_text, "context": group_context, "speaker": speaker_name}
        )
        return True  # 模型门判断这条值得主动接

    async def fake_send_reply(websocket, event, messages, **kwargs):
        # 与真实 send_reply 一致：返回已送达文本；返回空串才代表"未确认送达"。
        if isinstance(messages, str):
            sent.append(messages)
            return messages
        for message in messages:
            if getattr(message, "kind", "") == "text":
                sent.append(str(message))
        return "\n".join(sent)

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)
    monkeypatch.setattr(server, "_llm_ambient_gate_should_reply", fake_gate)

    # 真人之间在聊天，没有人 @ 机器人；模型门判断值得接，于是主动插话。
    event = _group_event(10002, _text("你说话太规整了 谁教的"))
    event.update({"self_id": BOT_QQ, "sender": {"card": "小鱿鱼"}})
    asyncio.run(server._handle_event(object(), event))

    assert len(gate_calls) == 1
    # 门里的当前消息也带上了发言人昵称。
    assert gate_calls[0]["speaker"] == "小鱿鱼"
    focus = captured["reply_focus"]
    assert focus is not None
    assert focus.source == "proactive_ambient"
    assert "强制回复焦点" not in focus.prompt_context()
    assert len(sent) == 1
    # 真正发出之后才记冷却；认领同时清空。
    assert server._last_ambient_group_reply_at.get(str(GROUP_ID))
    assert getattr(server, "_ambient_group_not_before", {}) == {}


def test_ambient_blocked_downstream_does_not_consume_cooldown(monkeypatch, tmp_path) -> None:
    """模型门通过、但被去重拦下：不发消息、不记 120 秒冷却，只留短暂退避。"""
    import time as _time

    engine, server = _fallback_server(tmp_path, reply_mode="smart")

    async def fake_reply(*args, **kwargs):
        return "模型回复"

    async def fake_analyze(*args, **kwargs):
        return None

    sent: list[str] = []

    async def fake_send_reply(websocket, event, messages, **kwargs):
        if isinstance(messages, str):
            sent.append(messages)
            return
        for message in messages:
            if getattr(message, "kind", "") == "text":
                sent.append(str(message))

    async def fake_gate(plain_text, group_context, speaker_name=""):
        return True  # 模型门判断这条值得接

    monkeypatch.setattr(engine, "reply", fake_reply)
    monkeypatch.setattr(server.tools, "analyze", fake_analyze)
    monkeypatch.setattr(server, "send_reply", fake_send_reply)
    monkeypatch.setattr(server, "_llm_ambient_gate_should_reply", fake_gate)
    # 强制下游去重拦下：这是"门通过但没发出"的最短路径。
    monkeypatch.setattr(
        server, "_duplicate_group_content", lambda event, conversation_id, text: True
    )

    event = _group_event(10002, _text("你说话太规整了 谁教的"))
    event.update({"self_id": BOT_QQ, "sender": {"card": "小鱿鱼"}})
    asyncio.run(server._handle_event(object(), event))

    assert sent == []
    assert server._last_ambient_group_reply_at == {}
    not_before = server._ambient_group_not_before[str(GROUP_ID)]
    assert 0 < not_before - _time.time() <= 31


def test_relative_time_label_boundaries() -> None:
    now = 1_000_000.0
    assert relative_time_label(None, now) == ""
    assert relative_time_label("not-a-number", now) == ""
    assert relative_time_label(now - 30, now) == ""        # 一分钟内不显示
    assert relative_time_label(now + 10, now) == ""        # 时钟偏移/未来时间
    assert relative_time_label(now - 90, now) == "1分钟前"
    assert relative_time_label(now - 3 * 3600, now) == "3小时前"
    assert relative_time_label(now - 25 * 3600, now) == "一天前"
    assert relative_time_label(now - 50 * 3600, now) == "2天前"


def test_history_context_carries_relative_time(monkeypatch, tmp_path) -> None:
    engine, server = _fallback_server(tmp_path, reply_mode="smart")
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)  # 冻结时钟，便于确定性断言

    # 群聊：一条一天前、一条一分钟内。
    group_conv = f"group:{GROUP_ID}"
    gitem = engine.memory._conversation(group_conv, save=False)
    gitem["history"] = [
        {"at": now - 25 * 3600, "role": "user", "text": "一天前的话",
         "nickname": "小花", "actor_id": "10002"},
        {"at": now - 30, "role": "user", "text": "刚刚的话",
         "nickname": "小鱿鱼", "actor_id": "10001"},
    ]
    group_ctx = engine._recent_conversation_context(group_conv)
    assert "[一天前] 小花#10002: 一天前的话" in group_ctx
    assert "小鱿鱼#10001: 刚刚的话" in group_ctx
    assert "[一天前]" not in group_ctx.split("刚刚的话", 1)[1]

    group_msgs = engine._recent_conversation_messages(group_conv)
    contents = [m["content"] for m in group_msgs]
    assert any(c.startswith("[一天前]") for c in contents)
    assert all(not c.startswith("[") for c in contents if "刚刚的话" in c)

    # 接话门上下文同样带标签与 QQ 锚点。
    gate_event = _group_event(10001, _text("触发"))
    gate_ctx = server._format_ambient_group_context(gate_event)
    assert "[一天前]" in gate_ctx
    assert "小花#10002" in gate_ctx
    assert "小鱿鱼#10001" in gate_ctx

    # 私聊：三小时前的用户消息也能识别新旧。
    private_conv = "private:10001"
    pitem = engine.memory._conversation(private_conv, save=False)
    pitem["history"] = [
        {"at": now - 3 * 3600, "role": "user", "text": "三小时前私聊"},
        {"at": now - 30, "role": "assistant", "text": "近期回复"},
    ]
    private_ctx = engine._recent_conversation_context(private_conv)
    assert "[3小时前] 用户: 三小时前私聊" in private_ctx
    assert "亚托莉: 近期回复" in private_ctx
