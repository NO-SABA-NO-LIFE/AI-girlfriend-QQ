from __future__ import annotations

import json
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import atri_qq_bot.memory as memory_module
from atri_qq_bot.memory import UserMemoryStore
from atri_webui.memory_admin import MemoryAdmin


def test_group_incoming_commits_once_with_legacy_memory_content(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    store = UserMemoryStore(path)
    legacy_path = tmp_path / "legacy.json"
    legacy = UserMemoryStore(legacy_path)
    event = {"message_type": "group", "group_id": 20001, "user_id": 30001}
    replacements = []
    replace = pathlib.Path.replace

    def count_replace(source, target):
        result = replace(source, target)
        if target == path:
            replacements.append(result)
        return result

    monkeypatch.setattr(pathlib.Path, "replace", count_replace)
    for addressed, now in ((False, 1000), (True, 1010)):
        text = "我叫小明，喜欢咖啡，明天有考试"
        legacy.remember_target("group:20001", event)
        legacy.remember_target("group:20001:user:30001", event)
        legacy.observe_group_message(20001, 30001, text, "小明", now, addressed)

        replacements.clear()
        assert store.record_group_incoming(
            20001, 30001, text, "小明", now=now, addressed_to_bot=addressed
        ) == ("group:20001", "group:20001:user:30001")
        assert replacements == [path]
        assert json.loads(path.read_text()) == json.loads(legacy_path.read_text())

    assert UserMemoryStore(path).recent_history("group:20001") == store.recent_history(
        "group:20001"
    )


def test_group_incoming_rolls_back_after_interrupted_observation(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    store = UserMemoryStore(path)
    store.record_group_incoming(20001, 30001, "原消息", now=1000)
    before = path.read_bytes()
    requests = []
    monkeypatch.setattr(store._memory_worker, "enabled", True)
    monkeypatch.setattr(store._memory_worker, "submit", requests.append)

    def abort(*args, **kwargs):
        raise ValueError("interrupted before updating the person profile")

    with monkeypatch.context() as patch:
        patch.setattr(store, "remember_person_profile_from_group", abort)
        with pytest.raises(ValueError, match="interrupted"):
            store.record_group_incoming(20001, 40001, "未完成的消息", now=1010)

    assert requests == []
    assert path.read_bytes() == before
    store.record_group_incoming(20001, 30001, "下一条消息", now=1020)

    data = json.loads(path.read_bytes())["conversations"]
    group = data["group:20001"]
    assert [entry["text"] for entry in group["history"]] == ["原消息", "下一条消息"]
    assert [entry["text"] for entry in group["structured_memory"]["l3"]] == [
        "原消息", "下一条消息",
    ]
    assert "group:20001:user:40001" not in data
    assert [request.conversation_id for request in requests] == [
        "group:20001", "group:20001:user:30001", "private:30001",
    ]


def test_group_incoming_keeps_external_replacement(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    store = UserMemoryStore(path)
    store.record_group_incoming(20001, 30001, "原消息", now=1000)
    edited = json.loads(path.read_bytes())
    edited["conversations"]["private:30001"]["affection_score"] = 87
    staged_file = tmp_path / "edited.json"
    staged_file.write_text(json.dumps(edited), encoding="utf-8")
    observe_group = store.observe_group_message
    requests = []
    monkeypatch.setattr(store._memory_worker, "enabled", True)
    monkeypatch.setattr(store._memory_worker, "submit", requests.append)

    def external_edit(*args, **kwargs):
        staged_file.replace(path)
        return observe_group(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "observe_group_message", external_edit)
        store.record_group_incoming(20001, 30001, "遇到外部修改的消息", now=1010)

    assert json.loads(path.read_bytes()) == edited
    assert requests == []
    store.record_group_incoming(20001, 30001, "接在编辑后面的消息", now=1020)
    data = json.loads(path.read_bytes())["conversations"]
    assert data["private:30001"]["affection_score"] == 87
    assert [entry["text"] for entry in data["group:20001"]["structured_memory"]["l3"]] == [
        "原消息", "接在编辑后面的消息",
    ]


def test_group_incoming_keeps_context_in_memory_until_corrupt_file_is_repaired(
    tmp_path, monkeypatch
):
    path = tmp_path / "users.json"
    damaged = b'{"conversations":'
    path.write_bytes(damaged)
    store = UserMemoryStore(path)
    requests = []
    monkeypatch.setattr(store._memory_worker, "enabled", True)
    monkeypatch.setattr(store._memory_worker, "submit", requests.append)

    store.record_group_incoming(20001, 30001, "临时消息一", now=1000)
    store.record_group_incoming(20001, 30001, "临时消息二", now=1010)
    group = "group:20001"
    assert [entry["text"] for entry in store.recent_history(group)] == [
        "临时消息一",
        "临时消息二",
    ]
    assert [entry["text"] for entry in store._session_l3[group]] == [
        "临时消息一",
        "临时消息二",
    ]
    assert path.read_bytes() == damaged
    assert requests == []

    restored_path = tmp_path / "restored.json"
    backup = UserMemoryStore(restored_path)
    backup.record_group_incoming(20001, 30001, "备份中的消息", now=900)
    restored_path.replace(path)
    store.record_group_incoming(20001, 30001, "恢复后的消息", now=1020)

    conversations = json.loads(path.read_bytes())["conversations"]
    for key in (group, "group:20001:user:30001", "private:30001"):
        assert [entry["text"] for entry in conversations[key]["structured_memory"]["l3"]] == [
            "备份中的消息",
            "恢复后的消息",
        ]
    assert [entry["text"] for entry in conversations[group]["history"]] == [
        "备份中的消息",
        "恢复后的消息",
    ]
    assert [request.conversation_id for request in requests] == [
        group,
        "group:20001:user:30001",
        "private:30001",
    ]


def test_group_incoming_keeps_complete_update_for_save_retry(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    store = UserMemoryStore(path)

    def fail_write(*args, **kwargs):
        raise OSError("temporary write failed")

    with monkeypatch.context() as patch:
        patch.setattr(memory_module.json, "dump", fail_write)
        store.record_group_incoming(20001, 30001, "暂时无法写入的消息", now=1000)

    assert not path.exists()
    assert store._save_failure_count == 1
    store._save_retry_after = 0.0
    store.record_group_incoming(20001, 30001, "恢复写入的消息", now=1010)
    data = json.loads(path.read_bytes())["conversations"]
    assert data["group:20001"]["message_count"] == 2
    assert data["group:20001:user:30001"]["message_count"] == 2
    assert [entry["text"] for entry in data["private:30001"]["structured_memory"]["l3"]] == [
        "暂时无法写入的消息", "恢复写入的消息",
    ]


def test_webui_relationship_edit_and_incoming_message_both_persist(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    store = UserMemoryStore(path)
    admin = MemoryAdmin(path, tmp_path / "backups")
    first_conversation = "private:10001"
    store.observe_user(first_conversation, "第一条消息", now=1000)
    admin.update_relationship(first_conversation, 35, None)
    reading = Event()
    editing = Event()
    continue_read = Event()
    original_read = store._read_file

    def pause_after_read():
        data = original_read()
        reading.set()
        assert continue_read.wait(5)
        return data

    def edit():
        unlocked = admin._lock.acquire(blocking=False)
        if unlocked:
            admin._lock.release()
        editing.set()
        assert not unlocked, "WebUI must wait for the message's memory transaction"
        return admin.update_relationship(first_conversation, 85, None)

    monkeypatch.setattr(store, "_read_file", pause_after_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        message = pool.submit(store.observe_user, "private:10002", "并发消息", now=1001)
        try:
            assert reading.wait(5)
            change = pool.submit(edit)
            assert editing.wait(5)
        finally:
            continue_read.set()
        message.result(timeout=5)
        change.result(timeout=5)
    data = json.loads(path.read_text(encoding="utf-8"))["conversations"]
    assert data[first_conversation]["affection_score"] == 85
    assert data["private:10002"]["history"][-1]["text"] == "并发消息"


def test_memory_writes_traits_events_and_session_context_to_separate_layers(tmp_path) -> None:
    memory_path = tmp_path / "users.json"
    store = UserMemoryStore(memory_path)

    store.observe_user("private:10001", "我喜欢咖啡", now=1000)
    store.observe_user("private:10001", "我喜欢咖啡", now=1001)
    store.observe_user("private:10001", "我平时晚上学习", now=1002)
    store.observe_user("private:10001", "我平时晚上学习", now=1003)
    store.observe_user("private:10001", "明天有考试", now=1004)
    store.observe_user("private:10001", "现在正在讨论记忆系统", now=1005)
    store.observe_user("private:10001", "谢谢你", now=1006)
    store.observe_user("private:10001", "哈哈", now=1007)

    saved = json.loads(memory_path.read_text(encoding="utf-8"))
    structured = saved["conversations"]["private:10001"]["structured_memory"]

    assert any(entry["value"] == "咖啡" for entry in structured["l1"])
    assert any(entry["value"] == "晚上学习" for entry in structured["l1"])
    assert any("考试" in entry["value"] for entry in structured["l2"])
    assert not any(entry.get("value") == "谢谢你" for entry in structured["l2"])
    assert any(entry["value"] == "现在正在讨论记忆系统" for entry in structured["l3"])
    assert not any(entry.get("value") == "哈哈" for entry in structured["l1"] + structured["l2"] + structured["l3"])

    detail = MemoryAdmin(memory_path, tmp_path / "backups").detail("id=private%3A10001")
    assert any(entry["value"] == "咖啡" for entry in detail["layers"]["l1"])
    assert any("考试" in entry["value"] for entry in detail["layers"]["l2"])
    assert any(entry["text"] == "现在正在讨论记忆系统" for entry in detail["layers"]["l3"])
    assert "哈哈" not in detail["natural"]


def test_memory_save_failure_does_not_break_message_path(tmp_path, monkeypatch) -> None:
    memory_path = tmp_path / "users.json"
    store = UserMemoryStore(memory_path)
    original_dump = memory_module.json.dump
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise MemoryError("simulated memory pressure")
        return original_dump(*args, **kwargs)

    monkeypatch.setattr(memory_module.json, "dump", fail_once)
    # 保存失败不能让消息处理函数抛出异常。
    store.observe_user("private:10001", "这条消息仍然应该得到回复", now=1000)

    assert not memory_path.exists()
    assert store._save_failure_count == 1

    # 模拟内存恢复；退避结束后仍可继续正常落盘。
    store._save_retry_after = 0.0
    store.observe_user("private:10001", "记忆应该继续保存", now=1001)
    saved = json.loads(memory_path.read_text(encoding="utf-8"))
    assert saved["conversations"]["private:10001"]["message_count"] == 2


def _bump_mtime(path) -> None:
    """强制文件 mtime 变化，让下一次访问触发重读。"""
    os.utime(
        path,
        ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000),
    )


def test_reload_read_failure_keeps_existing_file_and_skips_save(
    tmp_path, monkeypatch
) -> None:
    memory_path = tmp_path / "users.json"
    store = UserMemoryStore(memory_path)
    store.observe_user("private:10001", "这是会话A的内容", now=1000)
    original_bytes = memory_path.read_bytes()

    def fail_read(self, *args, **kwargs):
        raise OSError("transient read failure")

    _bump_mtime(memory_path)
    monkeypatch.setattr(pathlib.Path, "read_text", fail_read)
    store.observe_user("private:10002", "这是会话B的内容", now=1001)

    # 读取失败后进入降级，主文件一个字节都不能被改动。
    assert store._reload_failed is True
    monkeypatch.undo()
    assert memory_path.read_bytes() == original_bytes
    saved = json.loads(memory_path.read_text(encoding="utf-8"))
    assert list(saved["conversations"]) == ["private:10001"]


def test_reload_recovers_after_transient_failure(tmp_path, monkeypatch) -> None:
    memory_path = tmp_path / "users.json"
    store = UserMemoryStore(memory_path)
    store.observe_user("private:10001", "这是会话A的内容", now=1000)

    def fail_read(self, *args, **kwargs):
        raise OSError("transient read failure")

    _bump_mtime(memory_path)
    monkeypatch.setattr(pathlib.Path, "read_text", fail_read)
    store.observe_user("private:10002", "故障期间的会话B", now=1001)
    assert store._reload_failed is True

    # 读取恢复正常；下一次访问成功重读并恢复保存。
    monkeypatch.undo()
    _bump_mtime(memory_path)
    store.observe_user("private:10003", "恢复后的会话C", now=1002)

    assert store._reload_failed is False
    saved = json.loads(memory_path.read_text(encoding="utf-8"))
    assert "private:10001" in saved["conversations"]
    assert "private:10003" in saved["conversations"]


def test_corrupt_existing_file_at_startup_does_not_overwrite(tmp_path) -> None:
    memory_path = tmp_path / "users.json"
    memory_path.write_text("{not valid json", encoding="utf-8")
    original_bytes = memory_path.read_bytes()

    # 启动时已有文件却解析失败：降级运行，但绝不覆盖主文件。
    store = UserMemoryStore(memory_path)
    assert store._reload_failed is True
    store.observe_user("private:10001", "降级期间的新消息", now=1000)
    assert memory_path.read_bytes() == original_bytes

    # 修复文件后，下一次访问成功重读并恢复正常保存。
    memory_path.write_text(
        json.dumps(
            {"version": 1, "conversations": {"private:10009": {"message_count": 1}}}
        ),
        encoding="utf-8",
    )
    store.observe_user("private:10001", "恢复后的新消息", now=1001)
    assert store._reload_failed is False
    saved = json.loads(memory_path.read_text(encoding="utf-8"))
    assert "private:10009" in saved["conversations"]


def test_reload_recovers_without_file_change_after_boot_failure(
    tmp_path, monkeypatch
) -> None:
    """启动读取失败后，即使文件字节/mtime/inode 完全不变，也要能退出降级。"""
    memory_path = tmp_path / "users.json"
    memory_path.write_text(
        json.dumps(
            {"version": 1, "conversations": {"private:10001": {"message_count": 1}}}
        ),
        encoding="utf-8",
    )
    original_bytes = memory_path.read_bytes()

    def fail_read(self, *args, **kwargs):
        raise OSError("boot failure")

    # 构造（启动）期间第一次读取就失败；文件本身始终完好。
    monkeypatch.setattr(pathlib.Path, "read_text", fail_read)
    store = UserMemoryStore(memory_path)
    assert store._reload_failed is True
    monkeypatch.undo()  # 瞬时故障解除，没有 touch 任何文件

    # 不改变文件，下一次访问也必须成功重读、清除标志并恢复保存。
    store.observe_user("private:10002", "恢复后的新消息", now=1000)

    assert store._reload_failed is False
    saved = json.loads(memory_path.read_text(encoding="utf-8"))
    assert "private:10001" in saved["conversations"]
    assert "private:10002" in saved["conversations"]
    assert original_bytes != memory_path.read_bytes()


def test_invalid_utf8_degrades_without_overwriting(tmp_path) -> None:
    memory_path = tmp_path / "users.json"
    memory_path.write_bytes(b'{"version": 1, "conversations": {}, \xff\xfe bad}')
    original_bytes = memory_path.read_bytes()

    # 非法 UTF-8（UnicodeDecodeError）与其他读取失败走同一条降级路径。
    store = UserMemoryStore(memory_path)
    assert store._reload_failed is True
    assert memory_path.read_bytes() == original_bytes


def test_non_integer_version_degrades_without_overwriting(tmp_path) -> None:
    memory_path = tmp_path / "users.json"
    memory_path.write_text(
        '{"version": "not-a-number", "conversations": {}}', encoding="utf-8"
    )
    original_bytes = memory_path.read_bytes()

    # version 无法转成整数时同样降级，初始化不中断、主文件不被覆盖。
    store = UserMemoryStore(memory_path)
    assert store._reload_failed is True
    assert memory_path.read_bytes() == original_bytes


def test_overflowing_version_degrades_and_recovers(tmp_path) -> None:
    memory_path = tmp_path / "users.json"
    invalid_bytes = b'{"version": 1e999, "conversations": {}}'
    memory_path.write_bytes(invalid_bytes)

    store = UserMemoryStore(memory_path)
    store.observe_user("private:10001", "降级期间", now=1000)
    assert store._reload_failed is True
    assert memory_path.read_bytes() == invalid_bytes

    memory_path.write_text('{"version": 1, "conversations": {}}', encoding="utf-8")
    store.observe_user("private:10002", "恢复后", now=1001)
    assert store._reload_failed is False
    saved = json.loads(memory_path.read_text(encoding="utf-8"))
    assert set(saved["conversations"]) == {"private:10002"}
