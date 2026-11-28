import asyncio
import json
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from atri_qq_bot.config import BotConfig, load_config
from atri_qq_bot.message_plan import OutgoingMessage
from atri_qq_bot.onebot import server as onebot
from atri_qq_bot.onebot.reply_budget import ReplyBudget, reply_phase
from atri_qq_bot.onebot.visual_followup import VisualFollowupCache
from atri_qq_bot.persona import AtriReplyEngine
from atri_qq_bot.runtime.media_observability import log_attribution
from atri_qq_bot.toolbox_parts.analysis_result import ToolAnalysisResult
from atri_qq_bot.toolbox_parts.visual_origin import (
    ImageOrigin, image_origin_from_segment, is_visual_segment, stamp_visual_segments,
)


def test_material_worker_keeps_waiting_after_text_and_preserves_image_ids(monkeypatch):
    monkeypatch.setattr(onebot, "MESSAGE_DEBOUNCE_SECONDS", 0.025)
    monkeypatch.setattr(onebot, "QUEUE_IDLE_TIMEOUT_SECONDS", 0.03)
    server = object.__new__(onebot.OneBotServer)
    server.config = SimpleNamespace(
        image_attribution_enabled=True,
        material_bucket_enabled=True,
        material_bucket_private_seconds=0.2,
    )
    server._conversation_workers = {}
    captured = []

    async def capture(websocket, event):
        captured.append(event)

    server._handle_event = capture

    async def scenario():
        queue = asyncio.Queue()
        server._conversation_queues = {"private:1": queue}
        socket = object()

        def enqueue(message_id, kind, data):
            queue.put_nowait((socket, {
                "post_type": "message",
                "message_type": "private",
                "user_id": 1,
                "message_id": message_id,
                "message": [{"type": kind, "data": data}],
            }))

        enqueue(10, "text", {"text": "看这个"})
        worker = asyncio.create_task(server._conversation_worker("private:1"))
        await asyncio.sleep(0.035)
        enqueue(11, "text", {"text": "刚截的"})
        await asyncio.sleep(0.04)
        enqueue(12, "image", {"file": "pic"})
        await asyncio.wait_for(worker, 1)

    asyncio.run(scenario())
    assert len(captured) == 1
    event = captured[0]
    segments = event["message"]
    assert [item["data"]["text"] for item in segments if item["type"] == "text"] == [
        "看这个", "\n", "刚截的", "\n"
    ]
    image = image_origin_from_segment(segments[-1], index=len(segments) - 1)
    assert image.key == ("12", 0)


def test_followup_image_cache_selects_exact_image_and_isolates_scopes():
    cache = VisualFollowupCache()
    evidence = ToolAnalysisResult(category="image", style="direct", visual_data=b"first")
    images = [
        ImageOrigin("first", 0, "10", evidence_level="verified"),
        ImageOrigin("second", 0, "11", evidence_level="verified"),
    ]
    context = replace(
        evidence,
        image_origins=images,
        image_results=[evidence, replace(evidence, visual_data=b"second")],
    )
    cache.remember("group:1", context, now=100, max_bytes=8_000_000)
    assert cache.find("group:1", sender_id="10", message_id="second", now=105).result.visual_data == b"second"
    assert cache.find("group:1", sender_id="10", message_id="", now=105).result.visual_data == b"first"
    assert cache.find("group:1", sender_id="10", message_id="unknown", now=105) is None
    assert cache.find("group:2", sender_id="10", message_id="second", now=105) is None
    assert cache.find("group:1", sender_id="10", message_id="second", now=1001) is None


def test_turn_budget_switch_is_independent_and_budgets_are_validated():
    config = load_config(overrides={
        "TURN_BUDGET_ENABLED": "false",
        "IMAGE_ATTRIBUTION_ENABLED": "true",
    })
    assert not config.turn_budget_enabled and config.image_attribution_enabled
    with pytest.raises(ValueError, match="Reply budgets"):
        load_config(overrides={"TURN_BUDGET_SECONDS": "100", "TURN_MIN_CHAT_SECONDS": "30",
                               "TOOLBOX_VISION_STAGE_BUDGET_SECONDS": "90"})


def test_expired_analysis_cannot_consume_reserved_chat_time():
    async def scenario():
        start = asyncio.get_running_loop().time() - 0.1
        budget = ReplyBudget.create(start, 1, 0.05, 0.3)
        with pytest.raises(TimeoutError):
            async with reply_phase(budget.analysis_deadline):
                pytest.fail("expired analysis should not start work")
        async with reply_phase(budget.hard_deadline):
            await asyncio.sleep(0)

    asyncio.run(scenario())


