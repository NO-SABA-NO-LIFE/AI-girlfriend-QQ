"""战争状态聚合：星球/战役/事件索引与查询。"""

from __future__ import annotations

from typing import Any


FACTION_ALIASES: dict[str, str] = {
    "terminids": "Terminids",
    "terminid": "Terminids",
    "bugs": "Terminids",
    "bug": "Terminids",
    "终结族": "Terminids",
    "虫族": "Terminids",
    "虫子": "Terminids",
    "automaton": "Automaton",
    "automatons": "Automaton",
    "bots": "Automaton",
    "机器人": "Automaton",
    "铁卫": "Automaton",
    "illuminate": "Illuminate",
    "squids": "Illuminate",
    "光能": "Illuminate",
    "光能者": "Illuminate",
    "humans": "Humans",
    "human": "Humans",
    "super earth": "Humans",
    "人类": "Humans",
    "超级地球": "Humans",
}

FACTION_CN: dict[str, str] = {
    "Terminids": "终结族",
    "Automaton": "机器人",
    "Illuminate": "光能者",
    "Humans": "人类",
    "Super Earth": "超级地球",
}


def normalize_faction(name: str | None) -> str:
    if not name:
        return ""
    key = str(name).strip().lower()
    if key in FACTION_ALIASES:
        return FACTION_ALIASES[key]
    # 直接匹配标准名
    for std in ("Terminids", "Automaton", "Illuminate", "Humans"):
        if key == std.lower():
            return std
    return str(name).strip()


def faction_cn(name: str | None) -> str:
    n = normalize_faction(name) or (name or "")
    return FACTION_CN.get(n, n or "未知")


def planet_index(planet: dict[str, Any]) -> int | None:
    for key in ("index", "planetIndex", "id"):
        if key in planet and planet[key] is not None:
            try:
                return int(planet[key])
            except (TypeError, ValueError):
                continue
    return None


def planet_name(planet: dict[str, Any]) -> str:
    return str(planet.get("name") or planet.get("planetName") or "未知星球")


def planet_owner(planet: dict[str, Any]) -> str:
    return str(
        planet.get("currentOwner")
        or planet.get("owner")
        or planet.get("initialOwner")
        or "Unknown"
    )


def planet_players(planet: dict[str, Any]) -> int:
    stats = planet.get("statistics") or {}
    try:
        return int(stats.get("playerCount") or planet.get("players") or 0)
    except (TypeError, ValueError):
        return 0


def planet_health_pct(planet: dict[str, Any]) -> float | None:
    try:
        health = planet.get("health")
        max_health = planet.get("maxHealth")
        if health is None or max_health is None:
            return None
        max_health = float(max_health)
        if max_health <= 0:
            return None
        return max(0.0, min(100.0, float(health) / max_health * 100.0))
    except (TypeError, ValueError):
        return None


def regions_liberation_pct(planet: dict[str, Any]) -> float | None:
    """区域模式下的解放进度（0-100）。

    上游 API 改版后星球主战场恒满血，真实进度在 planet.regions 数组。
    聚合公式：sum(max-health) / sum(max)，无 regions 时返回 None。
    health 为 null 的 region 视为未开放（跳过），不能当 0 算成已解放。
    """
    regions = planet.get("regions")
    if not isinstance(regions, list) or not regions:
        return None
    total_max = 0.0
    total_health = 0.0
    for region in regions:
        if not isinstance(region, dict):
            continue
        raw_health = region.get("health")
        if raw_health is None:
            continue  # 未开放区域：无血量数据，不计入聚合
        try:
            max_health = float(region.get("maxHealth") or 0)
            health = float(raw_health)
        except (TypeError, ValueError):
            continue
        if max_health <= 0:
            continue
        total_max += max_health
        total_health += max(0.0, min(max_health, health))
    if total_max <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - total_health / total_max) * 100.0))


def planet_liberation_pct(planet: dict[str, Any]) -> float | None:
    """星球解放进度（0-100），event/regions 优先，兼容旧版主战场血量。

    API 改版过渡期两种模式并存：主战场血量损耗（旧）与 regions 聚合（新）
    都衡量距解放的距离，取最大值保证进度显示不倒退。
    """
    event = (
        planet.get("event") if isinstance(planet.get("event"), dict) else None
    )
    if event:
        try:
            eh = event.get("health")
            em = event.get("maxHealth")
            if eh is not None and em and float(em) > 0:
                return max(
                    0.0, min(100.0, (1.0 - float(eh) / float(em)) * 100.0)
                )
        except (TypeError, ValueError):
            pass
    candidates: list[float] = []
    rp = regions_liberation_pct(planet)
    if rp is not None:
        candidates.append(rp)
    hp = planet_health_pct(planet)
    if hp is not None:
        candidates.append(100.0 - hp)
    if not candidates:
        return None
    return max(candidates)


