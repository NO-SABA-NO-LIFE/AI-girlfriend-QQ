from __future__ import annotations

import pytest

from atri_qq_bot.helldivers.core.formatters import format_arsenal_fact

# ---- 内联 fixture：贴近真实 catalog / enemies 结构 ----


def _primary_weapon() -> dict:
    return {
        "id": "ar-2-coyote",
        "productKind": "primary-weapon",
        "model": "AR-2",
        "nameZh": "野狼",
        "nameEn": "AR-2 Coyote",
        "weaponType": "Assault Rifles",
        "acquisition": {
            "kind": "warbond",
            "warbondId": "dust-devils",
            "page": 1,
            "itemMedals": 35,
        },
        "combat": {
            "components": [
                {
                    "id": "c1",
                    "type": "projectile",
                    "label": "Ballistic",
                    "fields": {
                        "standardDamage": 75,
                        "armorPenetration": {"value": 3, "labelZh": "中型"},
                        "demolitionForce": 10,
                    },
                }
            ],
            "primaryComponentId": "c1",
        },
        "handling": {
            "magazine": 45,
            "spareMagazines": 8,
            "fireRate": 600,
            "firingModes": ["Auto", "Semi"],
        },
    }


def _grenade_multi_component() -> dict:
    return {
        "id": "g-10-incendiary",
        "productKind": "grenade",
        "model": "G-10",
        "nameZh": "燃烧弹",
        "nameEn": "G-10 Incendiary",
        "weaponType": "Standard",
        "combat": {
            "components": [
                {
                    "id": "gc1",
                    "type": "explosion",
                    "label": "Explosion",
                    "fields": {
                        "standardDamage": 300,
                        "armorPenetration": {"value": 3, "labelZh": "中型"},
                    },
                },
                {
                    "id": "gc2",
                    "type": "status",
                    "label": "Status",
                    "fields": {"dps": 100, "statusEffects": ["Fire", "BurningHeavy"]},
                },
            ],
            "primaryComponentId": "gc1",
        },
        "handling": {"magazine": 5},
    }


def _support_weapon() -> dict:
    return {
        "id": "40-k-meltagun",
        "productKind": "support-weapon",
        "model": "40-K",
        "nameZh": "40-K热熔枪",
        "nameEn": "40-K Meltagun",
        "weaponType": "Heavy Energy-Based",
        "combat": {
            "components": [
                {
                    "id": "sc1",
                    "type": "beam",
                    "label": "Beam",
                    "fields": {
                        "standardDamage": 2600,
                        "armorPenetration": {"value": 7, "labelZh": "反坦克 III"},
                        "stagger": 40,
                        "push": 40,
                    },
                }
            ],
            "primaryComponentId": "sc1",
        },
        "handling": {"magazine": 3, "fireRate": 50},
        "deployment": {
            "type": "Support Weapon",
            "code": ["down", "left", "up", "left", "left", "down"],
            "cooldownSeconds": 410,
            "callInSeconds": 7.75,
        },
    }


def _body_armor() -> dict:
    return {
        "id": "a-35-recon",
        "productKind": "body-armor",
        "model": "A-35",
        "nameZh": "A-35“侦察者”",
        "nameEn": "A-35 Recon",
        "armor": {
            "class": "Medium",
            "rating": 100,
            "speed": 500,
            "staminaRegen": 100,
            "passive": "Feet First",
        },
    }


def _stratagem() -> dict:
    return {
        "id": "a-ac-8-autocannon-sentry",
        "productKind": "other-stratagem",
        "model": "A/AC-8",
        "nameZh": "自动哨戒炮",
        "nameEn": "A/AC-8 Autocannon Sentry",
        "acquisition": {
            "kind": "requisition",
            "levelRequired": 13,
            "requisitionPoints": 6000,
        },
        "combat": {
            "components": [
                {
                    "id": "ac1",
                    "type": "projectile",
                    "label": "Ballistic",
                    "fields": {
                        "standardDamage": 450,
                        "armorPenetration": {"value": 5, "labelZh": "反坦克 I"},
                    },
                }
            ],
            "primaryComponentId": "ac1",
        },
        "deployment": {
            "type": "Sentry",
            "code": ["down", "up", "right", "up", "left", "up"],
            "cooldownSeconds": 150,
        },
    }


