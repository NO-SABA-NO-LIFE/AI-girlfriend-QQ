"""数据源健康登记与 /hd2ping 自诊断报告。

各网络客户端在成功/失败路径上调用 :data:`HEALTH` 记录最近一次请求结果，
``/hd2ping`` 命令读取 :meth:`DiagnosticsRegistry.snapshot` 生成纯文本报告。

登记逻辑是纯内存旁路：同步、无锁（仅依赖 GIL 原子的计数/赋值）、自身永不
抛错，不会拖慢或影响任何主流程。时间戳一律使用 ``time.time()`` 墙钟时间。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Final
from urllib.parse import urlsplit

# 逻辑数据源 → 中文显示名；未知主机回退为 hostname 本身。
SOURCE_LABELS: Final[dict[str, str]] = {
    "hd2_api": "主API",
    "steam": "Steam",
    "companion": "Companion",
    "wiki": "Wiki",
    "hd2tool": "军需簿",
    "dionatha": "补充目录",
    "bilibili": "B站动态",
    "screenshot": "截图",
    "translate": "翻译",
}

# /hd2ping 报告的固定展示顺序；未登记的已知数据源显示为「暂无记录」。
EXPECTED_SOURCES: Final[tuple[str, ...]] = (
    "hd2_api",
    "companion",
    "steam",
    "bilibili",
    "wiki",
    "hd2tool",
    "dionatha",
    "screenshot",
    "translate",
)

# 主机名后缀 → 逻辑数据源。cdn.helldiverscompanion.com 必须排在
# helldiverscompanion.com 之前（后者是前者的后缀子串）。
_HOST_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("api.helldivers2.dev", "hd2_api"),
    ("api.steampowered.com", "steam"),
    ("steamcommunity.com", "steam"),
    ("helldiverscompanion.com", "companion"),
    ("helldivers.wiki.gg", "wiki"),
    ("raw.githubusercontent.com", "hd2tool"),
    ("gh-proxy.org", "hd2tool"),
    ("cdn.jsdelivr.net", "hd2tool"),
)


def key_for_host(url: str) -> str:
    """按 URL 主机名归出逻辑数据源 key，未知主机回退为 hostname。"""

    host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    for suffix, key in _HOST_KEYS:
        if host == suffix or host.endswith("." + suffix):
            return key
    return host or "unknown"


_DETAIL_MAX_CHARS: Final[int] = 56


def _clip(text: object, limit: int = _DETAIL_MAX_CHARS) -> str:
    """压平空白并截断 detail，防止单条异常信息撑爆报告。"""

    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


@dataclass
class HealthRecord:
    """单个数据源的累计健康状态。"""

    source: str
    ok_count: int = 0
    fail_count: int = 0
    consecutive_failures: int = 0
    last_ok_at: float | None = None
    last_fail_at: float | None = None
    last_ok_detail: str = ""
    last_fail_detail: str = ""
    last_latency_ms: float | None = None

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    def copy(self) -> "HealthRecord":
        return replace(self)


class DiagnosticsRegistry:
    """内存健康登记。``record_*`` 自吞异常，绝不影响调用方主流程。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._records: dict[str, HealthRecord] = {}
        self.started_at: float = time.time()

    # ---- 写入侧 ----

    def mark_boot(self) -> None:
        """插件启动时调用：清空计数并把运行时长归零。"""

        self.reset()

    def record_ok(
        self,
        source: str,
        *,
        detail: object = "",
        latency_ms: float | None = None,
    ) -> None:
        try:
            key = str(source or "unknown")
            record = self._records.setdefault(key, HealthRecord(source=key))
            record.ok_count += 1
            record.consecutive_failures = 0
            record.last_ok_at = time.time()
            record.last_ok_detail = _clip(detail)
            if latency_ms is not None:
                try:
                    record.last_latency_ms = max(0.0, float(latency_ms))
                except (TypeError, ValueError):
                    pass
        except Exception:  # pragma: no cover - 旁路逻辑绝不抛错
            pass

    def record_fail(self, source: str, *, detail: object = "") -> None:
        try:
            key = str(source or "unknown")
            record = self._records.setdefault(key, HealthRecord(source=key))
            record.fail_count += 1
            record.consecutive_failures += 1
            record.last_fail_at = time.time()
            record.last_fail_detail = _clip(detail)
        except Exception:  # pragma: no cover - 旁路逻辑绝不抛错
            pass

    # ---- 读取侧 ----

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def snapshot(self) -> dict[str, HealthRecord]:
        return {key: record.copy() for key, record in self._records.items()}


