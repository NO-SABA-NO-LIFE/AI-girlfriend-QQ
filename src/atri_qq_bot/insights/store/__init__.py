"""SQLite 持久层。

每个方法使用短连接，配合 WAL 和 busy_timeout，可安全地被 asyncio 工作线程与
WebUI 的 ThreadingHTTPServer 同时调用；模块不依赖 OneBot 或 WebUI。
"""

from .common import utc_now as utc_now
from .delivery import DeliveryMixin
from .evaluations import EvaluationsMixin
from .groups import GroupsMixin
from .state import StateMixin
from .summaries import SummariesMixin


class InsightsStore(StateMixin, EvaluationsMixin, DeliveryMixin, GroupsMixin, SummariesMixin):
    """Stable SQLite interface assembled from domain-specific implementations."""


__all__ = ["InsightsStore", "utc_now"]
