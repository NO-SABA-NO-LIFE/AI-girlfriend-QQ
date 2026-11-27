from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import atri_shared.config_admin as config_admin
import atri_qq_bot.__main__ as main_module
import atri_qq_bot.config.loader as loader
import atri_webui.server as webui_server
from atri_webui.server_parts.handler_config import HandlerConfigMixin


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("REPLY_MODE=mention\nBOT_QQ=123456\n", encoding="utf-8")
    monkeypatch.setattr(config_admin, "ENV_PATH", env_path)
    monkeypatch.setattr(loader, "_project_root", lambda: tmp_path)
    return env_path


def test_merge_env_does_not_touch_disk(isolated_env) -> None:
    before = isolated_env.read_bytes()
    merged = config_admin.merge_env({"REPLY_MODE": "all"})
    assert merged["REPLY_MODE"] == "all"
    assert isolated_env.read_bytes() == before


def test_invalid_candidate_is_rejected_before_write(isolated_env) -> None:
    before = isolated_env.read_bytes()
    candidate = config_admin.merge_env({"REPLY_MODE": "typo"})
    with pytest.raises(ValueError):
        # 与 handler 相同的写盘前校验路径：overrides 仅临时改 os.environ 并还原，不写盘。
        loader.load_config(overrides=candidate)
    # 校验失败：.env 一个字节都不能变，错误值不得落盘。
    assert isolated_env.read_bytes() == before
    assert "typo" not in isolated_env.read_text(encoding="utf-8")


def test_valid_change_is_persisted(isolated_env) -> None:
    candidate = config_admin.merge_env({"REPLY_MODE": "all"})
    loader.load_config(overrides=candidate)
    config_admin.update_env({"REPLY_MODE": "all"})
    assert "REPLY_MODE=all" in isolated_env.read_text(encoding="utf-8")


def test_main_returns_nonzero_when_config_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module, "load_config", mock.Mock(side_effect=ValueError("bad"))
    )
    assert main_module.main() == 1


class _ConfigHandler(HandlerConfigMixin):
    """Minimal handler implementing only the IO used by _handle_config_update."""

    def __init__(self, payload) -> None:
        self._payload = payload
        self.sent: dict | None = None
        self.error: tuple | None = None

    def _read_json(self) -> dict:
        return self._payload

    def _send_json(self, payload: dict) -> None:
        self.sent = payload

    def _send_error(self, status, message: str) -> None:
        self.error = (status, message)

    def _refuse_when_read_only(self, action: str) -> bool:
        return False


def _point_env(monkeypatch, env_path) -> None:
    # Point both the shared module and the server module (whose globals are
    # copied into the handler mixin by _sync_webui_globals) at the temp .env.
    monkeypatch.setattr(config_admin, "ENV_PATH", env_path)
    monkeypatch.setattr(webui_server, "ENV_PATH", env_path)


