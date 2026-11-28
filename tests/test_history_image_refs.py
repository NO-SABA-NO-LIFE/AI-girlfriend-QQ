from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from atri_qq_bot.memory import UserMemoryStore
from atri_qq_bot.memory.image_history import build_image_refs
from atri_qq_bot.memory.sqlite_store import MemorySqliteStore
from atri_qq_bot.onebot.message_batch import _merge_message_batch
from atri_qq_bot.persona.engine_state import EngineStateMixin
from atri_qq_bot.toolbox_parts.visual_origin import stamp_visual_segments


def _event(message_id, *segments):
    return {
        "message_id": message_id,
        "user_id": 30001,
        "sender": {"nickname": "小林"},
        "time": 1000.0,
        "message": list(segments),
    }


def _image(file):
    return {"type": "image", "data": {"file": file}}


def test_group_history_keeps_original_image_locations_and_commits_once(
    tmp_path, monkeypatch,
):
    secret_url = "https://qpic.example/image?rkey=do-not-save"
    first = stamp_visual_segments(_event(101, _image(secret_url)), message_order=0)
    second = stamp_visual_segments(
        _event(102, {"type": "text", "data": {"text": "两张图"}}, _image("id-102.jpg")),
        message_order=1,
    )
    _, event = _merge_message_batch([(None, first), (None, second)])
    refs = build_image_refs(event)
    path = tmp_path / "users.json"
    store = UserMemoryStore(path)
    replacements = []
    replace = Path.replace

    def count_replace(source, dest):
        result = replace(source, dest)
        if dest == path:
            replacements.append(dest)
        return result

    monkeypatch.setattr(Path, "replace", count_replace)
    store.record_group_incoming(
        20001,
        30001,
        "这是两张图",
        now=1000.0,
        event_id=str(event["message_id"]),
        image_refs=refs,
    )

    assert replacements == [path]
    saved = json.loads(path.read_text(encoding="utf-8"))["conversations"]
    assert saved["group:20001"]["message_count"] == 1
    assert saved["group:20001:user:30001"]["message_count"] == 1
    entry = saved["group:20001"]["history"][0]
    assert entry["message_id"] == "102"
    assert [
        (ref["message_id"], ref["segment_index"], ref["file_id"], ref["message_order"])
        for ref in entry["image_refs"]
    ] == [("101", 0, "", 0), ("102", 1, "id-102.jpg", 1)]
    assert secret_url not in path.read_text(encoding="utf-8")
    assert "do-not-save" not in path.read_text(encoding="utf-8")

    reopened = UserMemoryStore(path)
    assert reopened.history_image_refs("group:20001", 101, now=1500) == refs[:1]
    assert reopened.history_image_refs("group:20001", "102", now=1500) == refs[1:]
    assert reopened.history_image_refs("group:20002", "102", now=1500) == []
    assert reopened.history_image_refs("private:30001", "102", now=1500) == []
    assert reopened.history_image_refs("group:20001", "101", now=2801) == []


def test_image_history_ignores_noncurrent_or_malformed_segments_and_bounds_refs():
    ordinary_text = _event(10)
    ordinary_text["message"] = "OneBot plain text"
    assert build_image_refs(ordinary_text) == []

    event = _event(
        11,
        None,
        {"type": "file", "data": {"file": "document.png"}},
        *(_image(f"file_{number}") for number in range(11)),
    )
    refs = build_image_refs(event)
    assert [(ref["segment_index"], ref["file_id"]) for ref in refs] == [
        (number + 2, f"file_{number}") for number in range(8)
    ]
    previous = stamp_visual_segments(_event(9, _image("previous.png")), origin="quoted")
    assert build_image_refs(previous) == []


def test_private_history_writes_only_valid_ref_metadata_and_keeps_old_entries(tmp_path):
    path = tmp_path / "users.json"
    store = UserMemoryStore(path)
    store.observe_user("private:30001", "旧格式条目", now=999, event_id="99")
    old_entry = store.recent_history("private:30001")[0].copy()
    assert "message_id" not in old_entry

    refs = build_image_refs(_event(101, _image("old-name.jpg")))
    refs[0]["file_id"] = "RKEY=unsafe"
    refs[0]["url"] = "https://example.invalid/not-for-persistence"
    store.observe_user(
        "private:30001", "我的图片", now=1000, event_id="101", image_refs=refs + refs
    )
    store.observe_user("private:30002", "另一个私聊", now=1000, event_id="101", image_refs=[])

    reopened = UserMemoryStore(path)
    assert reopened.recent_history("private:30001")[0] == old_entry
    saved_refs = reopened.history_image_refs("private:30001", "101", now=1001)
    assert len(saved_refs) == 1
    assert saved_refs[0]["segment_index"] == 0
    assert saved_refs[0]["file_id"] == ""
    assert "url" not in saved_refs[0]
    assert reopened.history_image_refs("private:30002", "101", now=1001) == []
    assert reopened.recent_history("private:30002")[0]["message_id"] == "101"


def test_engine_passes_image_metadata_through_the_existing_observation_paths(tmp_path):
    engine = object.__new__(EngineStateMixin)
    engine.config = SimpleNamespace(owner_qqs=())
    engine.memory = UserMemoryStore(tmp_path / "users.json")
    image = _image("locator")
    refs = build_image_refs(_event(101, image))
    engine.observe_incoming(
        "private:30001", "私聊图片", actor_id=30001, event_id="101", image_refs=refs
    )
    engine.observe_group_incoming(
        20001, 30001, "群聊图片", remember_targets=True, event_id="101", image_refs=refs
    )
    for conversation in ("private:30001", "group:20001"):
        entry = engine.memory.recent_history(conversation)[0]
        assert entry["message_id"] == "101"
        assert entry["image_refs"] == refs


def test_sqlite_storage_preserves_optional_image_fields_on_round_trip(tmp_path):
    database = tmp_path / "memory.sqlite3"
    refs = build_image_refs(_event(101, _image("locator")))
    history = [
        {"at": 1000.0, "role": "user", "text": "旧条目"},
        {
            "at": 1001.0,
            "role": "user",
            "text": "带图条目",
            "message_id": "101",
            "image_refs": refs,
        },
        {"at": 1002.0, "role": "assistant", "text": "回复"},
    ]
    original = {"history": history}
    MemorySqliteStore(database).write_conversation("private:30001", original)
    reopened = MemorySqliteStore(database)
    assert reopened.read_conversation("private:30001") == original
    assert reopened.history("private:30001", limit=2) == history[-2:]
