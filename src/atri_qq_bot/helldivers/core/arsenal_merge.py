"""Build equipment records without mixing categories or upgrade conditions."""

from __future__ import annotations

import copy
import math
import re
from typing import Any


def _seconds(value: Any) -> float | None:
    """Only parse an explicitly labelled upstream duration, never arbitrary text."""
    match = re.fullmatch(r"\s*([\d,.]+)\s*(?:s|sec|seconds)\s*", str(value), re.I)
    if not match:
        return None
    number = float(match[1].replace(",", ""))
    return number if math.isfinite(number) and number >= 0 else None


def annotate_supplement(
    item: dict[str, Any], raw: dict[str, Any], collection: str, version: str
) -> dict[str, Any]:
    """Preserve explicit unknowns, units, upgrade variants and quality flags."""
    item["_collection"] = collection
    item["_data_version"] = version
    wiki = raw.get("wiki") if isinstance(raw.get("wiki"), dict) else {}
    flags = wiki.get("flags")
    item["_source_flags"] = (
        [str(flag) for flag in flags] if isinstance(flags, list) else []
    )
    stats = raw.get("statsRaw") if isinstance(raw.get("statsRaw"), dict) else {}
    firearm = raw.get("firearm") if isinstance(raw.get("firearm"), dict) else {}
    handling = item.setdefault("handling", {})
    # An explicit null must not revive an old capacity or recoil value.
    for src, dst in (
        ("capacity", "magazine"),
        ("spareMagazines", "spareMagazines"),
        ("recoil", "recoil"),
        ("reloadTimeS", "reloadSeconds"),
    ):
        if src in firearm:
            handling[dst] = firearm[src]
    if "reloadTimeUpgradedS" in firearm:
        handling["upgradedReloadSeconds"] = firearm["reloadTimeUpgradedS"]
    rates = firearm.get("fireRateRpm")
    if isinstance(rates, list) and rates:
        handling["fireRate"] = rates if len(rates) > 1 else rates[0]
    heat = bool(re.search(r"\bheat\b", str(stats.get("Traits") or ""), re.I))
    if heat:
        handling["magazine"] = None
        handling["ammunitionType"] = "热量武器（无固定弹匣容量）"
    if not handling:
        item.pop("handling", None)

    if collection == "stratagems":
        deployment = item.setdefault("deployment", {})
        for src, dst in (
            ("cooldownS", "cooldownSeconds"),
            ("callInTimeS", "callInSeconds"),
        ):
            if src in raw:
                deployment[dst] = raw[src]
        for src, dst in (
            ("General › Cooldown › Standard", "cooldownSeconds"),
            ("General › Cooldown › With All Upgrades", "upgradedCooldownSeconds"),
            ("General › Call-in Time › Standard", "callInSeconds"),
            ("General › Call-in Time › Upgraded", "upgradedCallInSeconds"),
            ("General › Rearm Time › Standard", "rearmSeconds"),
        ):
            value = _seconds(stats.get(src))
            if value is not None:
                deployment[dst] = int(value) if value.is_integer() else value
        deployment["basis"] = "base"
        for value_key, basis_key in (
            ("cooldownSeconds", "cooldownBasis"),
            ("callInSeconds", "callInBasis"),
        ):
            if value_key in deployment:
                deployment[basis_key] = "base"
    if collection == "armors":
        armor = item.setdefault("armor", {})
        for src, dst in (
            ("armorRating", "rating"),
            ("speed", "speed"),
            ("staminaRegen", "staminaRegen"),
        ):
            if src in raw:
                armor[dst] = raw[src]
        if raw.get("passiveId") and not armor.get("passive"):
            armor["passive"] = "被动资料暂缺"
    attacks = raw.get("attacks") if isinstance(raw.get("attacks"), list) else []
    for component in (item.get("combat") or {}).get("components", []):
        index_match = re.search(r"-component-(\d+)$", component["id"])
        if not index_match or int(index_match[1]) > len(attacks):
            continue
        attack = attacks[int(index_match[1]) - 1]
        if not isinstance(attack, dict):
            continue
        projectile = (
            attack.get("projectile")
            if isinstance(attack.get("projectile"), dict)
            else {}
        )
        pellets = projectile.get("pellets")
        if isinstance(pellets, (int, float)) and pellets > 1:
            component["fields"]["pellets"] = pellets
        if (
            attack.get("kind") == "beam"
            and "dps" in str(stats.get("Standard Damage") or "").lower()
        ):
            component["fields"]["damageUnit"] = "per_second"
    return item


def equipment_key(item: dict[str, Any]) -> tuple[str, str]:
    slug = str(item.get("slug") or item.get("id") or "")
    if slug.startswith("dionatha:"):
        slug = slug.split(":")[-1]
    return str(item.get("productKind") or ""), slug.casefold()


def _version_date(version: str) -> str:
    match = re.search(r"(\d{4})[.-](\d{2})[.-](\d{2})", version)
    return "".join(match.groups()) if match else ""


