"""Static zh-CN labels for Helldivers 2 UI surfaces.

The catalog is intentionally independent from renderers.  Renderers can use
``t("dashboard.title")`` directly or use one of the typed domain helpers for
values received from the API.
"""

from __future__ import annotations

from typing import Final, Literal, Mapping, TypeAlias

from .glossary import (
    ENEMY_NAMES,
    PLANET_NAMES as _PLANET_NAMES,
    SECTOR_NAMES,
    STRATAGEM_NAMES,
)


Locale: TypeAlias = Literal["zh-CN"]

# Keep this list explicit so editors and type checkers can discover the public
# catalog surface without importing a renderer.
LabelKey: TypeAlias = Literal[
    # Common UI
    "common.unknown",
    "common.none",
    "common.active",
    "common.inactive",
    "common.loading",
    "common.no_data",
    "common.error",
    "common.updated",
    "common.total",
    "common.details",
    "common.source",
    "common.remaining",
    "common.deadline",
    "common.location",
    "common.type",
    "common.status",
    "common.progress",
    "common.players",
    "common.online_players",
    "common.hours",
    "common.minutes",
    "common.days",
    "common.per_hour",
    "common.percent",
    # Dashboard
    "dashboard.title",
    "dashboard.subtitle",
    "dashboard.galactic_overview",
    "dashboard.urgent_liberations",
    "dashboard.defending",
    "dashboard.attacking",
    "dashboard.low_impact",
    "dashboard.no_campaigns",
    "dashboard.no_active_tactical_action",
    "dashboard.democracy_space_station",
    "dashboard.next_jump",
    # Statistics
    "stats.title",
    "stats.online_players",
    "stats.mission_stats",
    "stats.battle_stats",
    "stats.missions_won",
    "stats.missions_lost",
    "stats.total_time",
    "stats.bullets_fired",
    "stats.bullets_hit",
    "stats.accuracy",
    "stats.enemies_killed",
    "stats.deaths",
    "stats.planets_liberated",
    "stats.war_day",
    # DSS
    "dss.title",
    "dss.full_title",
    "dss.telemetry",
    "dss.tactical_actions",
    "dss.tactical_action",
    "dss.preparing",
    "dss.in_progress",
    "dss.cooldown",
    "dss.not_active",
    "dss.next_jump",
    "dss.preparation_progress",
    "dss.status_expiry",
    "dss.no_tactical_actions",
    # Map
    "map.title",
    "map.active_campaigns",
    "map.defense_campaigns",
    "map.events",
    "map.dss",
    "map.attacking",
    "map.legend_active",
    "map.legend_defense",
    "map.legend_event",
    "map.legend_dss",
    "map.legend_attack",
    "map.no_planets",
    # Global events
    "global_events.title",
    "global_events.briefing",
    "global_events.no_active",
    "global_events.event",
    "global_events.event_type",
    "global_events.faction",
    "global_events.ends",
    "global_events.victory",
    "global_events.defeat",
    "global_events.completed",
    # Steam
    "steam.title",
    "steam.update",
    "steam.news",
    "steam.no_updates",
    "steam.no_title",
    "steam.community_announcement",
    "steam.empty_body",
    "steam.view_full_post",
    "steam.page",
    # Warfront
    "warfront.title",
    "warfront.defense_event",
    "warfront.active_campaigns",
    "warfront.no_active",
    "warfront.campaign_count",
    "warfront.progress",
    "warfront.rate",
    "warfront.estimated_result",
    "warfront.liberation",
    "warfront.defense",
    # Awaiting orders
    "awaiting_orders.title",
    "awaiting_orders.message",
    "awaiting_orders.no_orders",
    "awaiting_orders.command",
    "awaiting_orders.further_instructions",
    # Statuses
    "status.unknown",
    "status.active",
    "status.inactive",
    "status.preparing",
    "status.in_progress",
    "status.cooldown",
    "status.completed",
    "status.victory",
    "status.defeat",
    "status.defending",
    "status.attacking",
    "status.awaiting",
    "status.stalled",
    # Factions
    "faction.terminids",
    "faction.automaton",
    "faction.illuminate",
    "faction.humans",
    "faction.super_earth",
    # Trend outcomes
    "trend.success",
    "trend.failure",
    "trend.advancing",
    "trend.losing",
    "trend.stalled",
    "trend.unknown",
    "trend.liberated",
    "trend.defense_secured",
    "trend.defense_failed",
    "trend.awaiting",
    # Units
    "unit.player",
    "unit.players",
    "unit.mission",
    "unit.missions",
    "unit.planet",
    "unit.planets",
    "unit.hour",
    "unit.hours",
    "unit.minute",
    "unit.minutes",
    "unit.day",
    "unit.days",
    "unit.percent",
    "unit.per_hour",
]


