import asyncio

from atri_qq_bot.config import BotConfig
from atri_qq_bot.group_reply_focus import GroupReplyFocus, GroupReplyFocusStore
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

def _fallback_server(tmp_path):
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
    return engine, server


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
