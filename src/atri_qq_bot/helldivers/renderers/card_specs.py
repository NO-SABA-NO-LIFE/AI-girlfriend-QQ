"""各指令数据 → GWW 风格 Card 构建。"""

from __future__ import annotations

from typing import Any

from .card_renderer import COLOR_DSS, COLOR_MO, FACTION_COLORS, Card
from ..core.text_utils import (
    clean_game_text,
    dispatch_type_name,
    format_expires_in,
    format_iso_time,
    format_number,
    format_percentage,
    format_time_hours,
)
from ..core.war_state import (
    campaign_planet,
    campaign_progress_pct,
    faction_cn,
    normalize_faction,
    planet_health_pct,
    planet_index,
    planet_liberation_pct,
    planet_name,
    planet_owner,
    planet_players,
    tactical_action_progress,
    tactical_action_status_label,
)

FOOTER = "为了超级地球！🌍  ·  api.helldivers2.dev"

# 战术行动中文名
_TA_NAME_CN = {
    "EAGLE STORM": "鹰风暴",
    "HEAVY ORDNANCE DISTRIBUTION": "重型军械分发",
    "ORBITAL BLOCKADE": "轨道封锁",
    "ORBITAL NAPALM BARRAGE": "轨道燃烧弹幕",
}


def _ta_name_cn(name: str) -> str:
    key = (name or "").strip().upper()
    return _TA_NAME_CN.get(key, name or "战术行动")


def _remaining_label(iso_or_val: Any) -> str:
    """ISO 时间 → 剩余 X天Y小时；已过期则提示。"""
    if iso_or_val is None or iso_or_val == "":
        return ""
    s = str(iso_or_val)
    try:
        from datetime import datetime, timezone

        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        sec = int((dt - now).total_seconds())
        if sec <= 0:
            return "已结束"
        days, rem = divmod(sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}天")
        if hours or days:
            parts.append(f"{hours}小时")
        if mins and days == 0:
            parts.append(f"{mins}分")
        if not parts:
            parts.append(f"{secs}秒")
        return "".join(parts)
    except Exception:
        # 可能是相对秒数
        try:
            sec = int(float(iso_or_val))
            if sec > 1_000_000_000:  # 像 war time 整数
                return str(iso_or_val)
            return format_expires_in(sec) or str(iso_or_val)
        except Exception:
            return str(iso_or_val)


def _accent_for_faction(name: str | None) -> tuple[int, int, int]:
    return FACTION_COLORS.get(normalize_faction(name), FACTION_COLORS["Humans"])


def build_stats_card(
    war_summary: dict[str, Any] | None,
    war_v1: dict[str, Any] | None,
    *,
    subtitle: str = "",
) -> Card:
    stats: dict[str, Any] = {}
    impact = 0.0
    players = 0
    if isinstance(war_summary, dict):
        if isinstance(war_summary.get("galaxy_stats"), dict):
            stats = dict(war_summary["galaxy_stats"])
            impact = war_summary.get("impactMultiplier", 0) or 0
            players = stats.get("playerCount", 0) or 0
        elif "statistics" in war_summary:
            stats = war_summary.get("statistics") or {}
            impact = war_summary.get("impactMultiplier", 0) or 0
            players = stats.get("playerCount", 0) or 0
        else:
            stats = dict(war_summary)
            impact = war_summary.get("impactMultiplier", 0) or 0
            players = war_summary.get("playerCount", 0) or 0
    if isinstance(war_v1, dict):
        v1 = war_v1.get("statistics") or {}
        if war_v1.get("impactMultiplier") is not None:
            impact = war_v1.get("impactMultiplier")
        vp = v1.get("playerCount", war_v1.get("playerCount"))
        if vp is not None:
            players = vp
        for k, v in v1.items():
            if k not in stats or not stats.get(k):
                stats[k] = v

    try:
        impact_s = f"{float(impact):.6f}"
    except (TypeError, ValueError):
        impact_s = str(impact)

    terminid = stats.get("terminidKills", 0) or stats.get("bugKills", 0)
    card = Card(
        title="银河战争统计",
        accent=FACTION_COLORS["Humans"],
        subtitle=subtitle,
        emblem="super_earth",
        footer=FOOTER,
    )
    card.grid(
        [
            ("在线玩家", format_number(players)),
            ("影响系数", impact_s),
            ("发射子弹", format_number(stats.get("bulletsFired", 0))),
            ("冻肉储备", format_number(stats.get("friendlies", 0))),
        ]
    )
    card.divider().heading("任务统计")
    card.grid(
        [
            ("胜利任务", format_number(stats.get("missionsWon", 0))),
            ("失败任务", format_number(stats.get("missionsLost", 0))),
            ("成功率", format_percentage(stats.get("missionSuccessRate", 0))),
            ("总时长", format_time_hours(stats.get("timePlayed", 0))),
        ]
    )
    card.divider().heading("战斗统计")
    card.grid(
        [
            ("终结族击杀", format_number(terminid)),
            ("机器人击杀", format_number(stats.get("automatonKills", 0))),
            ("光能者击杀", format_number(stats.get("illuminateKills", 0))),
            ("阵亡次数", format_number(stats.get("deaths", 0))),
        ]
    )
    return card


