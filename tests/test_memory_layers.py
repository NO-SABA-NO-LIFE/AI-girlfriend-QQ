from __future__ import annotations

import json
import os
import pathlib

import atri_qq_bot.memory as memory_module
from atri_qq_bot.memory import UserMemoryStore
from atri_webui.memory_admin import MemoryAdmin


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