ZhCNLabels: TypeAlias = dict[LabelKey, str]


ZH_CN: Final[ZhCNLabels] = {
    # Common UI
    "common.unknown": "未知",
    "common.none": "无",
    "common.active": "活跃",
    "common.inactive": "未激活",
    "common.loading": "加载中",
    "common.no_data": "暂无数据",
    "common.error": "数据获取失败",
    "common.updated": "更新时间",
    "common.total": "共计",
    "common.details": "详情",
    "common.source": "数据来源",
    "common.remaining": "剩余",
    "common.deadline": "截止时间",
    "common.location": "位置",
    "common.type": "类型",
    "common.status": "状态",
    "common.progress": "进度",
    "common.players": "玩家",
    "common.online_players": "在线玩家",
    "common.hours": "小时",
    "common.minutes": "分钟",
    "common.days": "天",
    "common.per_hour": "%/小时",
    "common.percent": "%",
    # Dashboard
    "dashboard.title": "银河战争总览",
    "dashboard.subtitle": "银河战争总览",
    "dashboard.galactic_overview": "银河总览",
    "dashboard.urgent_liberations": "紧急解放行动",
    "dashboard.defending": "防御中",
    "dashboard.attacking": "进攻中",
    "dashboard.low_impact": "低影响战役",
    "dashboard.no_campaigns": "当前没有相关战役",
    "dashboard.no_active_tactical_action": "无进行中战术行动",
    "dashboard.democracy_space_station": "民主空间站",
    "dashboard.next_jump": "下次跃迁",
    # Statistics
    "stats.title": "银河战争统计",
    "stats.online_players": "在线玩家",
    "stats.mission_stats": "任务统计",
    "stats.battle_stats": "战斗统计",
    "stats.missions_won": "胜利任务",
    "stats.missions_lost": "失败任务",
    "stats.total_time": "总时长",
    "stats.bullets_fired": "发射子弹",
    "stats.bullets_hit": "命中子弹",
    "stats.accuracy": "命中率",
    "stats.enemies_killed": "消灭敌人",
    "stats.deaths": "阵亡次数",
    "stats.planets_liberated": "解放星球",
    "stats.war_day": "战争日",
    # DSS
    "dss.title": "DSS",
    "dss.full_title": "民主空间站 · DSS",
    "dss.telemetry": "民主空间站遥测",
    "dss.tactical_actions": "战术行动",
    "dss.tactical_action": "战术行动",
    "dss.preparing": "准备中",
    "dss.in_progress": "进行中",
    "dss.cooldown": "冷却中",
    "dss.not_active": "未激活",
    "dss.next_jump": "下次跃迁",
    "dss.preparation_progress": "准备进度",
    "dss.status_expiry": "状态结束",
    "dss.no_tactical_actions": "当前没有战术行动。",
    # Map
    "map.title": "银河战争地图",
    "map.active_campaigns": "活跃战役",
    "map.defense_campaigns": "防御战役",
    "map.events": "事件",
    "map.dss": "DSS",
    "map.attacking": "进攻",
    "map.legend_active": "活跃",
    "map.legend_defense": "防御",
    "map.legend_event": "事件",
    "map.legend_dss": "DSS",
    "map.legend_attack": "进攻",
    "map.no_planets": "暂无星球数据",
    # Global events
    "global_events.title": "全球事件",
    "global_events.briefing": "事件简报",
    "global_events.no_active": "当前没有活跃的全球事件。",
    "global_events.event": "事件",
    "global_events.event_type": "事件类型",
    "global_events.faction": "事件阵营",
    "global_events.ends": "事件结束",
    "global_events.victory": "胜利",
    "global_events.defeat": "失败",
    "global_events.completed": "完成",
    # Steam
    "steam.title": "Steam 更新日志",
    "steam.update": "Steam 更新",
    "steam.news": "Steam 新闻",
    "steam.no_updates": "当前没有 Steam 更新日志",
    "steam.no_title": "无标题",
    "steam.community_announcement": "Steam 社区公告",
    "steam.empty_body": "公告正文为空",
    "steam.view_full_post": "完整内容请查看 Steam 页面",
    "steam.page": "第 {page}/{pages} 页",
    # Warfront
    "warfront.title": "战线战况",
    "warfront.defense_event": "防御 / 事件",
    "warfront.active_campaigns": "活跃战役",
    "warfront.no_active": "当前没有该阵营的活跃战役。",
    "warfront.campaign_count": "共 {count} 条活跃战役",
    "warfront.progress": "进度",
    "warfront.rate": "净推进率",
    "warfront.estimated_result": "预计结果",
    "warfront.liberation": "解放",
    "warfront.defense": "防御",
    # Awaiting orders
    "awaiting_orders.title": "最高命令",
    "awaiting_orders.message": "等待超级地球最高指挥部的进一步指示",
    "awaiting_orders.no_orders": "当前没有活跃的最高命令",
    "awaiting_orders.command": "命令",
    "awaiting_orders.further_instructions": "等待进一步指示",
    # Statuses
    "status.unknown": "未知",
    "status.active": "活跃",
    "status.inactive": "未激活",
    "status.preparing": "准备中",
    "status.in_progress": "进行中",
    "status.cooldown": "冷却中",
    "status.completed": "已完成",
    "status.victory": "胜利",
    "status.defeat": "失败",
    "status.defending": "防御中",
    "status.attacking": "进攻中",
    "status.awaiting": "等待中",
    "status.stalled": "停滞",
    # Factions
    "faction.terminids": "终结族",
    "faction.automaton": "机器人",
    "faction.illuminate": "光能者",
    "faction.humans": "人类",
    "faction.super_earth": "超级地球",
    # Trend outcomes
    "trend.success": "成功",
    "trend.failure": "失败",
    "trend.advancing": "推进中",
    "trend.losing": "失守风险",
    "trend.stalled": "停滞",
    "trend.unknown": "等待趋势",
    "trend.liberated": "已解放",
    "trend.defense_secured": "防御成功",
    "trend.defense_failed": "防御失败",
    "trend.awaiting": "等待趋势数据",
    # Units
    "unit.player": "玩家",
    "unit.players": "玩家",
    "unit.mission": "任务",
    "unit.missions": "任务",
    "unit.planet": "星球",
    "unit.planets": "星球",
    "unit.hour": "小时",
    "unit.hours": "小时",
    "unit.minute": "分钟",
    "unit.minutes": "分钟",
    "unit.day": "天",
    "unit.days": "天",
    "unit.percent": "%",
    "unit.per_hour": "%/小时",
}

