"""Resolve raw planet effect IDs using the bundled Community API catalog."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .._logging import logger

MAX_DISPLAY_EFFECTS = 8
_CATALOG = Path(__file__).resolve().parent.parent / "assets" / "catalogs" / "planet_effects.json"
_CAUSTIC_DESCRIPTION = (
    "带有高碱性胆囊的终结族变种已侵占这颗星球。"
    "这些敌人会喷射腐蚀性液体，并在死亡时猛烈爆裂。"
)
_NOXIOUS_DESCRIPTION = (
    "带有毒素分泌腺体的终结族变种潜伏在这颗星球上。"
    "这些敌人的毒液攻击会使目标丧失行动能力。"
)
_TRANSLATIONS = {
    1418: ("人员撤离", "超级地球正在从这颗星球撤离人员。"),
    1430: ("腐蚀变种", _CAUSTIC_DESCRIPTION),
    1431: ("腐蚀变种", _CAUSTIC_DESCRIPTION),
    1432: ("巢穴世界", "这颗星球已被终结族彻底侵占，内外均化为虫巢，是幽暗孢子的来源。"),
    1433: ("毒性变种", _NOXIOUS_DESCRIPTION),
    1434: ("毒性变种", _NOXIOUS_DESCRIPTION),
}


def _effect_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if (
        isinstance(value, str) and len(value.strip()) <= 10
        and value.strip().isascii() and value.strip().isdigit()
    ):
        return int(value)
    return None


@lru_cache(maxsize=1)
def _load_catalog() -> dict[int, dict[str, str]]:
    try:
        raw = json.loads(_CATALOG.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("planet effect catalog must be an object")
    except (OSError, ValueError) as exc:
        logger.warning("[HD2] 星球效果目录不可用，保留效果编号: %s", exc)
        return {}
    catalog = {}
    for key, value in raw.items():
        effect_id = _effect_id(key)
        if effect_id is None or not isinstance(value, dict):
            continue
        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        description = value.get("description")
        catalog[effect_id] = {
            "name": name.strip(),
            "description": description.strip() if isinstance(description, str) else "",
        }
    return catalog


def resolve_planet_effects(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep unknown IDs, merging duplicated UI/enemy definitions of an effect."""

    catalog = _load_catalog()
    resolved: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        effect_id = _effect_id(row.get("galacticEffectId"))
        if effect_id is None:
            continue
        definition = catalog.get(effect_id)
        name = definition["name"] if definition else f"未收录效果（{effect_id}）"
        description = definition["description"] if definition else ""
        identity = (
            re.sub(r"\s*\(enemies\)$", "", name.casefold()),
            description.casefold(),
        )
        if identity in seen:
            continue
        seen.add(identity)
        effect = {"id": effect_id, "name": name, "description": description}
        translated = _TRANSLATIONS.get(effect_id)
        if translated:
            effect["name_zh"], effect["description_zh"] = translated
        resolved.append(effect)
    return resolved
