"""Persona engine assembly and compatibility imports."""

from __future__ import annotations

from ..group_chat import GROUP_PROMPT as GROUP_PROMPT
from ..lore import ATRI_LORE_PROMPT as ATRI_LORE_PROMPT
from .engine_context import EngineContextMixin
from .engine_proactive import EngineProactiveMixin
from .engine_reply import EngineReplyMixin
from .engine_state import EngineStateMixin
from .engine_tools import EngineToolsMixin
from .prompt_rules import (
    FAILURE_TEMPLATE_PATTERNS as FAILURE_TEMPLATE_PATTERNS,
    PERSONA_PROMPT as PERSONA_PROMPT,
    _has_current_visual_context as _has_current_visual_context,
    _visual_fail_safe_context as _visual_fail_safe_context,
    has_prompt_leak as has_prompt_leak,
)
from .reply_fallbacks import (
    _canonical_evidence_url as _canonical_evidence_url,
    _looks_incomplete_reply as _looks_incomplete_reply,
    _looks_like_tool_schema_error as _looks_like_tool_schema_error,
    _persona_repair_fallback as _persona_repair_fallback,
    _persona_violations as _persona_violations,
    _provider_payload_overrides as _provider_payload_overrides,
    _source_matches_receipts as _source_matches_receipts,
    _tool_receipt_urls as _tool_receipt_urls,
)
from .reply_rules import (
    _group_fallback_reply as _group_fallback_reply,
    _needs_proactive_grounding_repair as _needs_proactive_grounding_repair,
    _needs_topic_guidance_repair as _needs_topic_guidance_repair,
    _normalize_reply as _normalize_reply,
)


class AtriReplyEngine(
    EngineStateMixin,
    EngineProactiveMixin,
    EngineReplyMixin,
    EngineToolsMixin,
    EngineContextMixin,
):
    """ATRI 的回复编排层。

    它把人格提示词、当前会话、三层记忆、认知状态、工具结果和输出护栏
    组装成一次模型请求；不直接发送 QQ 消息，发送由 OneBot 层负责。
    """