# Public aliases make the intended default locale explicit while preserving a
# simple module-level lookup for callers that do not need locale negotiation.
DEFAULT_LOCALE: Final[Locale] = "zh-CN"
LABELS: Final[Mapping[LabelKey, str]] = ZH_CN
ZH_CN_LABELS: Final[Mapping[LabelKey, str]] = ZH_CN


_FACTION_ALIASES: Final[dict[str, LabelKey]] = {
    "terminids": "faction.terminids",
    "terminid": "faction.terminids",
    "bugs": "faction.terminids",
    "bug": "faction.terminids",
    "终结族": "faction.terminids",
    "虫族": "faction.terminids",
    "虫子": "faction.terminids",
    "automaton": "faction.automaton",
    "automatons": "faction.automaton",
    "bots": "faction.automaton",
    "机器人": "faction.automaton",
    "铁卫": "faction.automaton",
    "illuminate": "faction.illuminate",
    "illuminates": "faction.illuminate",
    "squids": "faction.illuminate",
    "光能": "faction.illuminate",
    "光能者": "faction.illuminate",
    "humans": "faction.humans",
    "human": "faction.humans",
    "super earth": "faction.super_earth",
    "super_earth": "faction.super_earth",
    "人类": "faction.humans",
    "超级地球": "faction.super_earth",
}