def event_faction(event: dict[str, Any] | None) -> str:
    if not event:
        return ""
    return normalize_faction(event.get("faction") or event.get("enemy") or "")


def campaign_planet(campaign: dict[str, Any]) -> dict[str, Any] | None:
    p = campaign.get("planet")
    return p if isinstance(p, dict) else None


def campaign_faction(campaign: dict[str, Any]) -> str:
    """返回战役的实际敌对阵营。

    社区 API 的普通解放战役经常把 campaign.faction 标为 Humans，
    因此事件入侵方和星球当前占领方必须优先。
    """
    p = campaign_planet(campaign) or {}
    ev = p.get("event") if isinstance(p.get("event"), dict) else None
    if ev:
        faction = event_faction(ev)
        if faction:
            return faction
    owner = normalize_faction(p.get("currentOwner") or p.get("owner"))
    if owner and owner != "Humans":
        return owner
    return normalize_faction(campaign.get("faction") or owner)


def event_planet_index(event: dict[str, Any]) -> int | None:
    planet = event.get("planet")
    if isinstance(planet, dict):
        idx = planet_index(planet)
        if idx is not None:
            return idx
    for key in ("planetIndex", "planet_index", "index"):
        try:
            if event.get(key) is not None:
                return int(event[key])
        except (TypeError, ValueError):
            continue
    return None


def planet_regen_pct_per_hour(planet: dict[str, Any]) -> float | None:
    try:
        regen = float(planet.get("regenPerSecond"))
        max_health = float(planet.get("maxHealth"))
        if max_health <= 0:
            return None
        return regen * 3600.0 / max_health * 100.0
    except (TypeError, ValueError):
        return None


