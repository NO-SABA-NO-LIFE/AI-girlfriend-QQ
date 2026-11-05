"""消息格式化：stats / order / news / steam。"""

from __future__ import annotations

import re
from typing import Any

from .._logging import logger

from .text_utils import (
    clean_game_text,
    dispatch_type_name,
    format_expires_in,
    format_iso_time,
    format_number,
    format_percentage,
    format_time_hours,
    truncate_at_boundary,
)


def format_stats_message(
    war_summary: dict[str, Any] | None,
    war_v1: dict[str, Any] | None = None,
) -> str:
    try:
        statistics: dict[str, Any]
        impact_multiplier = 0.0
        player_count = 0

        if war_summary and isinstance(war_summary, dict):
            if "statistics" in war_summary:
                statistics = war_summary.get("statistics") or {}
                impact_multiplier = war_summary.get("impactMultiplier", 0) or 0
                player_count = statistics.get("playerCount", 0) or 0
            else:
                statistics = war_summary
                impact_multiplier = war_summary.get("impactMultiplier", 0) or 0
                player_count = war_summary.get("playerCount", 0) or 0
        else:
            statistics = {}

        if war_v1 and isinstance(war_v1, dict):
            # playerCount / impactMultiplier 以 war v1 为准（原项目行为）
            v1_impact = war_v1.get("impactMultiplier", None)
            if v1_impact is not None:
                impact_multiplier = v1_impact
            v1_stats = war_v1.get("statistics") or {}
            v1_players = v1_stats.get("playerCount", None)
            if v1_players is None:
                v1_players = war_v1.get("playerCount", None)
            if v1_players is not None:
                player_count = v1_players
            # 用 v1 补齐 summary 缺失字段
            for key, val in v1_stats.items():
                if key not in statistics or not statistics.get(key):
                    statistics[key] = val

        terminid_kills = statistics.get("terminidKills", 0) or statistics.get(
            "bugKills", 0
        )
        automaton_kills = statistics.get("automatonKills", 0)
        illuminate_kills = statistics.get("illuminateKills", 0)
        deaths = statistics.get("deaths", 0)
        friendlies = statistics.get("friendlies", 0)

        try:
            impact_str = f"{float(impact_multiplier):.6f}"
        except (TypeError, ValueError):
            impact_str = str(impact_multiplier)

        lines = [
            "📊 银河战争统计 | HELLDIVERS 2",
            "-------------",
            "🌌战争信息",
            f"▎在线玩家: {format_number(player_count)}",
            f"▎影响系数: {impact_str}",
            f"▎发射子弹: {format_number(statistics.get('bulletsFired', 0))}",
            f"▎冻肉储备数: {format_number(friendlies)}",
            "-------------",
            "📜任务统计",
            f"▎胜利任务: {format_number(statistics.get('missionsWon', 0))}",
            f"▎失败任务: {format_number(statistics.get('missionsLost', 0))}",
            f"▎成功率: {format_percentage(statistics.get('missionSuccessRate', 0))}",
            f"▎总任务时间: {format_time_hours(statistics.get('timePlayed', 0))}",
            "-------------",
            "⚔️战斗统计",
            f"▎终结族击杀: {format_number(terminid_kills)}",
            f"▎机器人击杀: {format_number(automaton_kills)}",
            f"▎光能者击杀: {format_number(illuminate_kills)}",
            f"▎阵亡次数: {format_number(deaths)}",
            f"▎TK伤亡: {format_number(friendlies)}",
            "-------------",
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("[HD2] 格式化失败")
        return "❌ 数据格式化失败，请稍后重试。"


def format_order_message(
    order: dict[str, Any],
    *,
    index: int = 1,
    title: str | None = None,
    brief: str | None = None,
    task_desc: str | None = None,
) -> str:
    try:
        setting = order.get("setting") or {}
        title = clean_game_text(
            title if title is not None else setting.get("overrideTitle", "未知命令")
        )
        brief = clean_game_text(
            brief if brief is not None else setting.get("overrideBrief", "")
        )
        task_desc = clean_game_text(
            task_desc if task_desc is not None else setting.get("taskDescription", "")
        )

        lines = [
            f"📋 最高命令 {index} | HELLDIVERS 2",
            "-------------",
            f"▎命令: {title or '未知命令'}",
        ]
        if brief:
            lines.append(f"▎简介: {brief}")
        if task_desc:
            lines.append(f"▎任务: {task_desc}")

        tasks = setting.get("tasks") or []
        progress = order.get("progress") or []
        for i, task in enumerate(tasks):
            if i >= len(progress):
                break
            task_values = task.get("values") or []
            if len(task_values) <= 2:
                continue
            current_progress = progress[i]
            target = task_values[2]
            try:
                target_num = float(target)
                current_num = float(current_progress)
            except (TypeError, ValueError):
                continue
            if target_num <= 0:
                continue
            percentage = (current_num / target_num) * 100
            if percentage >= 10:
                pct = f"{percentage:.1f}%"
            elif percentage >= 1:
                pct = f"{percentage:.2f}%"
            else:
                pct = f"{percentage:.3f}%"
            lines.append(
                f"▎任务{i + 1}进度: {pct} ({format_number(int(current_num))} / {format_number(int(target_num))})"
            )

        expires = format_expires_in(order.get("expiresIn", 0) or 0)
        if expires:
            lines.append(f"▎剩余时间: {expires}")

        reward = setting.get("reward") or {}
        amount = reward.get("amount", 0) or 0
        if amount:
            lines.append(f"▎奖励: {format_number(amount)}奖章")

        lines.append("-------------")
        lines.append("执行命令，为了超级地球！🌍")
        return "\n".join(lines)
    except Exception:
        logger.exception("[HD2] 格式化失败")
        return "❌ 数据格式化失败，请稍后重试。"


def format_news_message(
    dispatch: dict[str, Any],
    *,
    index: int = 1,
    message_text: str | None = None,
) -> str:
    try:
        dispatch_id = dispatch.get("id", 0)
        published_time = format_iso_time(dispatch.get("published", ""))
        dtype = dispatch_type_name(dispatch.get("type", 0))
        content = clean_game_text(
            message_text
            if message_text is not None
            else dispatch.get("message", "无内容")
        )

        lines = [
            f"📰 快讯 {index} | HELLDIVERS 2",
            "-------------",
            f"▎类型: {dtype}",
            f"▎编号: #{dispatch_id}",
            f"▎时间: {published_time}",
            f"▎内容: {content}",
            "-------------",
            "使用 /news [1-5] 可以查看其他快讯！🌍",
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("[HD2] 格式化失败")
        return "❌ 数据格式化失败，请稍后重试。"


def _truncate_steam_section(
    content: str,
    *,
    max_length: int,
    balancing_limit: int,
    fixes_limit: int,
    issues_limit: int,
    max_sections: int,
) -> str:
    if not content:
        return content
    if len(content) <= max_length:
        return clean_game_text(content)

    sections: list[str] = []

    balancing_start = content.find("⚖️")
    if balancing_start == -1:
        balancing_start = content.lower().find("balancing")
    if balancing_start != -1:
        next_section = content.find("[h2]", balancing_start + 10)
        balancing_content = (
            content[balancing_start:]
            if next_section == -1
            else content[balancing_start:next_section]
        )
        cleaned = clean_game_text(balancing_content)
        if len(cleaned) > balancing_limit:
            cleaned = truncate_at_boundary(cleaned, balancing_limit)
        sections.append(f"⚖️ 平衡性调整\n{'-' * 15}\n{cleaned}")

    fixes_start = content.find("🔧")
    if fixes_start == -1:
        fixes_start = content.lower().find("fixes")
    if fixes_start != -1:
        next_section = content.find("[h2]", fixes_start + 10)
        fixes_content = (
            content[fixes_start:]
            if next_section == -1
            else content[fixes_start:next_section]
        )
        cleaned = clean_game_text(fixes_content)
        if len(cleaned) > fixes_limit:
            cleaned = truncate_at_boundary(cleaned, fixes_limit)
        sections.append(f"🔧 修复内容\n{'-' * 15}\n{cleaned}")

    issues_start = content.lower().find("known issues")
    if issues_start != -1:
        issues_content = content[issues_start:]
        cleaned = clean_game_text(issues_content)
        if len(cleaned) > issues_limit:
            cleaned = truncate_at_boundary(cleaned, issues_limit)
        sections.append(f"🐛 已知问题\n{'-' * 15}\n{cleaned}")

    if not sections:
        cleaned_full = clean_game_text(content)
        return (
            truncate_at_boundary(cleaned_full, min(1500, max_length))
            + "\n\n📄 完整内容请查看Steam页面"
        )

    if len(sections) > max_sections:
        sections = sections[:max_sections]

    result = "\n\n".join(sections)
    if len(result) > max_length and max_sections > 1:
        result = "\n\n".join(sections[: max(1, max_sections - 1)])
    result += "\n\n📄 完整内容请查看Steam页面"
    return result


def _format_content_structure(content: str) -> str:
    if not content:
        return content
    formatted = clean_game_text(content)
    formatted = re.sub(r"⚖️\s*\*\*([^*]+)\*\*", r"## ⚖️ \1", formatted)
    formatted = re.sub(r"🔧\s*\*\*([^*]+)\*\*", r"## 🔧 \1", formatted)
    formatted = re.sub(
        r"^\s*🔧\s*修复\s*$", "## 🔧 修复", formatted, flags=re.MULTILINE
    )
    formatted = re.sub(r"\*\s+\*([^*]+)\*\*", r"**\1**", formatted)
    formatted = re.sub(r"\n\s*\n\s*\n+", "\n\n", formatted)
    return formatted.strip()


def format_steam_message(
    update: dict[str, Any],
    *,
    title: str | None = None,
    content: str | None = None,
    max_content_length: int = 2000,
    balancing_limit: int = 800,
    fixes_limit: int = 600,
    issues_limit: int = 300,
    max_sections: int = 3,
) -> str:
    try:
        if not update:
            return "🎮 当前没有 Steam 更新日志"

        title = clean_game_text(
            title if title is not None else update.get("title", "无标题")
        )
        raw_content = (
            content if content is not None else update.get("content", "无内容")
        )
        author = update.get("author", "未知") or "未知"
        published_time = format_iso_time(update.get("publishedAt", ""))

        body = _truncate_steam_section(
            raw_content or "",
            max_length=max_content_length,
            balancing_limit=balancing_limit,
            fixes_limit=fixes_limit,
            issues_limit=issues_limit,
            max_sections=max_sections,
        )
        body = _format_content_structure(body)

        lines = [
            "🎮 Steam 更新日志 | HELLDIVERS 2",
            "-------------",
            f"▎标题: {title}",
            f"▎作者: {author}",
            f"▎时间: {published_time}",
            "-------------",
            f"▎内容:\n{body}",
            "-------------",
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("[HD2] 格式化失败")
        return "❌ 数据格式化失败，请稍后重试。"


def format_wiki_summary(
    *,
    title: str,
    extract: str,
    url: str,
    source: str = "helldivers.wiki.gg",
    note: str = "",
    full: bool = False,
) -> str:
    try:
        body = (
            clean_game_text(extract or "").strip()
            or "（暂无摘要，请打开链接查看完整内容）"
        )
        header = f"📖 Wiki{' 全文' if full else ''} | {title}"
        if note:
            header += f" {note}"
        lines = [
            header,
            "-------------",
            f"▎{'全文' if full else '摘要'}: {body}",
            "-------------",
            f"🔗 {url}",
            f"来源: {source}",
        ]
        if not full:
            lines.append("提示: 加 -f 可查看全文，如 /wiki 磁轨炮 -f")
        return "\n".join(lines)
    except Exception:
        logger.exception("[HD2] Wiki 摘要格式化失败")
        return "❌ Wiki 摘要格式化失败，请稍后重试。"


def format_wiki_search_results(
    query: str,
    results: list[dict[str, Any]],
    *,
    search_url: str = "",
) -> str:
    try:
        if not results:
            lines = [
                f"🔍 Wiki 搜索: {query}",
                "-------------",
                "未找到相关词条。",
            ]
            if search_url:
                lines.append(f"可在网页继续搜索: {search_url}")
            return "\n".join(lines)

        lines = [
            f"🔍 Wiki 搜索: {query}",
            "-------------",
        ]
        for i, item in enumerate(results, 1):
            title = item.get("title") or "未知"
            snippet = clean_game_text(item.get("snippet") or "").strip()
            if snippet:
                # 列表里 snippet 保持短一些
                if len(snippet) > 80:
                    snippet = snippet[:77] + "..."
                lines.append(f"{i}. {title}")
                lines.append(f"   {snippet}")
            else:
                lines.append(f"{i}. {title}")
        lines.append("-------------")
        example = results[0].get("title") or query
        lines.append(f"请用更精确标题重试，例如: /wiki {example}")
        if search_url:
            lines.append(f"网页搜索: {search_url}")
        return "\n".join(lines)
    except Exception:
        logger.exception("[HD2] Wiki 搜索结果格式化失败")
        return "❌ Wiki 搜索结果格式化失败，请稍后重试。"


WIKI_USAGE = """\
📖 Wiki 查询用法
-------------
/wiki <关键词>       搜索并查看词条摘要
/wiki <关键词> -f    查看词条全文
支持中英文关键词（中文自动按官方译名搜索）
例如:
  /wiki Railgun
  /wiki 磁轨炮
  /wiki Bile Titan -f
-------------
资料来源: helldivers.wiki.gg"""


def format_dss_message(station: dict[str, Any] | None) -> str:
    if not station:
        return "❌ 暂时无法获取民主空间站（DSS）信息。"
    try:
        from .war_state import faction_cn

        planet = (
            station.get("planet") if isinstance(station.get("planet"), dict) else {}
        )
        p_name = planet.get("name") or station.get("planetName") or "未知"
        owner = faction_cn(
            planet.get("currentOwner") or planet.get("owner") or "未知"
        )
        election = station.get("electionEnd") or station.get("nextElectionEnd") or ""
        election_str = format_iso_time(str(election)) if election else "未知"
        flags = station.get("flags", "")
        sid = station.get("id32") or station.get("id") or ""

        lines = [
            "🛰️ 民主空间站 | DSS",
            "-------------",
            f"▎ID: {sid}",
            f"▎位置: {p_name}",
            f"▎星球所有者: {owner}",
            f"▎选举/跃迁截止: {election_str}",
        ]
        if flags != "":
            lines.append(f"▎标志: {flags}")

        lines.append("-------------")
        lines.append("为了超级地球！🌍")
        return "\n".join(lines)
    except Exception:
        logger.exception("[HD2] DSS 数据格式化失败")
        return "❌ DSS 数据格式化失败，请稍后重试。"


def format_planet_message(planet: dict[str, Any]) -> str:
    try:
        from .war_state import (
            faction_cn,
            planet_health_pct,
            planet_index,
            planet_liberation_pct,
            planet_name,
            planet_owner,
            planet_players,
        )

        name = planet_name(planet)
        idx = planet_index(planet)
        owner = faction_cn(planet_owner(planet))
        sector = planet.get("sector") or "未知"
        players = planet_players(planet)
        hp = planet_health_pct(planet)
        biome = ""
        if isinstance(planet.get("biome"), dict):
            biome = planet["biome"].get("name") or ""
        elif planet.get("biome"):
            biome = str(planet.get("biome"))
        hazards = planet.get("hazards") or []
        hazard_names = []
        for h in hazards:
            if isinstance(h, dict):
                hazard_names.append(str(h.get("name") or h.get("description") or ""))
            else:
                hazard_names.append(str(h))
        hazard_names = [h for h in hazard_names if h]

        event = planet.get("event") if isinstance(planet.get("event"), dict) else None
        lines = [
            f"🪐 星球 | {name}",
            "-------------",
            f"▎编号: {idx if idx is not None else '未知'}",
            f"▎扇区: {sector}",
            f"▎所有者: {owner}",
            f"▎在线: {format_number(players)}",
        ]
        if hp is not None:
            lines.append(f"▎生命值: {hp:.1f}%")
        lib = planet_liberation_pct(planet)
        if lib is not None:
            lines.append(f"▎解放进度: {lib:.1f}%")
        if biome:
            lines.append(f"▎生物群系: {biome}")
        if hazard_names:
            lines.append(f"▎灾害: {', '.join(hazard_names[:5])}")
        if event:
            lines.append("-------------")
            lines.append(
                f"▎事件类型: {event.get('eventType') or event.get('type') or '活跃'}"
            )
            if event.get("faction"):
                lines.append(f"▎事件阵营: {faction_cn(str(event.get('faction')))}")
            if event.get("expireTime") or event.get("endTime"):
                lines.append(
                    f"▎事件结束: {format_iso_time(str(event.get('expireTime') or event.get('endTime')))}"
                )
        attacking = planet.get("attacking") or []
        if attacking:
            lines.append(f"▎进攻关联: {', '.join(str(x) for x in attacking[:8])}")
        lines.append("-------------")
        return "\n".join(lines)
    except Exception:
        logger.exception("[HD2] 星球数据格式化失败")
        return "❌ 星球数据格式化失败，请稍后重试。"


def format_planet_candidates(query: str, planets: list[dict[str, Any]]) -> str:
    from .war_state import planet_index, planet_name, planet_players

    lines = [f"🔍 星球搜索: {query}", "-------------"]
    if not planets:
        lines.append("未找到匹配星球。")
        lines.append("用法: /planet <名称或编号>")
        return "\n".join(lines)
    for i, p in enumerate(planets, 1):
        idx = planet_index(p)
        lines.append(
            f"{i}. [{idx}] {planet_name(p)}  在线:{format_number(planet_players(p))}"
        )
    lines.append("-------------")
    example = planet_name(planets[0])
    lines.append(f"请用更精确名称重试，例如: /planet {example}")
    return "\n".join(lines)


def format_warfront_message(faction: str, campaigns: list[dict[str, Any]]) -> str:
    from .war_state import (
        campaign_planet,
        campaign_progress_pct,
        faction_cn,
        planet_name,
        planet_players,
    )

    fname = faction_cn(faction) or faction
    lines = [
        f"⚔️ 战线战况 | {fname}",
        "-------------",
    ]
    if not campaigns:
        lines.append("当前没有该阵营的活跃战役。")
        lines.append("-------------")
        return "\n".join(lines)

    defence: list[dict[str, Any]] = []
    attack: list[dict[str, Any]] = []
    for c in campaigns:
        p = campaign_planet(c) or {}
        ev = p.get("event") if isinstance(p.get("event"), dict) else None
        if ev:
            defence.append(c)
        else:
            attack.append(c)

    def _add_section(title: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        lines.append(title)
        for c in items[:12]:
            p = campaign_planet(c) or {}
            name = planet_name(p)
            players = planet_players(p)
            # 进度优先取 event/regions 聚合（API 改版后主战场恒满血）
            prog = campaign_progress_pct(c)
            prog_s = f"{prog:.1f}%" if prog is not None else "N/A"
            lines.append(f"▎{name}  进度:{prog_s}  在线:{format_number(players)}")
        lines.append("-------------")

    _add_section("🛡️ 防御/事件", defence)
    _add_section("🗡️ 进攻/解放", attack)
    if len(lines) <= 3:
        for c in campaigns[:15]:
            p = campaign_planet(c) or {}
            lines.append(f"▎{planet_name(p)}  在线:{format_number(planet_players(p))}")
        lines.append("-------------")
    lines.append(f"共 {len(campaigns)} 条活跃战役")
    return "\n".join(lines)


def format_global_events_message(events: list[dict[str, Any]]) -> str:
    lines = ["🌐 全球事件 | HELLDIVERS 2", "-------------"]
    if not events:
        lines.append("当前没有活跃的全球事件。")
        lines.append("-------------")
        return "\n".join(lines)

    shown = 0
    for e in events:
        if not isinstance(e, dict):
            continue
        title = clean_game_text(str(e.get("title") or "")).strip()
        msg = clean_game_text(str(e.get("message") or "")).strip()
        if not title and not msg:
            continue
        if not title:
            title = f"事件 #{e.get('eventId') or e.get('id32') or shown + 1}"
        if len(msg) > 500:
            msg = msg[:497] + "..."
        lines.append(f"📢 {title}")
        if msg:
            lines.append(msg)
        expire = e.get("expireTime") or e.get("endTime")
        if expire:
            lines.append(f"⏳ 截止(游戏时间): {expire}")
        shown += 1
        if shown >= 5:
            break
        lines.append("-----")
    if shown == 0:
        lines.append("当前没有活跃的全球事件。")
    lines.append("-------------")
    return "\n".join(lines)


def format_map_caption(active_campaigns: int, planets: int, updated: str = "") -> str:
    lines = [
        "🗺️ 银河战争地图 | HELLDIVERS 2",
        f"▎活跃战役: {active_campaigns}",
        f"▎星球数据: {planets}",
    ]
    if updated:
        lines.append(f"▎更新: {updated}")
    lines.append("-------------")
    return "\n".join(lines)