_STATUS_ALIASES: Final[dict[str, LabelKey]] = {
    "active": "status.active",
    "in_progress": "status.in_progress",
    "in progress": "status.in_progress",
    "running": "status.in_progress",
    "preparing": "status.preparing",
    "ready": "status.preparing",
    "cooldown": "status.cooldown",
    "cooling_down": "status.cooldown",
    "inactive": "status.inactive",
    "not_active": "status.inactive",
    "completed": "status.completed",
    "complete": "status.completed",
    "victory": "status.victory",
    "success": "status.victory",
    "defeat": "status.defeat",
    "failure": "status.defeat",
    "defending": "status.defending",
    "defense": "status.defending",
    "attacking": "status.attacking",
    "attack": "status.attacking",
    "awaiting": "status.awaiting",
    "waiting": "status.awaiting",
    "unknown": "status.unknown",
}

_TREND_ALIASES: Final[dict[str, LabelKey]] = {
    "success": "trend.success",
    "succeeded": "trend.success",
    "failure": "trend.failure",
    "failed": "trend.failure",
    "advancing": "trend.advancing",
    "advancing_liberation": "trend.advancing",
    "losing": "trend.losing",
    "losing_ground": "trend.losing",
    "stalled": "trend.stalled",
    "unknown": "trend.unknown",
    "awaiting": "trend.awaiting",
    "awaiting_trend": "trend.awaiting",
    "liberated": "trend.liberated",
    "defense_secured": "trend.defense_secured",
    "defense_failed": "trend.defense_failed",
}


# Numeric DSS statuses match the API values used by war_state.py.
_DSS_STATUS_KEYS: Final[dict[int, LabelKey]] = {
    0: "status.inactive",
    1: "status.preparing",
    2: "status.in_progress",
    3: "status.cooldown",
}

# Official Simplified Chinese planet/enemy/sector/stratagem names now live in
# ``assets/glossary/*.json`` and are loaded by :mod:`glossary`. The four tables
# are re-exported here so existing call sites keep working.
ENEMY_NAMES: Final[dict[str, str]] = ENEMY_NAMES
SECTOR_NAMES: Final[dict[str, str]] = SECTOR_NAMES
STRATAGEM_NAMES: Final[dict[str, str]] = STRATAGEM_NAMES

# DSS tactical action names — keep as a small explicit alias so format_dss_action
# and the icon-by-name mapping in services.py resolve consistently.
_DSS_ACTION_NAMES: Final[dict[str, str]] = {
    "eagle storm": "飞鹰风暴",
    "orbital blockade": "轨道封锁",
    "heavy ordnance distribution": "重型军械分发",
}


def t(key: LabelKey | str, default: str | None = None) -> str:
    """Return a zh-CN label, using ``default`` or the key when unknown."""

    value = ZH_CN.get(key)  # type: ignore[arg-type]
    if value is not None:
        return value
    return default if default is not None else str(key)


def format_label(key: LabelKey | str, default: str | None = None) -> str:
    """Format any catalog key through the default zh-CN locale."""

    return t(key, default)


def format_common_label(name: object, default: str | None = None) -> str:
    """Translate a common label name such as ``online_players`` or ``status``."""

    token = _token(name)
    key = token if token.startswith("common.") else f"common.{token}"
    return t(key, default or str(name) if name not in (None, "") else t("common.unknown"))


def _token(value: object) -> str:
    return str(value).strip().casefold() if value is not None else ""


def format_faction(value: object, default: str | None = None) -> str:
    """Translate a canonical faction name or a known API/user alias."""

    token = _token(value)
    key = _FACTION_ALIASES.get(token)
    return t(key, default or str(value) if value not in (None, "") else t("common.unknown")) if key else (default or str(value) if value not in (None, "") else t("common.unknown"))


