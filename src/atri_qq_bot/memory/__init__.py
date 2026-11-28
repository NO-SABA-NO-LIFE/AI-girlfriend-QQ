from __future__ import annotations

import json as json

from .extraction import MemoryExtractionMixin
from .extraction_rules import is_memory_pollution_text
from .ingestion import (
    MemoryExtractionRequest,
    MemoryExtractionResult,
    MemoryExtractionWorker,
)
from .observation import MemoryObservationMixin
from .proactive import MemoryProactiveMixin
from .state import MemoryStateMixin


class UserMemoryStore(
    MemoryStateMixin,
    MemoryObservationMixin,
    MemoryProactiveMixin,
    MemoryExtractionMixin,
):
    """对外保持原有记忆存储入口，内部按职责拆分实现。"""


__all__ = [
    "UserMemoryStore",
    "is_memory_pollution_text",
    "MemoryExtractionRequest",
    "MemoryExtractionResult",
    "MemoryExtractionWorker",
]