class WarState:
    """从缓存原始列表构建可查询战争状态。"""

    def __init__(
        self,
        *,
        planets: list[dict[str, Any]] | None = None,
        campaigns: list[dict[str, Any]] | None = None,
        planet_events: list[dict[str, Any]] | None = None,
        space_stations: list[dict[str, Any]] | None = None,
        war: dict[str, Any] | None = None,
        global_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.planets: list[dict[str, Any]] = list(planets or [])
        self.campaigns: list[dict[str, Any]] = list(campaigns or [])
        self.planet_events: list[dict[str, Any]] = list(planet_events or [])
        self.space_stations: list[dict[str, Any]] = list(space_stations or [])
        self.war = war or {}
        self._global_events: list[dict[str, Any]] = list(global_events or [])
        self._by_index: dict[int, dict[str, Any]] = {}
        self._events_by_planet: dict[int, dict[str, Any]] = {}
        for event in self.planet_events:
            if not isinstance(event, dict):
                continue
            idx = event_planet_index(event)
            if idx is not None:
                self._events_by_planet[idx] = event
        for p in self.planets:
            idx = planet_index(p)
            if idx is not None:
                self._by_index[idx] = p

    def get_planet(self, index: int) -> dict[str, Any] | None:
        return self._by_index.get(index)

    def event_for_planet(self, planet: dict[str, Any]) -> dict[str, Any] | None:
        embedded = planet.get("event")
        if isinstance(embedded, dict):
            return embedded
        idx = planet_index(planet)
        return self._events_by_planet.get(idx) if idx is not None else None

    def find_planets(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        # 112-Name
        if "-" in q:
            head = q.split("-", 1)[0].strip()
            if head.isdigit():
                p = self.get_planet(int(head))
                return [p] if p else []
        if q.isdigit():
            p = self.get_planet(int(q))
            return [p] if p else []

        ql = q.lower()
        exact: list[dict[str, Any]] = []
        partial: list[dict[str, Any]] = []
        for p in self.planets:
            name = planet_name(p)
            nl = name.lower()
            if nl == ql:
                exact.append(p)
            elif ql in nl or nl in ql:
                partial.append(p)
        exact.sort(key=planet_players, reverse=True)
        partial.sort(key=planet_players, reverse=True)
        out = exact + [p for p in partial if p not in exact]
        return out[:limit]

    def dss_station(self) -> dict[str, Any] | None:
        if not self.space_stations:
            return None
        # 优先已知 DSS id，否则取第一个
        for s in self.space_stations:
            if int(s.get("id32") or s.get("id") or 0) == 749875195:
                return s
        return self.space_stations[0]

    def dss_planet_index(self) -> int | None:
        s = self.dss_station()
        if not s:
            return None
        planet = s.get("planet")
        if isinstance(planet, dict):
            return planet_index(planet)
        try:
            return int(s.get("planetIndex"))
        except (TypeError, ValueError):
            return None

    def campaigns_for_faction(self, faction: str) -> list[dict[str, Any]]:
        target = normalize_faction(faction)
        result = []
        for c in self.campaigns:
            if campaign_faction(c) == target:
                result.append(c)

        # 玩家数排序
        def sort_key(c: dict[str, Any]) -> int:
            p = campaign_planet(c) or {}
            return planet_players(p)

        result.sort(key=sort_key, reverse=True)
        return result

    def active_campaign_indices(self) -> set[int]:
        indices: set[int] = set()
        for c in self.campaigns:
            p = campaign_planet(c)
            if not p:
                continue
            idx = planet_index(p)
            if idx is not None:
                indices.add(idx)
        return indices

    def global_events(self) -> list[dict[str, Any]]:
        """真正的全球事件（来自 raw WarStatus.globalEvents 或 war 字段）。

        过滤掉空标题/空正文以及仅作为 MO 简报的条目。
        """
        events: list[dict[str, Any]] = []
        for e in self._global_events:
            if not isinstance(e, dict):
                continue
            title = str(e.get("title") or "").strip()
            message = str(e.get("message") or "").strip()
            if not title and not message:
                continue
            events.append(e)
        # war 对象兜底
        if not events:
            for key in ("globalEvents", "events"):
                raw = self.war.get(key)
                if isinstance(raw, list):
                    for e in raw:
                        if isinstance(e, dict) and (e.get("title") or e.get("message")):
                            events.append(e)
        return events

    def map_points(self) -> list[dict[str, Any]]:
        """地图绘制用点集。"""
        active = self.active_campaign_indices()
        dss_idx = self.dss_planet_index()
        points = []
        for p in self.planets:
            if p.get("disabled"):
                continue
            pos = p.get("position")
            if (
                not isinstance(pos, dict)
                or pos.get("x") is None
                or pos.get("y") is None
            ):
                continue
            try:
                x = float(pos["x"])
                y = float(pos["y"])
            except (KeyError, TypeError, ValueError):
                continue
            idx = planet_index(p)
            owner = planet_owner(p)
            event = self.event_for_planet(p)
            # 地图领土色用实际占领方；人类星球被入侵时用入侵方着色。
            display_owner = owner
            if event and event.get("faction") and normalize_faction(owner) == "Humans":
                display_owner = str(event.get("faction"))
            waypoints = p.get("waypoints")
            if not isinstance(waypoints, list):
                waypoints = []
            attacking = p.get("attacking")
            if not isinstance(attacking, list):
                attacking = []
            biome = p.get("biome") if isinstance(p.get("biome"), dict) else {}
            points.append(
                {
                    "index": idx,
                    "name": planet_name(p),
                    "x": x,
                    "y": y,
                    "owner": display_owner,
                    "true_owner": owner,
                    "players": planet_players(p),
                    "active": idx in active if idx is not None else False,
                    "defense": bool(event) and normalize_faction(owner) == "Humans",
                    "event": event,
                    "dss": idx is not None and idx == dss_idx,
                    "health_pct": planet_health_pct(p),
                    "waypoints": [
                        int(w)
                        for w in waypoints
                        if isinstance(w, (int, float))
                        or (isinstance(w, str) and w.isdigit())
                    ],
                    "attacking": [
                        int(w)
                        for w in attacking
                        if isinstance(w, (int, float))
                        or (isinstance(w, str) and w.isdigit())
                    ],
                    "sector": p.get("sector") or "",
                    "hash": p.get("hash") or idx or 0,
                    "biome": biome.get("name") or "",
                    "disabled": False,
                }
            )
        return points

    def _campaign_row(self, campaign: dict[str, Any]) -> dict[str, Any]:
        p = campaign_planet(campaign) or {}
        event = self.event_for_planet(p)
        if event and not isinstance(p.get("event"), dict):
            # 只复制本地视图，不能修改 API 缓存中的嵌套对象。
            p = dict(p)
            p["event"] = event
            campaign = dict(campaign)
            campaign["planet"] = p
        players = planet_players(p)
        progress = campaign_progress_pct(campaign)
        ends = event.get("endTime") or event.get("expireTime") if event else None
        idx = planet_index(p)
        regen = None if event else planet_regen_pct_per_hour(p)
        identity = f"{idx}:{event.get('id') if event else campaign.get('id')}"
        return {
            "campaign": campaign,
            "planet": p,
            "name": planet_name(p),
            "planet_name": planet_name(p),
            "index": idx,
            "planet_index": idx,
            "identity": identity,
            "campaign_id": campaign.get("id"),
            "event_id": event.get("id") if event else None,
            "owner": planet_owner(p),
            "faction": campaign_faction(campaign),
            "players": players,
            "progress": progress,
            "liberation": progress,
            "regen": regen,
            "event": event,
            "defense": bool(event) and normalize_faction(planet_owner(p)) == "Humans",
            "is_defense": bool(event)
            and normalize_faction(planet_owner(p)) == "Humans",
            "event_type": event.get("eventType") if event else None,
            "ends": ends,
            "campaign_type": campaign.get("type"),
            "dss": idx is not None and idx == self.dss_planet_index(),
        }

    def campaign_rows(self) -> list[dict[str, Any]]:
        """返回全部活跃战役的标准展示行。"""
        return [self._campaign_row(campaign) for campaign in self.campaigns]

    def warfront_rows(self, faction: str) -> list[dict[str, Any]]:
        """返回所选敌方阵营的标准战线行。"""
        target = normalize_faction(faction)
        return [
            row
            for row in self.campaign_rows()
            if normalize_faction(row.get("faction")) == target
        ]

    def dashboard_sections(self, *, low_impact_threshold: int = 800) -> dict[str, Any]:
        """GWW/Companion 风格战役分区。

        - urgent: 有事件且星球非人类占领（紧急解放等）
        - defending: 有事件且星球为人类占领（防御入侵）
        - attacking: 无事件的进攻解放，按敌对阵营分组
        """
        urgent: list[dict[str, Any]] = []
        defending: list[dict[str, Any]] = []
        attacking: dict[str, list[dict[str, Any]]] = {
            "Automaton": [],
            "Terminids": [],
            "Illuminate": [],
        }

        for c in self.campaigns:
            row = self._campaign_row(c)
            event = row["event"]
            owner = normalize_faction(row["owner"])
            if event:
                # 人类星球上的事件 = 防御入侵；敌占星球上的事件 = 紧急解放
                if owner == "Humans":
                    defending.append(row)
                else:
                    urgent.append(row)
            else:
                # 进攻解放：按星球当前敌对占领方分组
                fac = owner
                if fac in attacking:
                    attacking[fac].append(row)

        def sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(rows, key=lambda r: int(r.get("players") or 0), reverse=True)

        def split_low(
            rows: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
            rows = sort_rows(rows)
            main, low = [], []
            for r in rows:
                if int(r.get("players") or 0) < low_impact_threshold and len(main) >= 3:
                    low.append(r)
                elif (
                    int(r.get("players") or 0) < low_impact_threshold and len(rows) > 6
                ):
                    low.append(r)
                else:
                    main.append(r)
            # 保证至少展示一些
            if not main and low:
                main = low[:5]
                low = low[5:]
            other_players = sum(int(r.get("players") or 0) for r in low)
            return main, low, other_players

        urgent = sort_rows(urgent)
        defending = sort_rows(defending)
        atk_out = {}
        for fac, rows in attacking.items():
            main, low, other_p = split_low(rows)
            atk_out[fac] = {
                "main": main,
                "low": low,
                "other_players": other_p,
                "total_players": sum(int(r.get("players") or 0) for r in rows),
                "count": len(rows),
            }

        total_players = sum(planet_players(p) for p in self.planets)
        war_stats = self.war.get("statistics") if isinstance(self.war, dict) else {}
        try:
            war_players = int((war_stats or {}).get("playerCount") or 0)
        except (TypeError, ValueError):
            war_players = 0

        dss = self.dss_station()
        return {
            "urgent": urgent,
            "defending": defending,
            "attacking": atk_out,
            "total_players": war_players or total_players,
            "dss": dss,
            "dss_planet": (dss or {}).get("planet")
            if isinstance((dss or {}).get("planet"), dict)
            else {},
        }


def campaign_progress_pct(campaign: dict[str, Any]) -> float | None:
    """战役进度百分比（0-100）。

    优先 event，其次 planet.regions 聚合（API 改版后主战场恒满血），
    兼容旧版主战场血量损耗，取最大值避免过渡期进度倒退。
    """
    p = campaign_planet(campaign) or {}
    try:
        result = planet_liberation_pct(p)
        if result is not None:
            return result
    except (TypeError, ValueError):
        return None
    return None


def tactical_action_status_label(status: Any) -> str:
    try:
        s = int(status)
    except (TypeError, ValueError):
        return str(status or "未知")
    return {
        1: "准备中",
        2: "进行中",
        3: "冷却中",
        0: "未激活",
    }.get(s, f"状态{s}")


def tactical_action_progress(ta: dict[str, Any]) -> float | None:
    costs = ta.get("costs") or []
    if not isinstance(costs, list) or not costs:
        return None
    c0 = costs[0] if isinstance(costs[0], dict) else {}
    try:
        cur = float(c0.get("currentValue"))
        tgt = float(c0.get("targetValue"))
        if tgt <= 0:
            return None
        return max(0.0, min(100.0, cur / tgt * 100.0))
    except (TypeError, ValueError):
        return None