def merge_equipment(
    primary: list[dict[str, Any]],
    supplement: list[dict[str, Any]],
    primary_version: str = "",
) -> list[dict[str, Any]]:
    """Merge known identical items; ambiguous identities remain separate records."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in supplement:
        by_key.setdefault(equipment_key(item), []).append(item)
    merged = []
    consumed: set[int] = set()
    for old in primary:
        key = equipment_key(old)
        matches = by_key.get(key, []) if all(key) else []
        new = matches[0] if len(matches) == 1 else None
        result = copy.deepcopy(old)
        result["_source_versions"] = {"HD2Tool": primary_version or "未知"}
        result["_field_sources"] = {}
        result["_conflicts"] = []
        if new is not None:
            consumed.add(id(new))
            version = str(new.get("_data_version") or "未知")
            source = f"dionatha {version}"
            result["_source_versions"]["dionatha"] = version
            result["_source"] = "merged"
            result["_source_flags"] = list(new.get("_source_flags") or [])
            result["_updated_at"] = new.get("_updated_at", 0)
            result["_collection"] = new.get("_collection")
            result["slug"] = new.get("slug") or key[1]
            result["_search_ids"] = [str(new.get("id") or "")]
            result["alternateNames"] = list(
                dict.fromkeys(
                    [
                        *(old.get("alternateNames") or []),
                        *(new.get("alternateNames") or []),
                    ]
                )
            )
            # Prefer newer known dates; missing metadata never proves freshness.
            old_date, new_date = _version_date(primary_version), _version_date(version)
            prefer_new = bool(new_date and (not old_date or new_date >= old_date))
            for group in ("handling", "deployment", "armor"):
                incoming = new.get(group)
                if not isinstance(incoming, dict):
                    continue
                target = result.setdefault(group, {})
                if not isinstance(target, dict):
                    target = result[group] = {}
                for field, value in incoming.items():
                    path = f"{group}.{field}"
                    previous = target.get(field)
                    has_previous = field in target
                    if has_previous and previous != value and previous is not None:
                        resolved = False
                        if group == "deployment" and field in {
                            "cooldownSeconds",
                            "callInSeconds",
                        }:
                            upgrade_field = (
                                "upgradedCooldownSeconds"
                                if field == "cooldownSeconds"
                                else "upgradedCallInSeconds"
                            )
                            upgrade = incoming.get(upgrade_field)
                            resolved = (
                                isinstance(previous, (int, float))
                                and isinstance(upgrade, (int, float))
                                and abs(previous - upgrade) < 1
                            )
                        result["_conflicts"].append(
                            {
                                "field": path,
                                "HD2Tool": previous,
                                "dionatha": value,
                                "resolved_upgrade": resolved,
                            }
                        )
                    if prefer_new or not has_previous:
                        target[field] = copy.deepcopy(value)
                        result["_field_sources"][path] = source
                    else:
                        result["_field_sources"][path] = f"HD2Tool {primary_version}"
                # Do not label old unqualified values as base stats when only
                # filling missing fields from an older supplemental snapshot.
                if (
                    not prefer_new
                    and "deployment" == group
                    and "basis" not in (old.get(group) or {})
                ):
                    target.pop("basis", None)
            deployment = result.get("deployment")
            incoming_deployment = new.get("deployment") or {}
            if isinstance(deployment, dict):
                old_deployment = old.get("deployment") or {}
                for value_key, basis_key in (
                    ("cooldownSeconds", "cooldownBasis"),
                    ("callInSeconds", "callInBasis"),
                ):
                    if value_key in old_deployment and (
                        not prefer_new or value_key not in incoming_deployment
                    ):
                        deployment[basis_key] = old_deployment.get(
                            basis_key, old_deployment.get("basis", "unknown")
                        )
            if isinstance(new.get("combat"), dict) and (
                prefer_new or not result.get("combat")
            ):
                result["combat"] = copy.deepcopy(new["combat"])
                result["_field_sources"]["combat"] = source
            for field in ("description", "weaponType", "model", "wiki"):
                if not result.get(field) and new.get(field):
                    result[field] = copy.deepcopy(new[field])
            result["_footer"] = (
                f"中文/别名: HD2Tool {primary_version or '未知'} (MIT) · 数值: wiki.gg / dionatha {version} (CC BY-NC-SA)"
            )
            if not prefer_new:
                result["_footer"] = (
                    f"数据: HD2Tool {primary_version or '未知'} (MIT) · 缺项补充: wiki.gg / dionatha {version} (CC BY-NC-SA)"
                )
        else:
            result["_footer"] = f"数据: HD2Tool {primary_version or '未知'} (MIT)"
        merged.append(result)
    for item in supplement:
        if id(item) not in consumed:
            result = copy.deepcopy(item)
            result["_source_versions"] = {
                "dionatha": str(item.get("_data_version") or "未知")
            }
            merged.append(result)
    return merged
