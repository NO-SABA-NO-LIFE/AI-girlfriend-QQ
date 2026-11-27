from __future__ import annotations

from unittest import mock

import pytest

import atri_shared.config_admin as config_admin
import atri_qq_bot.__main__ as main_module
import atri_qq_bot.config.loader as loader


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