def _media_server(tmp_path, **options):
    config = BotConfig(
        bot_qq=10001, host="127.0.0.1", port=8765, reply_mode=options.pop("reply_mode", "mention"),
        openai_api_key=None, openai_base_url="https://example.invalid/v1",
        openai_model="unused", temperature=0, max_tokens=100,
        memory_path=tmp_path / "users.json", sticker_dir=tmp_path / "stickers",
        sticker_capture_enabled=False, group_ambient_reply_enabled=False,
        memory_extraction_enabled=False, voice_tts_enabled=False, **options,
    )
    engine = AtriReplyEngine(config)
    server = onebot.OneBotServer(config, engine)
    server._event_log = tmp_path / "events.log"
    server._reply_event_log = tmp_path / "reply.jsonl"
    server._voice_event_log = tmp_path / "voice.log"
    return server, engine


@pytest.mark.parametrize(("message_type", "mismatch"), [
    ("group", {}),
    ("group", {"group_id": 20002}),
    ("group", {"message_id": 103}),
    ("group", {"self_id": 10003}),
    ("private", {}),
    ("private", {"peer_id": 10003}),
])
def test_quote_prefetch_and_analysis_share_context_validation(tmp_path, monkeypatch, message_type, mismatch):
    server, engine = _media_server(tmp_path)
    calls = []
    parsed = []
    response = {
        "message_type": message_type,
        "message_id": 101, "self_id": 10001,
        "sender": {"user_id": 10002},
        "message": [{"type": "image", "data": {"file": "quoted-image-fixture"}}],
        **({"group_id": 20001} if message_type == "group" else {"peer_id": 10002}),
        **mismatch,
    }

    async def get_message(_connection, action, params, _timeout):
        assert action == "get_msg"
        calls.append(params["message_id"])
        return {"status": "ok", "data": response}

    async def analyze(incoming, question, _call_action, **_kwargs):
        parsed.append((question, [seg for seg in incoming["message"] if is_visual_segment(seg)]))

    async def send(*_args, **_kwargs):
        return "收到"

    monkeypatch.setattr(server, "call_action_and_wait", get_message)
    monkeypatch.setattr(server.tools, "analyze", analyze)
    monkeypatch.setattr(server, "send_reply", send)
    monkeypatch.setattr(engine, "reply", send)
    event = {
        "post_type": "message", "message_type": message_type, "self_id": 10001,
        "user_id": 10002, "message_id": 102,
        **({"group_id": 20001} if message_type == "group" else {}),
        "message": [
            {"type": "reply", "data": {"id": "101"}},
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": "这张图是什么？"}},
        ],
    }
    asyncio.run(server._handle_event(object(), event))
    [(question, images)] = parsed
    if mismatch:
        assert images == []
        assert "引用消息原文暂时无法读取" in question
        assert "reply" not in event
    else:
        assert calls == [101]
        assert image_origin_from_segment(images[0], index=0).key == ("101", 0)


def test_quote_payload_with_only_top_level_id_is_validated(monkeypatch):
    server = object.__new__(onebot.OneBotServer)
    raw = {
        "message_type": "group", "group_id": 20001,
        "message": [{"type": "reply", "data": {}}],
    }
    inline = {
        "message_id": 101, "group_id": 20001,
        "sender": {"user_id": 10002}, "message": "合法引用",
    }

    async def missing_message(*_args):
        return {"status": "failed", "retcode": 1404}

    monkeypatch.setattr(server, "call_action_and_wait", missing_message)

    async def scenario():
        matching = {**raw, "reply": dict(inline)}
        assert await server._ensure_quoted_sender_id(object(), matching) == "10002"
        assert (await server._resolve_quoted_message(object(), matching))[1] == "合法引用"
        conflicting = {**raw, "reply": {**inline, "group_id": 20002}}
        assert await server._ensure_quoted_sender_id(object(), conflicting) is None
        assert "reply" not in conflicting
        assert not (await server._resolve_quoted_message(object(), conflicting))[2]

    asyncio.run(scenario())


