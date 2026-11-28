from dataclasses import replace
from pathlib import Path

from atri_qq_bot.config import BotConfig
from atri_qq_bot.config.loader import load_config
from atri_qq_bot.toolbox import ToolAnalyzer


def _config(tmp_path: Path) -> BotConfig:
    return BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="smart",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4.1-mini",
        temperature=0.8,
        max_tokens=350,
        memory_path=tmp_path / "memory.json",
    )


def test_runtime_accepts_90_second_vision_timeout(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        toolbox_vision_enabled=True,
        toolbox_vision_timeout_seconds=90.0,
    )

    analyzer = ToolAnalyzer(config)

    assert analyzer.vision_timeout_seconds == 90.0


def test_runtime_clamps_timeout_bounds(tmp_path: Path) -> None:
    too_large = replace(
        _config(tmp_path),
        toolbox_vision_enabled=True,
        toolbox_vision_timeout_seconds=120.0,
    )
    assert ToolAnalyzer(too_large).vision_timeout_seconds == 90.0

    too_small = replace(
        _config(tmp_path),
        toolbox_vision_enabled=True,
        toolbox_vision_timeout_seconds=5.0,
    )
    assert ToolAnalyzer(too_small).vision_timeout_seconds == 20.0


def test_env_loader_accepts_90_and_clamps_higher() -> None:
    config = load_config(overrides={"TOOLBOX_VISION_TIMEOUT_SECONDS": "90"})
    assert config.toolbox_vision_timeout_seconds == 90.0

    clamped = load_config(overrides={"TOOLBOX_VISION_TIMEOUT_SECONDS": "120"})
    assert clamped.toolbox_vision_timeout_seconds == 90.0