def _enemy() -> dict:
    return {
        "id": "enemy:bile titan",
        "productKind": "enemy",
        "nameZh": "吐酸泰坦",
        "nameEn": "Bile Titan",
        "faction": "Terminids",
        "factionZh": "虫族",
        "enemyClass": "Boss",
        "size": "5",
        "healthRaw": "6,500 (Main) <br> 2,100 (Belly)",
        "damageRaw": "60 Bile Spew +3/s Acid; 120 Melee Stomp",
    }


# ---- 装备分支 ----


def test_primary_weapon_fact_contains_key_values() -> None:
    text = format_arsenal_fact(_primary_weapon(), aliases=["野狼步枪"])
    assert "【装备事实】主武器 · 野狼" in text
    assert "型号 AR-2" in text
    assert "穿甲 中型" in text
    assert "伤害 75" in text
    assert "射速 600 RPM" in text
    assert "射击模式 Auto/Semi" in text
    assert "玩家俗称: 野狼步枪" in text
    assert "战争债券 第1页 · 35勋章" in text


def test_multi_component_marks_primary_and_lists_status() -> None:
    text = format_arsenal_fact(_grenade_multi_component())
    assert "爆炸（主）" in text
    assert "秒伤 100" in text
    assert "状态效果 Fire/BurningHeavy" in text


def test_support_weapon_renders_deployment_and_arrow_code() -> None:
    text = format_arsenal_fact(_support_weapon())
    assert "冷却 410s" in text
    assert "呼叫 7.75s" in text
    assert "输入码: ↓←↑←←↓" in text
    assert "穿甲 反坦克 III" in text


def test_body_armor_renders_armor_block() -> None:
    text = format_arsenal_fact(_body_armor())
    assert "【装备事实】护甲" in text
    assert "护甲值 100（Medium）" in text
    assert "移速 500" in text
    assert "体力回复 100" in text
    assert "被动 Feet First" in text


def test_stratagem_renders_requisition_and_code() -> None:
    text = format_arsenal_fact(_stratagem())
    assert "【装备事实】战备" in text
    assert "申请单" in text
    assert "等级13" in text
    assert "6000申请点" in text
    assert "输入码: ↓↑→↑←↑" in text


# ---- 敌人分支 ----


def test_enemy_fact_renders_faction_health_parts_and_damage() -> None:
    text = format_arsenal_fact(
        _enemy(), armor_parts=[("Main", 6), ("Belly", 3), ("Legs", 4)]
    )
    assert "【敌人事实】吐酸泰坦" in text
    assert "阵营 虫族" in text
    assert "护甲类型 Boss" in text
    # 血量部位经 _PART_NAME_ZH 中文化
    assert "生命 6,500（主体）" in text
    assert "生命 2,100（腹部）" in text
    # 部位护甲
    assert "部位 主体: 6 坦克 II" in text
    assert "部位 腹部: 3 中型" in text
    assert "部位 腿部: 4 重型" in text
    # 攻击方式与伤害类型
    assert "攻击 60 Bile Spew +3/s Acid（酸）" in text
    assert "攻击 120 Melee Stomp（近战）" in text


# ---- 脏数据清理与容错 ----


def test_firing_modes_drops_none_and_nbsp_and_dedupes() -> None:
    item = _primary_weapon()
    item["handling"]["firingModes"] = ["None", "&nbsp;Auto", "Auto", "Semi"]
    text = format_arsenal_fact(item)
    assert "None" not in text
    assert "&nbsp;" not in text
    # Auto 去重后只出现一次（射击模式片段内）
    assert "射击模式 Auto/Semi" in text


def test_missing_fields_are_tolerated() -> None:
    minimal = {"productKind": "primary-weapon", "nameZh": "测试", "nameEn": "Test"}
    text = format_arsenal_fact(minimal)
    assert isinstance(text, str)
    assert "【装备事实】主武器 · 测试" in text


def test_non_dict_input_returns_error_text() -> None:
    text = format_arsenal_fact(None)
    assert isinstance(text, str)
    assert "装备事实为空" in text


@pytest.mark.parametrize(
    "factory",
    [
        _primary_weapon,
        _grenade_multi_component,
        _support_weapon,
        _body_armor,
        _stratagem,
        _enemy,
    ],
)
def test_every_kind_produces_nonempty_text(factory) -> None:
    text = format_arsenal_fact(factory())
    assert isinstance(text, str)
    assert text.strip()