def test_rollback_restores_file_env_runtime_and_new_process(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("REPLY_MODE=mention\nTEMPERATURE=0.4\n", encoding="utf-8")
    original_bytes = env_path.read_bytes()
    _point_env(monkeypatch, env_path)
    monkeypatch.setattr(loader, "_project_root", lambda: tmp_path)
    os.environ.pop("STICKER_CHANCE", None)

    state = SimpleNamespace()
    rebuilt: dict = {}
    call = {"n": 0}

    async def fake_reload():
        # Real load against the on-disk .env so os.environ and the config are
        # genuine, not a stub: merely asserting "reload was called twice" would
        # not prove the runtime actually returned to the previous config.
        call["n"] += 1
        cfg = loader.load_config(env_path)
        if call["n"] == 1:
            raise RuntimeError("insights boom")  # late component failure
        rebuilt["cfg"] = cfg
        return cfg

    state.reload_config = fake_reload
    monkeypatch.setattr(
        webui_server, "run_coro", lambda coro: asyncio.run(coro)
    )

    handler = _ConfigHandler({"STICKER_CHANCE": "0.9"})
    handler.webui_state = state
    handler._handle_config_update()

    assert handler.error is not None
    # (1) the .env bytes are restored exactly ...
    assert env_path.read_bytes() == original_bytes
    # ... (2) process env presence and value are restored: the newly added key
    # is removed rather than left holding the applied value ...
    assert "STICKER_CHANCE" not in os.environ
    # ... (3) the rebuilt runtime config is the previous valid state ...
    runtime_cfg = rebuilt["cfg"]
    assert runtime_cfg.sticker_chance == pytest.approx(0.24)
    assert runtime_cfg.temperature == pytest.approx(0.4)
    assert call["n"] == 2
    # ... (4) a brand-new process reading the restored file gets the old config.
    code = (
        "from atri_qq_bot.config.loader import load_config;"
        "c=load_config(r'''%s''');"
        "print(round(c.sticker_chance,4), round(c.temperature,4))" % str(env_path)
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "0.24 0.4"


def test_config_transaction_holds_the_shared_lock(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("REPLY_MODE=mention\nTEMPERATURE=0.4\n", encoding="utf-8")
    _point_env(monkeypatch, env_path)
    monkeypatch.setattr(loader, "_project_root", lambda: tmp_path)
    real_config = loader.load_config(env_path)
    held_during_reload: dict = {}

    state = SimpleNamespace()

    async def fake_reload():
        # The reload runs inside the handler's critical section, so the lock is
        # held on this thread. The rollback uses the same `with` block/lock.
        held_during_reload["held"] = config_admin.CONFIG_UPDATE_LOCK._is_owned()
        return real_config

    state.reload_config = fake_reload
    monkeypatch.setattr(
        webui_server, "run_coro", lambda coro: asyncio.run(coro)
    )

    handler = _ConfigHandler({"REPLY_MODE": "all"})
    handler.webui_state = state
    handler._handle_config_update()

    assert webui_server.CONFIG_UPDATE_LOCK is config_admin.CONFIG_UPDATE_LOCK
    assert held_during_reload["held"] is True


def test_write_env_uses_atomic_replace(isolated_env, monkeypatch) -> None:
    replaced: list = []

    def record_replace(src, dst):
        replaced.append((src, dst))

    monkeypatch.setattr(config_admin.os, "replace", record_replace)
    config_admin.write_env({"REPLY_MODE": "all"}, target_path=isolated_env)

    # The official file is published through os.replace, not written in place.
    assert replaced and replaced[0][1] == isolated_env


def test_write_env_leaves_no_temp_file(isolated_env) -> None:
    config_admin.write_env({"REPLY_MODE": "all"}, target_path=isolated_env)
    assert "REPLY_MODE=all" in isolated_env.read_text(encoding="utf-8")
    assert not [p for p in isolated_env.parent.iterdir() if ".tmp." in p.name]


def test_successful_save_applies_and_returns_ok(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("REPLY_MODE=mention\nTEMPERATURE=0.4\n", encoding="utf-8")
    _point_env(monkeypatch, env_path)

    state = SimpleNamespace()
    real_config = loader.load_config(env_path)

    async def fake_reload():
        return real_config

    state.reload_config = fake_reload
    monkeypatch.setattr(
        webui_server,
        "run_coro",
        lambda coro: asyncio.run(coro),
    )

    handler = _ConfigHandler({"REPLY_MODE": "all"})
    handler.webui_state = state
    handler._handle_config_update()

    assert handler.error is None
    assert handler.sent and handler.sent["ok"] is True
    assert "REPLY_MODE=all" in env_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("initial", "expected"),
    [
        (None, 0o600),
        (0o600, 0o600),
        (0o640, 0o640),
    ],
)
def test_write_env_preserves_file_mode(tmp_path, initial, expected) -> None:
    env_path = tmp_path / ".env"
    if initial is not None:
        env_path.write_text("REPLY_MODE=mention\n", encoding="utf-8")
        os.chmod(env_path, initial)
    config_admin.write_env({"REPLY_MODE": "all"}, target_path=env_path)
    assert stat.S_IMODE(env_path.stat().st_mode) == expected


def test_isolated_validation_uses_a_unique_candidate_file(
    isolated_env, monkeypatch
) -> None:
    used_paths: list = []
    real_run = subprocess.run

    def capture_run(args, **kwargs):
        used_paths.append(args[-1])
        return real_run(args, **kwargs)

    monkeypatch.setattr(config_admin.subprocess, "run", capture_run)
    config_admin.validate_env_isolated({"REPLY_MODE": "all"})
    config_admin.validate_env_isolated({"REPLY_MODE": "mention"})

    # Each transaction validates its own distinct file; no shared candidate.
    assert len(used_paths) == 2
    assert used_paths[0] != used_paths[1]


def test_all_writers_reference_one_shared_lock() -> None:
    assert webui_server.CONFIG_UPDATE_LOCK is config_admin.CONFIG_UPDATE_LOCK


def test_config_lock_blocks_while_held() -> None:
    lock = config_admin.CONFIG_UPDATE_LOCK
    result: dict = {}
    with lock:
        thread = threading.Thread(
            target=lambda: result.__setitem__("got", lock.acquire(timeout=0.2))
        )
        thread.start()
        thread.join()
    assert result["got"] is False


def test_run_coro_cancels_timed_out_coroutine() -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    webui_server.bind_loop(loop)
    cancelled = {"v": False}

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["v"] = True
            raise

    try:
        with pytest.raises(TimeoutError):
            webui_server.run_coro(slow(), timeout=0.1)
        deadline = time.monotonic() + 2.0
        while not cancelled["v"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert cancelled["v"] is True
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