@pytest.mark.parametrize("group_id", [20001, 20002])
def test_expired_analysis_still_checks_and_reads_inline_quote(tmp_path, monkeypatch, group_id):
    server, engine = _media_server(tmp_path, reply_mode="smart")
    captured = []

    def expired_analysis(_cls, started_at, *_args):
        return ReplyBudget(started_at, started_at + 2, started_at - 1)

    async def no_get_message(*_args):
        pytest.fail("expired analysis cannot call OneBot get_msg")

    async def reply(_conversation, question, *_args, **_options):
        captured.append(question)
        return "收到"

    async def send(*_args, **_options):
        return "收到"

    monkeypatch.setattr(ReplyBudget, "create", classmethod(expired_analysis))
    monkeypatch.setattr(server, "call_action_and_wait", no_get_message)
    monkeypatch.setattr(server, "send_reply", send)
    monkeypatch.setattr(engine, "reply", reply)
    event = {
        "post_type": "message", "message_type": "group", "self_id": 10001,
        "user_id": 10002, "group_id": 20001, "message_id": 102,
        "message": [
            {"type": "reply", "data": {"id": "101"}},
            {"type": "text", "data": {"text": "我刚看到"}},
        ],
        "reply": {
            "message_id": 101, "group_id": group_id,
            "sender": {"user_id": 10001}, "message": "当前群的原文",
        },
    }
    asyncio.run(server._handle_event(object(), event))
    if group_id == 20001:
        [question] = captured
        assert "当前群的原文" in question
    else:
        assert captured == []
        assert "reply" not in event


@pytest.mark.parametrize("quoted", [False, True])
def test_picture_context_distinguishes_current_sticker_from_someone_elses_quote(tmp_path, monkeypatch, quoted):
    server, engine = _media_server(tmp_path)
    vision_prompts = []
    replies = []

    async def load_image(*_args):
        return b"fixture", "fixture.png", ""

    async def describe(_image, _source, prompt, **_options):
        vision_prompts.append(prompt)
        return "表情/情绪：无奈；身份/出处：未确认；需要联网核验：否。", ""

    async def reply(_conversation, question, *_args, tool_context, visual_question, **_options):
        replies.append((question, visual_question, tool_context))
        return "收到"

    async def send(*_args, **_options):
        return "收到"

    monkeypatch.setattr(server.tools, "_resolve_ref_material", load_image)
    monkeypatch.setattr(server.tools, "_analyze_image_with_vision", describe)
    monkeypatch.setattr(engine, "reply", reply)
    monkeypatch.setattr(server, "send_reply", send)
    image = {"type": "image", "data": {"file": "fixture.png", "sub_type": 1}}
    message = [{"type": "at", "data": {"qq": "10001"}}]
    event = {
        "post_type": "message", "message_type": "group", "self_id": 10001,
        "user_id": 10002, "group_id": 20001, "message_id": 102,
        "message": message,
    }
    if quoted:
        caption = "把图中的文字逐字转写。"
        message.extend([
            {"type": "reply", "data": {"id": "101"}},
            {"type": "text", "data": {"text": "图1是什么？"}},
        ])
        event["reply"] = {
            "message_id": 101, "group_id": 20001, "sender": {"user_id": 10003, "nickname": "群友"},
            "message": [{"type": "text", "data": {"text": caption}}, image],
        }
    else:
        message.append(image)

    asyncio.run(server._handle_event(object(), event))

    [visual_prompt] = vision_prompts
    [(model_input, question, context)] = replies
    assert "普通发表情时自然接话" in context.prompt_context()
    assert not context.full_text_requested
    if quoted:
        assert context.image_origins[0].origin == "quoted"
        assert "原发送者是“群友”" in context.prompt_context()
        assert caption in model_input
        assert caption not in question
        assert caption not in visual_prompt
        assert "图1是什么？" in visual_prompt
        assert "不得用这张图推断当前说话人的情绪或意图" in visual_prompt
    else:
        assert context.image_origins[0].origin == "current"
        assert "用户此刻是什么状态、想表达什么" in visual_prompt


def test_two_separately_processed_image_batches_are_consumed_once(tmp_path, monkeypatch):
    server, engine = _media_server(tmp_path)
    analyzed = []

    async def analyze(incoming, _question, _caller, **_options):
        analyzed.append([
            image_origin_from_segment(segment, index=index).key
            for index, segment in enumerate(incoming["message"]) if is_visual_segment(segment)
        ])

    async def reply(*args, **kwargs):
        return "收到"

    def event(message_id, segments, user_id=10002):
        return {
            "post_type": "message", "message_type": "group", "self_id": 10001,
            "group_id": 20001, "user_id": user_id, "message_id": message_id,
            "message": segments,
        }

    def images(message_id):
        return [
            {"type": "image", "data": {"file": f"{message_id}-{index}"}}
            for index in range(2)
        ]

    monkeypatch.setattr(server.tools, "analyze", analyze)
    monkeypatch.setattr(server, "send_reply", reply)
    monkeypatch.setattr(server, "_duplicate_group_content", lambda *_args: False)
    monkeypatch.setattr(engine, "reply", reply)

    async def scenario():
        for message_id, sender in ((101, 10002), (201, 10003), (102, 10002)):
            await server._handle_event(object(), stamp_visual_segments(event(message_id, images(message_id), sender)))
        mention = [{"type": "at", "data": {"qq": "10001"}}]
        await server._handle_event(object(), event(103, mention))
        await server._handle_event(object(), event(104, mention))

    asyncio.run(scenario())
    assert analyzed == [[("101", 0), ("101", 1), ("102", 0), ("102", 1)], []]


