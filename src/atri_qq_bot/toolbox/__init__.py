from __future__ import annotations

import asyncio as asyncio
import time as time

from ..toolbox_parts.analysis_result import ToolAnalysisResult, _VisionModelResponse
from .documents import ToolboxDocumentsMixin
from .helpers import (
    _demote_unverified_visual_identity as _demote_unverified_visual_identity,
    _is_complete_visual_analysis as _is_complete_visual_analysis,
    _normalize_visual_model_output as _normalize_visual_model_output,
    _prepare_images_for_vision as _prepare_images_for_vision,
    _recover_visual_json_fields as _recover_visual_json_fields,
    _requests_full_text_transcription as _requests_full_text_transcription,
    _search_result_supports_query as _search_result_supports_query,
    _visual_prompt_for_kind as _visual_prompt_for_kind,
    _visual_search_query as _visual_search_query,
)
from .request import ToolboxRequestMixin
from .state import ToolboxStateMixin
from .video import ToolboxVideoMixin
from .vision import ToolboxVisionMixin


class ToolAnalyzer(
    ToolboxStateMixin,
    ToolboxRequestMixin,
    ToolboxDocumentsMixin,
    ToolboxVisionMixin,
    ToolboxVideoMixin,
):
    """对外保持原有材料分析入口，内部按材料类型拆分实现。"""


__all__ = [
    "ToolAnalyzer",
    "ToolAnalysisResult",
    "_VisionModelResponse",
]
