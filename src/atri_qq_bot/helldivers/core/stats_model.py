"""Normalization and view-model helpers for galaxy-wide season statistics.

The public API intentionally accepts several generations of Helldivers data:

* the old raw ``galaxy_stats`` object;
* the raw summary wrapper ``{galaxy_stats, planets_stats}``;
* the community V1 war object with ``statistics``;
* a full V1 planets list;
* an optional raw/full war status object.

The resulting :class:`StatsViewModel` is a plain, JSON-serializable dictionary.
It has no AstrBot or rendering dependency and is safe to use with incomplete,
stale, or partially malformed API responses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Mapping, Sequence, TypedDict

JsonMapping = Mapping[str, Any]

FACTION_ORDER = ("Humans", "Terminids", "Automaton", "Illuminate")
FACTION_LABELS = {
    "Humans": "超级地球",
    "Terminids": "终结族",
    "Automaton": "机器人",
    "Illuminate": "光能者",
}
_FACTION_ALIASES = {
    "1": "Humans",
    "human": "Humans",
    "humans": "Humans",
    "super earth": "Humans",
    "superearth": "Humans",
    "2": "Terminids",
    "bug": "Terminids",
    "bugs": "Terminids",
    "terminid": "Terminids",
    "terminids": "Terminids",
    "3": "Automaton",
    "automaton": "Automaton",
    "automatons": "Automaton",
    "bot": "Automaton",
    "bots": "Automaton",
    "4": "Illuminate",
    "illuminate": "Illuminate",
    "illuminates": "Illuminate",
    "squid": "Illuminate",
    "squids": "Illuminate",
}


class PlanetSlice(TypedDict):
    name: str
    index: int | None
    players: int
    percentage: float
    faction: str


class OthersSlice(TypedDict):
    name: str
    players: int
    percentage: float
    planet_count: int


class FrontTotal(TypedDict):
    faction: str
    label: str
    players: int
    percentage: float
    planet_count: int


class MetaView(TypedDict):
    sest: str
    display_date: str
    generated_at: str
    war_day: int | None
    war_day_label: str
    war_time: int | None


class DistributionView(TypedDict):
    total_players: int
    top_planets: list[PlanetSlice]
    others: OthersSlice
    fronts: list[FrontTotal]


class StatsMetrics(TypedDict):
    missions_won: int
    missions_lost: int
    mission_time: int
    mission_time_display: str
    bullets_fired: int
    bullets_hit: int
    api_accuracy: float
    calculated_accuracy: float
    win_loss_ratio: float | None
    win_loss_display: str
    kill_death_ratio: float | None
    kill_death_display: str
    terminid_kills: int
    automaton_kills: int
    illuminate_kills: int
    total_kills: int
    deaths: int
    friendlies: int
    mission_success_rate: float


class StatsViewModel(TypedDict):
    meta: MetaView
    total_online: int
    distribution: DistributionView
    stats: StatsMetrics


_STAT_KEYS = {
    "missionsWon",
    "missionsLost",
    "missionTime",
    "timePlayed",
    "bugKills",
    "terminidKills",
    "automatonKills",
    "illuminateKills",
    "bulletsFired",
    "bulletsHit",
    "deaths",
    "friendlies",
    "missionSuccessRate",
    "accuracy",
    "accurracy",
    "playerCount",
}


def _mapping(value: Any) -> JsonMapping:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if isfinite(result) else default


def _integer(value: Any, default: int = 0) -> int:
    return max(0, int(_number(value, float(default))))


def _percentage(value: Any) -> float:
    return max(0.0, min(100.0, _number(value)))


def _index(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _first(mapping: JsonMapping, *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _first_nonzero(mapping: JsonMapping, *keys: str) -> Any:
    fallback = None
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        if fallback is None:
            fallback = value
        if _number(value) != 0.0:
            return value
    return fallback


def normalize_faction(value: Any) -> str:
    """Return one of the four canonical faction names, or ``Humans``.

    Numeric owner/race values from raw status payloads use the same 1-4 order as
    V1's faction list. Unknown owners are assigned to the human/home front so a
    malformed record never invents a fifth rendered faction.
    """

    if value is None:
        return "Humans"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        key = str(int(value))
    else:
        key = str(value).strip().lower().replace("_", " ")
    return _FACTION_ALIASES.get(key, "Humans")


def _looks_like_stats(value: JsonMapping) -> bool:
    return bool(_STAT_KEYS.intersection(value))


def _looks_like_full_planet(value: JsonMapping) -> bool:
    return any(
        key in value
        for key in ("name", "planetName", "currentOwner", "initialOwner", "event")
    )


def _extract_stats_and_planets(
    data: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Extract a statistics object and any planet records from one input."""

    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        return {}, [dict(item) for item in data if isinstance(item, Mapping)]
    root = _mapping(data)
    if not root:
        return {}, []

    stats: dict[str, Any] = {}
    planet_rows: list[dict[str, Any]] = []

    galaxy = _first(root, "galaxy_stats", "galaxyStats", "galaxyStatistics")
    if isinstance(galaxy, Mapping):
        stats.update(galaxy)
    elif isinstance(root.get("statistics"), Mapping):
        stats.update(root["statistics"])
    elif _looks_like_stats(root):
        stats.update(root)

    for key in ("planets_stats", "planet_stats", "planetsStats", "planets"):
        raw_rows = _sequence(root.get(key))
        if raw_rows:
            planet_rows.extend(
                dict(item) for item in raw_rows if isinstance(item, Mapping)
            )

    nested = root.get("data")
    if isinstance(nested, Mapping) and nested is not root:
        nested_stats, nested_planets = _extract_stats_and_planets(nested)
        for key, value in nested_stats.items():
            stats.setdefault(key, value)
        planet_rows.extend(nested_planets)

    return stats, planet_rows


