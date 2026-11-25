from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from atri_qq_bot.config import BotConfig
from atri_qq_bot.toolbox import (
    ToolAnalyzer,
    _requests_full_text_transcription,
    _visual_prompt_for_kind,
)
from atri_qq_bot.toolbox_parts.analysis_result import ToolAnalysisResult


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


def _png_bytes() -> bytes:
    source = io.BytesIO()
    Image.new("RGB", (320, 180), "white").save(source, format="PNG")
    return source.getvalue()


# --- 意图检测：明确要全文/翻译才触发，默认 fail-closed ---

FULL_TEXT_CASES = [
    "翻译一下",
    "把图里的英文翻译成中文",
    "全文识别",
    "逐字读出来",
    "这上面写的啥",
    "帮我识别图里的文字",
    "读一下",
    "完整翻译",
    "图里有什么字",
]

BRIEF_CASES = [
    "这个表情包好好笑",
    "你看",
    "哈哈哈哈",
    "？？？",
    "早上好",
    "这图绝了",
]


def test_detects_full_text_intent() -> None:
    for question in FULL_TEXT_CASES:
        assert _requests_full_text_transcription(question), question


def test_brief_messages_do_not_trigger_full_text() -> None:
    for question in BRIEF_CASES:
        assert not _requests_full_text_transcription(question), question


def test_empty_question_is_not_full_text() -> None:
    assert not _requests_full_text_transcription("")


# --- 视觉模型 prompt 随模式切换 ---

def test_full_text_prompt_requires_complete_text() -> None:
    prompt = _visual_prompt_for_kind("image", "帮我翻译一下")
    assert "按原文完整列出图中文字" in prompt
    assert "60 字" not in prompt


def test_brief_prompt_keeps_sixty_char_limit() -> None:
    prompt = _visual_prompt_for_kind("image", "你看这个")
    assert "控制在 60 字以内" in prompt


def test_explicit_full_text_flag_overrides_detection() -> None:
    # 即使文字本身不含全文指令，显式传 True 也进入全文模式。
    forced = _visual_prompt_for_kind("image", "你看", full_text=True)
    assert "按原文完整列出图中文字" in forced
    # 显式 False 时，即便含“翻译”也保持精简，便于上层精确控制。
    suppressed = _visual_prompt_for_kind("image", "翻译一下", full_text=False)
    assert "控制在 60 字以内" in suppressed


# --- 对话上下文规则 ---

def test_prompt_context_includes_full_text_rule() -> None:
    result = ToolAnalysisResult(
        "image",
        "casual",
        findings=["独立 OCR 文字：hello world"],
        full_text_requested=True,
    )
    context = result.prompt_context()
    assert "全文转写要求" in context
    assert "逐字完整输出" in context


def test_prompt_context_omits_rule_by_default() -> None:
    result = ToolAnalysisResult("image", "casual")
    assert "全文转写要求" not in result.prompt_context()


# --- 端到端：全文模式标记置位，并放宽 OCR 截断 ---

def _install_fakes(monkeypatch, analyzer, long_text: str) -> None:
    async def fake_ocr(data: bytes):
        return SimpleNamespace(
            text=long_text,
            average_confidence=0.97,
            line_count=len(long_text.splitlines()),
            error="",
        )

    async def fake_vision(data: bytes, source_name: str, prompt: str | None = None):
        return (
            (
                "直接回答：用户要读文字；图中文字：占位；人物/主体：未确认；"
                "身份/出处：未确认；表情/情绪：中性；表达含义/社交意图：读取文字；"
                "依据：OCR；置信度：高；需要联网核验：否；检索关键词："
            ),
            "",
        )

    monkeypatch.setattr(analyzer, "_extract_image_ocr", fake_ocr)
    monkeypatch.setattr(analyzer, "_analyze_image_with_vision", fake_vision)


def test_full_text_mode_keeps_long_ocr_and_sets_flag(monkeypatch, tmp_path) -> None:
    analyzer = ToolAnalyzer(_config(tmp_path))
    analyzer.ocr_enabled = True
    long_text = "abcdefgh" * 250  # 2000 字符，超过普通模式 1600 的截断
    _install_fakes(monkeypatch, analyzer, long_text)

    result = asyncio.run(
        analyzer._analyze_image_bytes(
            _png_bytes(),
            "doc.png",
            full_text=True,
        )
    )

    assert result.full_text_requested is True
    context = result.prompt_context()
    assert long_text in context  # 完整 OCR 未被截断
    assert "全文转写要求" in context


def test_brief_mode_truncates_long_ocr(monkeypatch, tmp_path) -> None:
    analyzer = ToolAnalyzer(_config(tmp_path))
    analyzer.ocr_enabled = True
    long_text = "abcdefgh" * 250
    _install_fakes(monkeypatch, analyzer, long_text)

    result = asyncio.run(
        analyzer._analyze_image_bytes(
            _png_bytes(),
            "doc.png",
            full_text=False,
        )
    )

    assert result.full_text_requested is False
    assert long_text not in result.prompt_context()  # 普通模式仍截断