def build_order_card(
    order: dict[str, Any],
    *,
    index: int = 1,
    title: str,
    brief: str,
    task_desc: str,
) -> Card:
    setting = order.get("setting") or {}
    title = clean_game_text(title) or "未知命令"
    brief = clean_game_text(brief)
    task_desc = clean_game_text(task_desc)

    card = Card(
        title=f"最高命令 {index}",
        accent=COLOR_MO,
        emblem="mo",
        footer=FOOTER,
    )
    card.field("命令", title)
    if brief:
        card.field("简介", brief)
    if task_desc:
        card.field("任务", task_desc)

    tasks = setting.get("tasks") or []
    progress = order.get("progress") or []
    for i, task in enumerate(tasks):
        if i >= len(progress):
            break
        vals = task.get("values") or []
        if len(vals) <= 2:
            continue
        try:
            cur = float(progress[i])
            target = float(vals[2])
        except (TypeError, ValueError):
            continue
        if target <= 0:
            continue
        pct = cur / target * 100
        card.bar(
            f"任务 {i + 1}",
            pct,
            color=COLOR_MO,
            note=f"{pct:.1f}%  ({format_number(int(cur))}/{format_number(int(target))})",
        )

    expires = format_expires_in(order.get("expiresIn", 0) or 0)
    reward = setting.get("reward") or {}
    amount = reward.get("amount", 0) or 0
    tail = []
    if expires:
        tail.append(("剩余时间", expires))
    if amount:
        tail.append(("奖励", f"{format_number(amount)} 奖章"))
    if tail:
        card.grid(tail)
    return card


def build_dss_card(station: dict[str, Any] | None) -> Card:
    card = Card(
        title="民主空间站 · DSS",
        accent=COLOR_DSS,
        emblem="dss",
        footer=FOOTER,
        subtitle="民主空间站",
    )
    if not station:
        card.text("暂时无法获取民主空间站信息。", muted=True)
        return card
    planet = station.get("planet") if isinstance(station.get("planet"), dict) else {}
    p_name = planet.get("name") or station.get("planetName") or "未知"
    sector = planet.get("sector") or ""
    owner = faction_cn(planet.get("currentOwner") or planet.get("owner") or "")
    election = station.get("electionEnd") or station.get("nextElectionEnd") or ""
    remain = _remaining_label(election)
    loc = f"{p_name}" + (f" · {sector}" if sector else "")
    card.grid(
        [
            ("当前位置", loc),
            ("星球所有者", owner or "未知"),
            (
                "下次跃迁",
                remain or (format_iso_time(str(election)) if election else "未知"),
            ),
            ("跃迁截止(本地)", format_iso_time(str(election)) if election else "—"),
        ]
    )
    actions = (
        station.get("tacticalActions")
        or station.get("tactical_actions")
        or station.get("actions")
        or []
    )
    if isinstance(actions, list) and actions:
        card.divider().heading("战术行动")

        # 排序：进行中 > 准备中 > 冷却
        def _sk(a: dict[str, Any]) -> int:
            try:
                s = int(a.get("status") or 0)
            except (TypeError, ValueError):
                s = 0
            return {2: 0, 1: 1, 3: 2}.get(s, 9)

        for a in sorted([x for x in actions if isinstance(x, dict)], key=_sk)[:6]:
            raw_name = str(a.get("name") or a.get("title") or "行动")
            name = str(a.get("display_name") or _ta_name_cn(raw_name))
            status = a.get("status")
            status_cn = tactical_action_status_label(status)
            expire = a.get("statusExpire") or a.get("expireTime") or ""
            rem = _remaining_label(expire)
            desc = clean_game_text(
                str(
                    a.get("display_strategic_description")
                    or a.get("display_description")
                    or a.get("strategicDescription")
                    or a.get("description")
                    or a.get("desc")
                    or ""
                )
            )
            if len(desc) > 160:
                desc = desc[:157] + "..."
            head = f"{name}  ·  {status_cn}"
            if rem:
                head += f"  ·  {rem}"
            card.field(head, desc or "—")
            pct = tactical_action_progress(a)
            # status 1 preparing 显示进度；2 active 满条；3 cooldown 空条
            try:
                st = int(status)
            except (TypeError, ValueError):
                st = -1
            if st == 1 and pct is not None:
                card.bar("准备进度", pct, color=COLOR_DSS, note=f"{pct:.1f}%")
            elif st == 2:
                card.bar("进行中", 100.0, color=(80, 200, 120), note="Active")
            elif st == 3:
                card.bar("冷却中", 0.0, color=(120, 120, 130), note=rem or "Cooldown")
    return card