def format_status(value: object, default: str | None = None) -> str:
    """Translate common status tokens, including numeric DSS statuses."""

    if isinstance(value, bool):
        key = "status.active" if value else "status.inactive"
    else:
        try:
            key = _DSS_STATUS_KEYS.get(int(value)) if value is not None else None
        except (TypeError, ValueError):
            key = None
        if key is None:
            key = _STATUS_ALIASES.get(_token(value))
    return t(key, default or str(value) if value not in (None, "") else t("status.unknown")) if key else (default or str(value) if value not in (None, "") else t("status.unknown"))


def format_planet(value: object, default: str | None = None) -> str:
    """Translate an API planet name to its official Simplified Chinese name."""

    official = lookup_planet_name(value)
    if official:
        return official
    if value not in (None, ""):
        return str(value)
    return default or t("common.unknown")


def lookup_planet_name(value: object) -> str | None:
    """Return an official planet translation, without treating an unknown name as one."""

    return _PLANET_NAMES.get(_token(value))


def known_planet_names() -> tuple[str, ...]:
    """Return source names covered by the bundled official localization table."""

    return tuple(_PLANET_NAMES)


def format_dss_action(value: object, default: str | None = None) -> str:
    """Translate the three official DSS tactical action names when present."""

    token = _token(value)
    return _DSS_ACTION_NAMES.get(
        token, default or str(value) if value not in (None, "") else t("dss.tactical_action")
    )


def format_unit(unit: object, value: object | None = None) -> str:
    """Translate a unit name, optionally returning ``value + unit``."""

    token = _token(unit)
    aliases = {
        "player": "unit.player",
        "players": "unit.players",
        "mission": "unit.mission",
        "missions": "unit.missions",
        "planet": "unit.planet",
        "planets": "unit.planets",
        "hour": "unit.hour",
        "hours": "unit.hours",
        "minute": "unit.minute",
        "minutes": "unit.minutes",
        "day": "unit.day",
        "days": "unit.days",
        "percent": "unit.percent",
        "%": "unit.percent",
        "per_hour": "unit.per_hour",
    }
    label = t(aliases.get(token, "unit." + token), str(unit))
    return f"{value}{label}" if value is not None else label


def format_trend_state(value: object, default: str | None = None) -> str:
    """Translate a machine-readable campaign trend state."""

    key = _TREND_ALIASES.get(_token(value))
    return t(key, default or str(value) if value not in (None, "") else t("trend.unknown")) if key else (default or str(value) if value not in (None, "") else t("trend.unknown"))


def format_trend_result(
    state: object,
    *,
    remaining: str | None = None,
    projected: float | None = None,
    defense: bool = False,
) -> str:
    """Format a trend outcome and optional duration/projection details."""

    token = _token(state)
    if defense and token in {"success", "succeeded", "defense_secured"}:
        label = t("trend.defense_secured")
    elif defense and token in {"failure", "failed", "defense_failed"}:
        label = t("trend.defense_failed")
    elif token == "liberated":
        label = t("trend.liberated")
    else:
        label = format_trend_state(state)
    details: list[str] = []
    if remaining:
        details.append(str(remaining))
    if projected is not None:
        details.append(f"{float(projected):.1f}{t('unit.percent')}")
    return f"{label} · {' · '.join(details)}" if details else label


# Friendly aliases for callers that prefer the domain name in the function.
format_faction_label = format_faction
format_status_label = format_status
format_trend_status = format_trend_state
format_trend_label = format_trend_state


__all__ = [
    "DEFAULT_LOCALE",
    "LABELS",
    "LabelKey",
    "Locale",
    "ZH_CN",
    "ZH_CN_LABELS",
    "ZhCNLabels",
    "format_common_label",
    "format_faction",
    "format_dss_action",
    "format_label",
    "format_faction_label",
    "format_status",
    "format_status_label",
    "format_planet",
    "lookup_planet_name",
    "known_planet_names",
    "format_trend_label",
    "format_trend_result",
    "format_trend_state",
    "format_trend_status",
    "format_unit",
    "t",
]
