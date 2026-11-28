from __future__ import annotations

import time as time

from .automation import QZoneAutomationMixin
from .rules import (
    DEFAULT_SETTINGS as DEFAULT_SETTINGS,
    DEFAULT_TOPIC_ROTATION as DEFAULT_TOPIC_ROTATION,
    LEGACY_TOPIC_ROTATION as LEGACY_TOPIC_ROTATION,
    content_quality_issues as content_quality_issues,
    next_window_time as next_window_time,
    parse_keywords as parse_keywords,
    sanitize_qzone_fact_context as sanitize_qzone_fact_context,
    schedule_times as schedule_times,
    topic_for_schedule as topic_for_schedule,
    topic_prompt as topic_prompt,
)
from .store import QZoneAutomationStore


class QZoneAutomation(QZoneAutomationMixin):
    """Stable automation interface assembled from store and scheduling modules."""


__all__ = ["QZoneAutomationStore", "QZoneAutomation", "content_quality_issues", "next_window_time", "schedule_times"]