def build_dashboard_card(
    sections: dict[str, Any],
    *,
    subtitle: str = "",
    low_impact_threshold: int = 800,
) -> Card:
    """GWW Galactic Overview 风格总览。"""
    card = Card(
        title="银河总览",
        accent=FACTION_COLORS["Humans"],
        subtitle=subtitle or "银河战争总览",
        emblem="super_earth",
        footer=FOOTER,
    )

    dss = sections.get("dss")
    dss_planet = sections.get("dss_planet") or {}
    if dss:
        dp = dss_planet.get("name") or "未知"
        election = dss.get("electionEnd") or ""
        rem = _remaining_label(election)
        active_ta = []
        for a in dss.get("tacticalActions") or []:
            if not isinstance(a, dict):
                continue
            try:
                if int(a.get("status") or 0) == 2:
                    active_ta.append(_ta_name_cn(str(a.get("name") or "")))
            except (TypeError, ValueError):
                pass
        ta_s = "、".join(active_ta) if active_ta else "无进行中战术"
        card.heading("民主空间站")
        card.field(
            f"位置 {dp}",
            f"跃迁 {rem or '—'}  ·  {ta_s}",
        )
        card.divider()

    def _section_players(rows: list[dict[str, Any]]) -> int:
        return sum(int(r.get("players") or 0) for r in rows)

    def _add_rows(
        title: str,
        rows: list[dict[str, Any]],
        *,
        color: tuple[int, int, int],
        max_rows: int = 6,
    ) -> None:
        if not rows:
            return
        total_p = _section_players(rows)
        total_all = max(1, int(sections.get("total_players") or total_p))
        share = total_p / total_all * 100
        card.heading(f"{title}  ({share:.1f}% · {format_number(total_p)} 在线)")
        for r in rows[:max_rows]:
            name = r.get("name") or "?"
            players = int(r.get("players") or 0)
            prog = r.get("progress")
            ends = _remaining_label(r.get("ends"))
            note = f"在线 {format_number(players)}"
            if ends:
                note = f"{ends}  ·  " + note
            if r.get("dss"):
                name = f"{name}  [DSS]"
            pct = float(prog) if prog is not None else 0.0
            card.bar(name, pct, color=color, note=note)
        if len(rows) > max_rows:
            card.text(f"…及其他 {len(rows) - max_rows} 颗星球", muted=True)

    urgent = sections.get("urgent") or []
    defending = sections.get("defending") or []
    attacking = sections.get("attacking") or {}

    _add_rows("紧急解放行动", urgent, color=(220, 80, 80), max_rows=5)
    if urgent:
        card.divider()
    _add_rows("防御中", defending, color=(90, 160, 230), max_rows=5)
    if defending:
        card.divider()

    fac_meta = [
        ("Automaton", "进攻机器人", FACTION_COLORS["Automaton"]),
        ("Terminids", "进攻终结族", FACTION_COLORS["Terminids"]),
        ("Illuminate", "进攻光能者", FACTION_COLORS["Illuminate"]),
    ]
    for fac, title, color in fac_meta:
        block = attacking.get(fac) or {}
        main = block.get("main") or []
        low = block.get("low") or []
        if not main and not low:
            continue
        total_p = int(block.get("total_players") or 0)
        total_all = max(1, int(sections.get("total_players") or total_p))
        share = total_p / total_all * 100
        card.heading(f"{title}  ({share:.1f}%)")
        for r in main[:8]:
            name = r.get("name") or "?"
            players = int(r.get("players") or 0)
            prog = r.get("progress")
            pct = float(prog) if prog is not None else 0.0
            card.bar(
                name,
                pct,
                color=color,
                note=f"在线 {format_number(players)}"
                + (f"  ·  {pct:.1f}%" if prog is not None else ""),
            )
        if low:
            other_p = int(block.get("other_players") or 0)
            names = ", ".join(
                f"{r.get('name')} {format_number(r.get('players') or 0)}"
                for r in low[:6]
            )
            card.text(
                f"低影响星球: {names}"
                + (f" … 另 {len(low) - 6} 颗" if len(low) > 6 else "")
                + f"  (合计在线 {format_number(other_p)})",
                muted=True,
            )
        card.divider()

    total_players = int(sections.get("total_players") or 0)
    card.grid(
        [
            ("战役总在线", format_number(total_players)),
            ("紧急", str(len(urgent))),
            ("防御", str(len(defending))),
            (
                "进攻战役",
                str(
                    sum(
                        int((attacking.get(f) or {}).get("count") or 0)
                        for f, _, _ in fac_meta
                    )
                ),
            ),
        ]
    )
    return card


