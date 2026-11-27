from __future__ import annotations

import asyncio
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
        self.reload_calls = 0

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


def test_rollback_when_reload_fails_after_write(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("REPLY_MODE=mention\nTEMPERATURE=0.4\n", encoding="utf-8")
    original_bytes = env_path.read_bytes()
    _point_env(monkeypatch, env_path)

    state = SimpleNamespace()
    fail_next = {"value": True}

    async def fake_reload():
        handler.reload_calls += 1
        if fail_next["value"]:
            fail_next["value"] = False  # the rollback-triggered reload succeeds
            raise RuntimeError("insights boom")
        return SimpleNamespace(to_dict=lambda: {})

    state.reload_config = fake_reload
    monkeypatch.setattr(
        webui_server,
        "run_coro",
        lambda coro: asyncio.run(coro),
    )

    handler = _ConfigHandler({"TEMPERATURE": "0.7"})
    handler.webui_state = state
    handler._handle_config_update()

    assert handler.error is not None
    # The .env is restored to the previous valid content ...
    assert env_path.read_bytes() == original_bytes
    # ... and the rollback reloaded once to rebuild runtime components.
    assert handler.reload_calls == 2


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
