"""群聊归档分析、话题聚类和结构化总结。"""

from ..llm import StructuredLLMClient as StructuredLLMClient
from .preprocessing import (
    _activity_profile as _activity_profile,
    _statistics as _statistics,
)
from .prompts import (
    _fallback_report as _fallback_report,
    _summary_prompt as _summary_prompt,
)
from .service import (
    GroupSummaryService as GroupSummaryService,
    summary_markdown as summary_markdown,
)
from .topics import (
    _fallback_clusters as _fallback_clusters,
    _message_chunks as _message_chunks,
    _normalize_topics as _normalize_topics,
    _topic_payload as _topic_payload,
    _topic_signal as _topic_signal,
)

__all__ = ["GroupSummaryService", "summary_markdown"]