def build_global_events_card(events: list[dict[str, Any]]) -> Card:
    card = Card(title="全球事件", accent=(255, 180, 60), emblem="mo", footer=FOOTER)
    shown = 0
    for e in events:
        if not isinstance(e, dict):
            continue
        title = clean_game_text(str(e.get("title") or "")).strip()
        msg = clean_game_text(str(e.get("message") or "")).strip()
        if not title and not msg:
            continue
        if title:
            card.heading(title)
        if msg:
            if len(msg) > 600:
                msg = msg[:597] + "..."
            card.text(msg)
        expire = e.get("expireTime") or e.get("endTime")
        if expire:
            card.text(f"截止(游戏时间): {expire}", muted=True)
        shown += 1
        if shown >= 4:
            break
        card.divider()
    if shown == 0:
        card.text("当前没有活跃的全球事件。", muted=True)
    return card


def build_news_card(
    dispatch: dict[str, Any],
    *,
    index: int = 1,
    message_text: str | None = None,
    card_title: str | None = None,
    footer: str | None = None,
) -> Card:
    card = Card(
        title=card_title or f"游戏快讯 {index}",
        accent=FACTION_COLORS["Humans"],
        emblem="super_earth",
        footer=footer or "使用 /news [1-5] 查看其他快讯 · api.helldivers2.dev",
    )
    dtype = dispatch_type_name(dispatch.get("type", 0))
    published = format_iso_time(dispatch.get("published", ""))
    card.grid([("类型", dtype), ("编号", f"#{dispatch.get('id', 0)}")])
    card.field("时间", published)
    content = clean_game_text(
        message_text if message_text is not None else dispatch.get("message", "")
    )
    card.divider()
    card.text(content or "（无内容）")
    return card


