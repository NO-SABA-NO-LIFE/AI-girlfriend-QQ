from __future__ import annotations

from atri_qq_bot.memory import UserMemoryStore
from atri_qq_bot.memory.helpers import build_group_bridge_for_private

WINDOW = 21_600
CAP = 15
LOOKAHEAD = 3


def _fill_group(store: UserMemoryStore, group_id: str, sequence, now: float) -> None:
    """sequence 是 (role, actor_id, text)；user 同时在成员 scope 留痕。"""
    for i, (role, actor, text) in enumerate(sequence):
        if role == "user":
            store.observe_user(
                f"group:{group_id}",
                text,
                now=now + i,
                actor_id=actor,
                nickname=f"人{actor}",
                source_context=f"group:{group_id}",
                visibility=f"group:{group_id}",
            )
            store.observe_user(
                f"group:{group_id}:user:{actor}",
                text,
                now=now + i,
                actor_id=actor,
                nickname=f"人{actor}",
                source_context=f"group:{group_id}",
                visibility=f"group:{group_id}",
            )
        else:
            store.observe_bot(f"group:{group_id}", text, now=now + i)


def _build(store: UserMemoryStore, now: float, **overrides):
    params = dict(
        window_seconds=WINDOW,
        max_entries=CAP,
        reply_lookahead=LOOKAHEAD,
    )
    params.update(overrides)
    return build_group_bridge_for_private(
        store._data, "private:10001", now, **params
    )


def test_bridge_pairs_reply_even_when_others_interleave(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")
    now = 10_000.0
    _fill_group(
        store,
        "1051645731",
        [
            ("user", 10001, "我们周末去漫展吧"),
            ("user", 20002, "我也想去"),
            ("user", 20003, "+1"),
            ("assistant", None, "记得提前买票"),
            ("user", 10001, "那我先订票"),
            ("assistant", None, "订完发我"),
        ],
        now,
    )

    entries = _build(store, now + 6)
    # 本人两条发言 + 配对的两条回复，按时间正序。
    assert [e["kind"] for e in entries] == [
        "self_speech",
        "topic_reply",
        "self_speech",
        "topic_reply",
    ]
    assert entries[0]["text"] == "我们周末去漫展吧"
    assert entries[1]["text"] == "记得提前买票"
    assert entries[3]["text"] == "订完发我"


def test_bridge_excludes_other_members_speech(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")
    now = 10_000.0
    _fill_group(
        store,
        "1051645731",
        [
            ("user", 20002, "别人之间的闲聊"),
            ("user", 10001, "我才进来聊的话题"),
            ("assistant", None, "这是对你说的"),
        ],
        now,
    )

    entries = _build(store, now + 3)
    texts = [e["text"] for e in entries]
    assert "别人之间的闲聊" not in texts
    assert "我才进来聊的话题" in texts


def test_bridge_respects_time_window(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")
    now = 10_000.0
    # 第一条发言在 7 小时前（窗口外），第二条在窗口内。
    _fill_group(
        store,
        "1051645731",
        [("user", 10001, "很久以前的话题")],
        now - 25_200,
    )
    _fill_group(
        store,
        "1051645731",
        [("user", 10001, "最近的话题")],
        now - 60,
    )

    entries = _build(store, now)
    texts = [e["text"] for e in entries]
    assert "很久以前的话题" not in texts
    assert "最近的话题" in texts


def test_bridge_fail_closed_for_legacy_entries_without_actor_id(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")
    now = 10_000.0
    # 直接写入一条没有 actor_id 的旧格式 user 消息。
    group_item = store._conversation("group:1051645731")
    group_item["history"] = [
        {"at": now - 100, "role": "user", "text": "旧格式消息，没有 actor_id"}
    ]
    store._conversation("group:1051645731:user:10001")

    assert _build(store, now) == []


def test_bridge_caps_entries_and_keeps_most_recent(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")
    now = 10_000.0
    sequence = [
        ("user", 10001, f"话题{i}")
        for i in range(10)
    ]
    _fill_group(store, "1051645731", sequence, now)

    entries = _build(store, now + 10, max_entries=4)
    assert len(entries) == 4
    # 保留最近的 4 条。
    assert [e["text"] for e in entries] == [
        "话题6",
        "话题7",
        "话题8",
        "话题9",
    ]


def test_bridge_merges_multiple_groups_in_time_order(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")
    now = 10_000.0
    _fill_group(
        store, "111", [("user", 10001, "一群的话题")], now
    )
    _fill_group(
        store, "222", [("user", 10001, "二群的话题")], now + 5
    )

    entries = _build(store, now + 6)
    assert [e["group_id"] for e in entries] == ["111", "222"]


def test_bridge_empty_when_user_id_cannot_be_parsed(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")
    entries = build_group_bridge_for_private(
        store._data,
        "group:1051645731",  # 群整体 scope，解析不出私聊用户
        10_000.0,
        window_seconds=WINDOW,
        max_entries=CAP,
        reply_lookahead=LOOKAHEAD,
    )
    assert entries == []


def test_rendered_context_labels_speakers_and_sources(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json")
    now = 10_000.0
    _fill_group(
        store,
        "1051645731",
        [
            ("user", 10001, "桥接话题"),
            ("assistant", None, "桥接回复"),
        ],
        now,
    )

    rendered = store.group_bridge_context(
        "private:10001",
        now + 2,
        window_seconds=WINDOW,
        max_entries=CAP,
        reply_lookahead=LOOKAHEAD,
    )
    assert "用户在群里说：桥接话题" in rendered
    assert "亚托莉当时在群里的回复：桥接回复" in rendered
    # 无内容时返回空串。
    assert (
        store.group_bridge_context(
            "private:99999",
            now,
            window_seconds=WINDOW,
            max_entries=CAP,
            reply_lookahead=LOOKAHEAD,
        )
        == ""
    )