HEALTH: Final[DiagnosticsRegistry] = DiagnosticsRegistry()


def humanize_age(seconds: float | None) -> str:
    """秒数 → 「刚刚 / N分钟前 / N小时前 / N天前」；None → 从未。"""

    if seconds is None:
        return "从未"
    total = max(0, int(seconds))
    if total < 60:
        return "刚刚"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}小时前"
    return f"{hours // 24}天前"


def humanize_uptime(seconds: float) -> str:
    """运行时长 → 「N天N小时 / N小时N分 / N分钟」。"""

    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分钟"


def _status_symbol(record: HealthRecord) -> str:
    if record.ok_count == 0 and record.fail_count == 0:
        return "➖"
    if record.ok_count == 0:
        return "❌"
    if record.consecutive_failures >= 3:
        return "❌"
    if (
        record.last_fail_at is not None
        and record.last_ok_at is not None
        and record.last_fail_at > record.last_ok_at
    ):
        return "⚠️"
    return "✅"


def format_source_line(record: HealthRecord, *, now: float | None = None) -> str:
    """单条数据源状态行，如 ``✅ 主API 成功12/失败0 · 成功 2分钟前 · ...``"""

    now = time.time() if now is None else now
    symbol = _status_symbol(record)
    if symbol == "➖":
        return f"➖ {record.label} 暂无记录"

    counts = f"成功{record.ok_count}/失败{record.fail_count}"
    if record.consecutive_failures >= 2:
        counts += f"（连续失败{record.consecutive_failures}）"
    parts = [f"{symbol} {record.label} {counts}"]

    if (
        record.last_fail_at is not None
        and (record.last_ok_at is None or record.last_fail_at > record.last_ok_at)
    ):
        # 最近一次事件是失败：成功次数为 0 时明确标注「从未成功」
        if record.ok_count == 0:
            parts.append("从未成功")
        else:
            parts.append(f"失败 {humanize_age(now - record.last_fail_at)}")
        if record.last_fail_detail:
            parts.append(_clip(record.last_fail_detail))
        return " · ".join(parts)

    parts.append(f"成功 {humanize_age(now - record.last_ok_at)}")
    ok_detail = _clip(record.last_ok_detail) if record.last_ok_detail else ""
    if record.last_latency_ms is not None:
        ok_detail = (
            f"{ok_detail} · {record.last_latency_ms:.0f}ms"
            if ok_detail
            else f"{record.last_latency_ms:.0f}ms"
        )
    if ok_detail:
        parts.append(ok_detail)
    return " · ".join(parts)


# 展示排序：主 API 最前，其余按 EXPECTED_SOURCES 顺序，未知来源垫底。
_SOURCE_ORDER: Final[dict[str, int]] = {
    key: index for index, key in enumerate(EXPECTED_SOURCES)
}


def build_ping_report(
    sources: dict[str, HealthRecord],
    *,
    title: str,
    info_lines: list[str],
    cache_lines: list[str],
    expected_sources: tuple[str, ...] = EXPECTED_SOURCES,
    now: float | None = None,
) -> str:
    """组装 /hd2ping 纯文本报告。expected 中未登记的源显示「暂无记录」。"""

    now = time.time() if now is None else now
    merged = dict(sources)
    for key in expected_sources:
        merged.setdefault(key, HealthRecord(source=key))
    ordered = sorted(
        merged.values(),
        key=lambda r: (_SOURCE_ORDER.get(r.source, len(_SOURCE_ORDER)), r.source),
    )
    blocks: list[str] = [title, *info_lines]
    blocks.append(
        "数据源健康:\n" + "\n".join(format_source_line(r, now=now) for r in ordered)
    )
    if cache_lines:
        blocks.append("缓存:\n" + "\n".join(cache_lines))
    return "\n\n".join(blocks)