@pytest.mark.parametrize("live_quote", [False, True])
def test_cached_multi_image_quote_reanalyzes_only_the_requested_original_image(tmp_path, monkeypatch, live_quote):
    server, engine = _media_server(tmp_path, visual_history_refs_enabled=True)
    origins = [ImageOrigin("101", i, "10002", evidence_level="verified") for i in (0, 2)]
    first = ToolAnalysisResult("image", "direct", visual_data=b"first", visual_status="verified")
    second = replace(first, visual_data=b"second")
    read_images = []
    used_contexts = []

    async def resolve_quote(*_args, **_kwargs):
        if live_quote:
            image_segment = {"type": "image", "data": {"file": "image-token"}}
            return "101", "[两张图]", [image_segment, {"type": "text", "data": {}}, image_segment], "", "10002", 0.0
        return ("101", "[引用消息原文暂时无法读取]", [], "", "", 0.0)

    async def followup(result, _question, **_kwargs):
        read_images.append(result.visual_data)
        return replace(result, findings=["只分析第二张"])

    async def reply(*_args, tool_context, **_kwargs):
        used_contexts.append(tool_context)
        return "收到"

    async def send(*_args, **_kwargs):
        return "收到"

    monkeypatch.setattr(server, "_resolve_quoted_message", resolve_quote)
    monkeypatch.setattr(server.tools, "analyze_visual_followup", followup)
    monkeypatch.setattr(server, "send_reply", send)
    monkeypatch.setattr(engine, "reply", reply)
    event = {
        "post_type": "message", "message_type": "private", "self_id": 10001,
        "user_id": 10002, "message_id": 102,
        "reply": {"message_id": 101, "sender": {"user_id": 10002}},
        "message": "第2张是什么？",
    }

    async def scenario():
        server._visual_followup_cache.remember(
            "private:10002", replace(first, image_origins=origins, image_results=[None if live_quote else first, second]),
            now=asyncio.get_running_loop().time(), max_bytes=1024,
        )
        await server._handle_event(object(), event)

    asyncio.run(scenario())
    assert read_images == [b"second"]
    assert [(origin.label, origin.segment_index) for origin in used_contexts[0].image_origins] == [
        ("图1", 0), ("图2", 2)
    ]
    expected_first = None if live_quote else []
    assert [item.findings if item else None for item in used_contexts[0].image_results] == [
        expected_first, ["只分析第二张"]
    ]
    budget_record = json.loads((tmp_path / "visual-budget.jsonl").read_text())
    assert budget_record["images_analyzed"] == (1 if live_quote else 2)
    assert budget_record["turn_elapsed_ms"] >= budget_record["processing_elapsed_ms"]


def test_multibubble_delivery_shares_remaining_delay_budget(tmp_path, monkeypatch):
    server, _engine = _media_server(tmp_path)
    sent = []

    async def send(_connection, _event, message, **_options):
        sent.append(message.content)
        return ""

    monkeypatch.setattr(server, "_send_one_reply", send)
    monkeypatch.setattr(onebot, "_first_reply_delay", lambda *args: 2.5)

    async def scenario():
        await asyncio.wait_for(server.send_reply(
            object(), {"message_type": "private", "user_id": 10002},
            [OutgoingMessage("text", "甲" * 100), OutgoingMessage("text", "乙" * 100)],
            delivery_deadline=asyncio.get_running_loop().time() + 0.035,
        ), timeout=0.4)

    asyncio.run(scenario())
    assert sent == ["甲" * 100, "乙" * 100]


def test_attribution_log_redacts_ids_and_prunes_old_rotations(tmp_path):
    expired = tmp_path / "visual-attribution-2020-01-01.jsonl"
    expired.write_text("old")
    old = time.time() - 8 * 86400
    os.utime(expired, (old, old))
    origin = ImageOrigin(
        "https://example.invalid/?rkey=secret", 0, "10002", "显示名",
        origin="quoted", status="unavailable", reason="timeout",
    )
    log_attribution(tmp_path, "group:20001", [origin], [])
    path = tmp_path / "visual-attribution.jsonl"
    raw = path.read_text()
    row = json.loads(raw)
    assert row["message_id"] == ""
    assert row["reason"] == "timeout"
    assert row["selected"] and not row["dropped"]
    assert all(value not in raw for value in ("rkey", "secret", "20001", "10002", "显示名"))
    assert not expired.exists()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