def _merge_non_null(target: dict[str, Any], source: JsonMapping) -> None:
    for key, value in source.items():
        if value is not None:
            target[key] = value


def _planet_index(planet: JsonMapping) -> int | None:
    return _index(_first(planet, "index", "planetIndex", "planet_index", "id"))


def _planet_stats(planet: JsonMapping) -> JsonMapping:
    nested = planet.get("statistics")
    return nested if isinstance(nested, Mapping) else planet


def _merge_planet_rows(*row_groups: Sequence[JsonMapping]) -> list[dict[str, Any]]:
    """Merge raw summary, V1 planet, and status rows by planet index."""

    indexed: dict[int, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for rows in row_groups:
        for source in rows:
            if not isinstance(source, Mapping):
                continue
            idx = _planet_index(source)
            if idx is None:
                anonymous.append(dict(source))
                continue
            target = indexed.setdefault(idx, {"index": idx, "statistics": {}})
            current_stats = target.get("statistics")
            if not isinstance(current_stats, dict):
                current_stats = {}
                target["statistics"] = current_stats
            source_stats = _planet_stats(source)
            if source_stats is source and not _looks_like_full_planet(source):
                _merge_non_null(current_stats, source_stats)
            else:
                nested = source.get("statistics")
                if isinstance(nested, Mapping):
                    _merge_non_null(current_stats, nested)
                for key, value in source.items():
                    if key != "statistics" and value is not None:
                        target[key] = value
    return [*indexed.values(), *anonymous]


def _status_rows(status: JsonMapping) -> tuple[list[dict[str, Any]], JsonMapping]:
    rows = [
        dict(item)
        for item in _sequence(
            _first(status, "planetStatus", "planet_status", "planets")
        )
        if isinstance(item, Mapping)
    ]
    events: dict[int, JsonMapping] = {}
    for event in _sequence(_first(status, "planetEvents", "planet_events", "events")):
        if not isinstance(event, Mapping):
            continue
        idx = _index(_first(event, "planetIndex", "planet_index", "index"))
        if idx is not None:
            events[idx] = event
    for row in rows:
        idx = _planet_index(row)
        if idx is not None and idx in events:
            row["event"] = dict(events[idx])
    return rows, status


def _aggregate_planet_stats(planets: Sequence[JsonMapping]) -> dict[str, Any]:
    totals = {
        "missionsWon": 0,
        "missionsLost": 0,
        "missionTime": 0,
        "terminidKills": 0,
        "automatonKills": 0,
        "illuminateKills": 0,
        "bulletsFired": 0,
        "bulletsHit": 0,
        "deaths": 0,
        "friendlies": 0,
        "playerCount": 0,
    }
    for planet in planets:
        stats = _planet_stats(planet)
        totals["missionsWon"] += _integer(stats.get("missionsWon"))
        totals["missionsLost"] += _integer(stats.get("missionsLost"))
        totals["missionTime"] += _integer(
            _first(stats, "missionTime", "timePlayed", "mission_time")
        )
        totals["terminidKills"] += _integer(
            _first(stats, "terminidKills", "bugKills", "terminid_kills")
        )
        totals["automatonKills"] += _integer(
            _first(stats, "automatonKills", "automaton_kills")
        )
        totals["illuminateKills"] += _integer(
            _first(stats, "illuminateKills", "illuminate_kills")
        )
        totals["bulletsFired"] += _integer(
            _first(stats, "bulletsFired", "bullets_fired")
        )
        totals["bulletsHit"] += _integer(_first(stats, "bulletsHit", "bullets_hit"))
        totals["deaths"] += _integer(stats.get("deaths"))
        totals["friendlies"] += _integer(stats.get("friendlies"))
        totals["playerCount"] += _planet_players(planet)
    attempts = totals["missionsWon"] + totals["missionsLost"]
    if attempts:
        totals["missionSuccessRate"] = totals["missionsWon"] / attempts * 100.0
    fired = totals["bulletsFired"]
    if fired:
        totals["accuracy"] = totals["bulletsHit"] / fired * 100.0
    return totals


def _planet_players(planet: JsonMapping) -> int:
    stats = _planet_stats(planet)
    value = _first(
        planet,
        "players",
        "playerCount",
        "player_count",
        "activePlayers",
    )
    if value is None:
        value = _first(stats, "playerCount", "players", "player_count")
    return _integer(value)


def _planet_name(planet: JsonMapping) -> str:
    name = _first(planet, "name", "planetName", "planet_name")
    if name:
        return str(name).strip()
    idx = _planet_index(planet)
    return f"PLANET {idx}" if idx is not None else "UNKNOWN PLANET"


def _planet_faction(planet: JsonMapping) -> str:
    event = planet.get("event")
    if isinstance(event, Mapping):
        event_value = _first(event, "faction", "race", "enemy", "owner")
        if event_value is not None:
            return normalize_faction(event_value)
    return normalize_faction(
        _first(
            planet,
            "currentOwner",
            "current_owner",
            "owner",
            "initialOwner",
            "initial_owner",
        )
    )


def _war_time_seconds(status: JsonMapping, war: JsonMapping) -> int | None:
    raw = _first(status, "time", "warTime", "war_time")
    if raw is not None:
        value = int(_number(raw, -1.0))
        return value if value >= 0 else None

    raw_now = _first(war, "now", "currentTime", "current_time")
    if isinstance(raw_now, (int, float)) and not isinstance(raw_now, bool):
        value = int(_number(raw_now, -1.0))
        return value if value >= 0 else None
    if isinstance(raw_now, str):
        try:
            parsed = datetime.fromisoformat(raw_now.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = int(parsed.timestamp())
            # V1 represents raw war time as a Unix-epoch-looking date in 1970s.
            if 1970 <= parsed.year <= 1985 and timestamp >= 0:
                return timestamp
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return None


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0 小时"
    hours = seconds / 3600.0
    days = hours / 24.0
    years = days / 365.25
    if years >= 1.0:
        return f"{years:,.1f} 年"
    if days >= 1.0:
        return f"{days:,.1f} 天"
    return f"{hours:,.1f} 小时"


def _ratio(numerator: int, denominator: int) -> tuple[float | None, str]:
    if denominator > 0:
        value = numerator / denominator
        return value, f"{value:,.2f}"
    if numerator > 0:
        return None, "∞"
    return 0.0, "0.00"


def _normalization_sources(
    data: Any,
    planets: Any,
    war: Any,
    status: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], JsonMapping, JsonMapping]:
    primary_stats, primary_planets = _extract_stats_and_planets(data)
    explicit_planets = [
        dict(item) for item in _sequence(planets) if isinstance(item, Mapping)
    ]
    war_map = _mapping(war)
    status_map = _mapping(status)

    # Allow callers to pass a V1 war or raw status as the primary object.
    data_map = _mapping(data)
    if not war_map and isinstance(data_map.get("statistics"), Mapping):
        war_map = data_map
    if not status_map and any(
        key in data_map for key in ("planetStatus", "planetEvents")
    ):
        status_map = data_map
    if not status_map and any(
        key in war_map for key in ("planetStatus", "planetEvents")
    ):
        status_map = war_map

    war_stats, war_planets = _extract_stats_and_planets(war_map)
    status_planets, status_map = _status_rows(status_map)
    merged_planets = _merge_planet_rows(
        primary_planets,
        war_planets,
        explicit_planets,
        status_planets,
    )

    # Planet aggregation is the lowest-priority fallback. Explicit galaxy data
    # overrides it; V1 war.statistics then supplies the freshest non-null fields.
    merged_stats = _aggregate_planet_stats(merged_planets)
    _merge_non_null(merged_stats, primary_stats)
    _merge_non_null(merged_stats, war_stats)
    return merged_stats, merged_planets, war_map, status_map


def build_stats_view_model(
    data: Any = None,
    planets: Sequence[JsonMapping] | None = None,
    war: JsonMapping | None = None,
    status: JsonMapping | None = None,
    *,
    now: datetime | None = None,
    top_planets: int = 5,
) -> StatsViewModel:
    """Normalize supported payloads into a render-ready dictionary.

    Args:
        data: Old raw galaxy statistics, a new raw summary wrapper, a V1 war
            object, or a full planets list.
        planets: Optional full V1 planets list. It supplies names, current player
            counts, owners, and events for distribution calculations.
        war: Optional V1 war object. Its ``statistics`` fields take precedence
            when present.
        status: Optional raw/full war status. ``status.time`` determines war day;
            planet event race/faction takes precedence over planet owner.
        now: Timestamp used for the SEST display date. Naive values are treated
            as UTC. Defaults to the current time.
        top_planets: Number of named pie slices before the ``Others`` slice.
    """

    stats, planet_rows, war_map, status_map = _normalization_sources(
        data, planets, war, status
    )

    generated = now or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    generated_utc = generated.astimezone(timezone.utc)
    sest_zone = timezone(timedelta(hours=2), "SEST")
    sest = generated_utc.astimezone(sest_zone)
    super_earth_year = sest.year + 158
    display_date = f"SEST {sest:%H:%M:%S}  {sest:%d/%m}/{super_earth_year}"

    war_time = _war_time_seconds(status_map, war_map)
    direct_day = _first(status_map, "day", "warDay", "war_day")
    war_day = _integer(direct_day) if direct_day is not None else None
    if war_day == 0:
        # day=0 无意义：回退到按 war_time 推算的战争日
        war_day = None
    if war_day is None and war_time is not None:
        war_day = war_time // 86_400 + 1

    planet_entries = []
    front_players = {faction: 0 for faction in FACTION_ORDER}
    front_planets = {faction: 0 for faction in FACTION_ORDER}
    for planet in planet_rows:
        players_count = _planet_players(planet)
        if players_count <= 0:
            continue
        faction = _planet_faction(planet)
        front_players[faction] += players_count
        front_planets[faction] += 1
        planet_entries.append(
            {
                "name": _planet_name(planet),
                "index": _planet_index(planet),
                "players": players_count,
                "faction": faction,
            }
        )

    planet_entries.sort(key=lambda item: (-item["players"], item["name"]))
    distributed_total = sum(item["players"] for item in planet_entries)
    stats_online = _integer(
        _first(stats, "playerCount", "players", "totalPlayers", "total_players")
    )
    total_online = stats_online or distributed_total
    chart_total = distributed_total or total_online

    limit = max(1, min(8, int(top_planets)))
    top_rows = planet_entries[:limit]
    top_slices: list[PlanetSlice] = []
    for row in top_rows:
        top_slices.append(
            {
                "name": str(row["name"]),
                "index": row["index"],
                "players": int(row["players"]),
                "percentage": (
                    int(row["players"]) / chart_total * 100.0 if chart_total else 0.0
                ),
                "faction": str(row["faction"]),
            }
        )
    remainder = planet_entries[limit:]
    others_players = sum(int(item["players"]) for item in remainder)
    if not planet_entries and total_online:
        others_players = total_online
    others: OthersSlice = {
        "name": "其他星球",
        "players": others_players,
        "percentage": others_players / chart_total * 100.0 if chart_total else 0.0,
        "planet_count": len(remainder),
    }

    front_total = sum(front_players.values())
    front_denominator = front_total or total_online
    fronts: list[FrontTotal] = []
    for faction in FACTION_ORDER:
        players_count = front_players[faction]
        fronts.append(
            {
                "faction": faction,
                "label": FACTION_LABELS[faction],
                "players": players_count,
                "percentage": (
                    players_count / front_denominator * 100.0
                    if front_denominator
                    else 0.0
                ),
                "planet_count": front_planets[faction],
            }
        )

    missions_won = _integer(_first(stats, "missionsWon", "missions_won"))
    missions_lost = _integer(_first(stats, "missionsLost", "missions_lost"))
    mission_time = _integer(_first(stats, "missionTime", "mission_time"))
    terminid_kills = _integer(
        _first_nonzero(
            stats,
            "terminidKills",
            "bugKills",
            "terminid_kills",
            "bug_kills",
        )
    )
    automaton_kills = _integer(_first(stats, "automatonKills", "automaton_kills"))
    illuminate_kills = _integer(_first(stats, "illuminateKills", "illuminate_kills"))
    total_kills = terminid_kills + automaton_kills + illuminate_kills
    deaths = _integer(stats.get("deaths"))
    bullets_fired = _integer(_first(stats, "bulletsFired", "bullets_fired"))
    bullets_hit = _integer(_first(stats, "bulletsHit", "bullets_hit"))
    calculated_accuracy = bullets_hit / bullets_fired * 100.0 if bullets_fired else 0.0
    api_accuracy_raw = _first(
        stats,
        "accuracy",
        "accurracy",  # raw Arrowhead API's long-standing misspelling
        "apiAccuracy",
        "api_accuracy",
    )
    api_accuracy = (
        _percentage(api_accuracy_raw)
        if api_accuracy_raw is not None
        else _percentage(calculated_accuracy)
    )
    attempts = missions_won + missions_lost
    success_raw = _first(stats, "missionSuccessRate", "mission_success_rate")
    mission_success_rate = (
        _percentage(success_raw)
        if success_raw is not None
        else (missions_won / attempts * 100.0 if attempts else 0.0)
    )
    win_loss_ratio, win_loss_display = _ratio(missions_won, missions_lost)
    kill_death_ratio, kill_death_display = _ratio(total_kills, deaths)

    return {
        "meta": {
            "sest": sest.isoformat(),
            "display_date": display_date,
            "generated_at": generated_utc.isoformat(),
            "war_day": war_day,
            "war_day_label": f"战争日 {war_day:,}"
            if war_day is not None
            else "战争日 —",
            "war_time": war_time,
        },
        "total_online": total_online,
        "distribution": {
            "total_players": chart_total,
            "top_planets": top_slices,
            "others": others,
            "fronts": fronts,
        },
        "stats": {
            "missions_won": missions_won,
            "missions_lost": missions_lost,
            "mission_time": mission_time,
            "mission_time_display": _format_duration(mission_time),
            "bullets_fired": bullets_fired,
            "bullets_hit": bullets_hit,
            "api_accuracy": api_accuracy,
            "calculated_accuracy": calculated_accuracy,
            "win_loss_ratio": win_loss_ratio,
            "win_loss_display": win_loss_display,
            "kill_death_ratio": kill_death_ratio,
            "kill_death_display": kill_death_display,
            "terminid_kills": terminid_kills,
            "automaton_kills": automaton_kills,
            "illuminate_kills": illuminate_kills,
            "total_kills": total_kills,
            "deaths": deaths,
            "friendlies": _integer(stats.get("friendlies")),
            "mission_success_rate": mission_success_rate,
        },
    }


# Short aliases make later integration readable without tying callers to one name.
normalize_stats = build_stats_view_model
make_stats_view_model = build_stats_view_model

__all__ = [
    "DistributionView",
    "FACTION_LABELS",
    "FACTION_ORDER",
    "FrontTotal",
    "MetaView",
    "OthersSlice",
    "PlanetSlice",
    "StatsMetrics",
    "StatsViewModel",
    "build_stats_view_model",
    "make_stats_view_model",
    "normalize_faction",
    "normalize_stats",
]