def build_warfront_card(faction: str, campaigns: list[dict[str, Any]]) -> Card:
    norm = normalize_faction(faction)
    accent = _accent_for_faction(norm)
    card = Card(
        title=f"战线战况 · {faction_cn(norm)}",
        accent=accent,
        emblem=norm
        if norm in ("Terminids", "Automaton", "Illuminate")
        else "super_earth",
        footer=FOOTER,
    )
    if not campaigns:
        card.text("当前没有该阵营的活跃战役。", muted=True)
        return card

    defence: list[dict[str, Any]] = []
    attack: list[dict[str, Any]] = []
    for c in campaigns:
        p = campaign_planet(c) or {}
        if isinstance(p.get("event"), dict):
            defence.append(c)
        else:
            attack.append(c)

    def _section(title: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        card.heading(title)
        for c in items[:10]:
            p = campaign_planet(c) or {}
            name = planet_name(p)
            players = planet_players(p)
            # 进度优先取 event/regions 聚合（API 改版后主战场恒满血）
            prog = campaign_progress_pct(c)
            pct = prog if prog is not None else 0.0
            note = f"在线 {format_number(players)}"
            card.bar(name, pct, color=accent, note=note)

    _section("🛡 防御 / 事件", defence)
    _section("🗡 进攻 / 解放", attack)
    card.divider().text(f"共 {len(campaigns)} 条活跃战役", muted=True)
    return card


def build_planet_card(planet: dict[str, Any]) -> Card:
    owner = planet_owner(planet)
    accent = _accent_for_faction(owner)
    name = planet_name(planet)
    idx = planet_index(planet)
    card = Card(
        title=f"星球 · {name}",
        accent=accent,
        emblem=normalize_faction(owner)
        if normalize_faction(owner) in ("Terminids", "Automaton", "Illuminate")
        else "super_earth",
        footer=FOOTER,
    )
    card.grid(
        [
            ("编号", str(idx if idx is not None else "?")),
            ("扇区", str(planet.get("sector") or "未知")),
            ("所有者", faction_cn(owner)),
            ("在线", format_number(planet_players(planet))),
        ]
    )
    hp = planet_health_pct(planet)
    if hp is not None:
        card.bar("生命值", hp, color=accent, note=f"{hp:.1f}%")
    lib = planet_liberation_pct(planet)
    if lib is not None:
        card.bar("解放进度", lib, color=accent, note=f"{lib:.1f}%")
    biome = ""
    if isinstance(planet.get("biome"), dict):
        biome = planet["biome"].get("name") or ""
    elif planet.get("biome"):
        biome = str(planet.get("biome"))
    if biome:
        card.field("生物群系", str(biome))
    hazards = planet.get("hazards") or []
    hnames = []
    for h in hazards:
        if isinstance(h, dict):
            hnames.append(str(h.get("name") or ""))
        else:
            hnames.append(str(h))
    hnames = [h for h in hnames if h]
    if hnames:
        card.field("环境灾害", "、".join(hnames[:5]))
    event = planet.get("event") if isinstance(planet.get("event"), dict) else None
    if event:
        card.divider().heading("行星事件")
        card.field("类型", str(event.get("eventType") or event.get("type") or "活跃"))
        if event.get("faction"):
            card.field("事件阵营", faction_cn(str(event.get("faction"))))
    return card


def build_planet_candidates_card(query: str, planets: list[dict[str, Any]]) -> Card:
    card = Card(
        title=f"星球搜索 · {query}",
        accent=FACTION_COLORS["Humans"],
        emblem="super_earth",
        footer=FOOTER,
    )
    if not planets:
        card.text("未找到匹配星球。用法: /planet <名称或编号>", muted=True)
        return card
    for i, p in enumerate(planets, 1):
        card.field(
            f"{i}. [{planet_index(p)}] {planet_name(p)}",
            f"在线 {format_number(planet_players(p))}  ·  {faction_cn(planet_owner(p))}",
        )
    card.divider().text(
        f"请用更精确名称重试，例如: /planet {planet_name(planets[0])}", muted=True
    )
    return card


def build_arsenal_card(item: dict[str, Any], *, aliases: list[str] | None = None) -> Card:
    """HD2Tool 武器库物品卡片：武器/战备/护甲/手榴弹通用。"""
    from ..clients.arsenal_client import (
        ACQUISITION_ZH,
        COMPONENT_LABEL_ZH,
        FIELD_ZH,
        HANDLING_ZH,
        PRODUCT_KIND_ZH,
    )

    name_zh = str(item.get("nameZh") or item.get("nameEn") or "?")
    kind = PRODUCT_KIND_ZH.get(str(item.get("productKind")), str(item.get("productKind") or "装备"))
    card = Card(
        title=f"{kind} · {name_zh}",
        accent=FACTION_COLORS["Humans"],
        emblem="super_earth",
        footer=str(
            item.get("_footer") or "数据: HD2Tool (MIT) · helldivers.wiki.gg"
        ),
    )

    # 基本信息网格
    grid_pairs: list[tuple[str, str]] = [("型号", str(item.get("model") or "?"))]
    if item.get("weaponType"):
        grid_pairs.append(("类型", str(item["weaponType"])))
    acq = item.get("acquisition") or {}
    acq_kind = ACQUISITION_ZH.get(str(acq.get("kind")), str(acq.get("kind") or ""))
    if acq.get("kind") == "warbond":
        page = acq.get("page")
        medals = acq.get("itemMedals")
        acq_text = f"{acq_kind} 第{page}页" if page else acq_kind
        if medals:
            acq_text += f" · {medals}勋章"
    else:
        acq_text = acq_kind
    if acq_text:
        grid_pairs.append(("获取", acq_text))
    if item.get("nameEn"):
        grid_pairs.append(("英文名", str(item["nameEn"])))
    card.grid(grid_pairs)

    if aliases:
        card.text(f"玩家俗称: {'、'.join(aliases[:5])}", muted=True)
    description = str(item.get("description") or "")
    if description:
        card.text(description[:160], muted=True)

    # 护甲属性
    armor = item.get("armor")
    if isinstance(armor, dict) and armor:
        card.divider().heading("护甲属性")
        rating = armor.get("rating")
        if isinstance(rating, (int, float)) and rating:
            card.bar(
                "护甲等级",
                float(rating) / 150 * 100,
                note=f"{int(rating)} ({armor.get('class') or '?'})",
            )
        pairs: list[tuple[str, str]] = []
        if armor.get("speed") is not None:
            pairs.append(("移速", str(armor["speed"])))
        if armor.get("staminaRegen") is not None:
            pairs.append(("体力回复", str(armor["staminaRegen"])))
        if pairs:
            card.grid(pairs)
        if armor.get("passive"):
            card.field("被动", str(armor["passive"]))

    # 战斗数值组件
    combat = item.get("combat")
    if isinstance(combat, dict):
        components = [c for c in combat.get("components") or [] if isinstance(c, dict)]
        primary_id = combat.get("primaryComponentId")
        if components:
            card.divider().heading("战斗数值")
        for comp in components:
            label = COMPONENT_LABEL_ZH.get(
                str(comp.get("label")), str(comp.get("label") or comp.get("type") or "")
            )
            suffix = "（主）" if comp.get("id") == primary_id and len(components) > 1 else ""
            fields = comp.get("fields") or {}
            parts: list[str] = []
            for key, zh in FIELD_ZH.items():
                if key not in fields:
                    continue
                value = fields[key]
                if key == "armorPenetration" and isinstance(value, dict):
                    pen = value.get("labelZh") or f"等级{value.get('value', '?')}"
                    parts.append(f"穿甲 {pen}")
                elif isinstance(value, (int, float)):
                    parts.append(f"{zh} {int(value) if float(value).is_integer() else value}")
                elif value:
                    parts.append(f"{zh} {value}")
            if parts:
                card.field(f"{label}{suffix}", " · ".join(parts[:7]))

    # 操控属性（弹匣/射速等）
    handling = item.get("handling")
    if isinstance(handling, dict) and handling:
        pairs = []
        for key, zh in HANDLING_ZH.items():
            if key in handling and handling[key] not in (None, "", 0):
                value = handling[key]
                if key == "fireRate":
                    pairs.append((zh, f"{value} RPM"))
                elif key == "reloadSeconds":
                    pairs.append((zh, f"{value}s"))
                elif isinstance(value, list):
                    pairs.append((zh, "/".join(str(v) for v in value[:4])))
                else:
                    pairs.append((zh, str(value)))
        if pairs:
            card.grid(pairs)

    # 战备部署信息
    deploy = item.get("deployment")
    if isinstance(deploy, dict) and deploy.get("cooldownSeconds"):
        # 输入码方向 → 箭头符号（"↓↑→↑←↑"），避免打出 Python 原始列表
        code = deploy.get("code")
        code_text = ""
        if isinstance(code, list) and code:
            arrows = {"up": "↑", "down": "↓", "left": "←", "right": "→"}
            code_text = " · 输入码 " + "".join(
                arrows.get(str(step).lower(), "?") for step in code
            )
        card.field("战备", f"冷却 {deploy['cooldownSeconds']}s" + code_text)

    wiki = item.get("wiki") or {}
    if wiki.get("url"):
        card.divider().text(f"🔗 {wiki['url']}", muted=True)
    return card


def build_arsenal_candidates_card(
    query: str, items: list[dict[str, Any]]
) -> Card:
    from ..clients.arsenal_client import PRODUCT_KIND_ZH

    card = Card(
        title=f"装备搜索 · {query}",
        accent=FACTION_COLORS["Humans"],
        emblem="super_earth",
        footer=(
            "数据: HD2Tool + wiki.gg 补充目录"
            if any(it.get("_source") == "dionatha" for it in items)
            else "数据: HD2Tool (MIT) · 用 /hd2data <名称> 查看详情"
        ),
    )
    if not items:
        card.text("未找到匹配装备。支持中文名、英文型号和玩家俗称（如 电喷/次抛/磁小轨）。", muted=True)
        return card
    for i, it in enumerate(items[:8], 1):
        kind = PRODUCT_KIND_ZH.get(str(it.get("productKind")), "装备")
        name = str(it.get("nameZh") or it.get("nameEn") or "?")
        model = str(it.get("model") or "").strip()
        label = f"{name}（{model}）" if model else name
        card.text(f"[{i}] {label} · {kind}", muted=False)
    if len(items) > 8:
        card.text(f"...共 {len(items)} 条", muted=True)
    return card


def build_enemy_card(
    item: dict[str, Any], armor_parts: list[tuple[str, int]] | None = None
) -> Card:
    """敌人卡片：阵营、护甲类型、血量（含部位）、部位护甲、伤害信息、wiki 链接。"""
    from ..clients.arsenal_client import (
        _ENEMY_CLASS_ZH,
        _PART_NAME_ZH,
        armor_tier_label,
        parse_damage,
        parse_health,
    )

    faction = str(item.get("faction") or "")
    faction_lower = faction.casefold()
    # 根据阵营配色和徽章（变体阵营归到母阵营配色）
    if "terminid" in faction_lower or "spore" in faction_lower or "rupture" in faction_lower or "predator" in faction_lower:
        accent = FACTION_COLORS["Terminids"]
        emblem = "Terminids"
    elif "automaton" in faction_lower or "jet" in faction_lower or "inciner" in faction_lower:
        accent = FACTION_COLORS["Automaton"]
        emblem = "Automaton"
    elif "illuminate" in faction_lower or "vote" in faction_lower or "appropriator" in faction_lower or "cyborg" in faction_lower:
        accent = FACTION_COLORS["Illuminate"]
        emblem = "Illuminate"
    elif "super earth" in faction_lower:
        accent = FACTION_COLORS["Humans"]
        emblem = "super_earth"
    else:
        accent = FACTION_COLORS.get(faction, FACTION_COLORS["Humans"])
        emblem = "super_earth"

    card = Card(
        title=f"敌人 · {item.get('nameZh')}",
        accent=accent,
        emblem=emblem,
        footer="数据: helldivers.wiki.gg (CC BY-NC-SA)",
    )
    # 基本信息
    grid_pairs: list[tuple[str, str]] = [
        ("英文名", str(item.get("nameEn") or "?")),
        ("阵营", str(item.get("factionZh") or faction or "?")),
    ]
    enemy_class = str(item.get("enemyClass") or "").strip()
    if enemy_class:
        class_zh = _ENEMY_CLASS_ZH.get(enemy_class, enemy_class)
        grid_pairs.append(("护甲类型", class_zh))
    if item.get("size"):
        grid_pairs.append(("体型等级", str(item["size"])))
    card.grid(grid_pairs)

    # 生命值（含部位）
    parts = parse_health(str(item.get("healthRaw") or ""))
    if parts:
        card.divider().heading("生命值")
        for value, label in parts[:5]:
            card.field(label or "总血量", value)

    # 部位护甲等级（游戏 0-10 护甲体系，grid 表格展示）
    if armor_parts:
        card.divider().heading("部位护甲")
        grid_pairs: list[tuple[str, str]] = []
        for part_name, av in armor_parts[:8]:
            zh = _PART_NAME_ZH.get(part_name, part_name)
            grid_pairs.append((zh, armor_tier_label(av)))
        if grid_pairs:
            card.grid(grid_pairs)

    # 伤害信息
    dmg_parts = parse_damage(str(item.get("damageRaw") or ""))
    if dmg_parts:
        card.divider().heading("攻击方式")
        for desc, dtype in dmg_parts[:5]:
            label = f"（{dtype}）" if dtype else ""
            card.text(f"▎{desc}{label}")

    wiki = item.get("wiki") or {}
    if wiki.get("url"):
        card.divider().text(f"🔗 {wiki['url']}", muted=True)
    return card
