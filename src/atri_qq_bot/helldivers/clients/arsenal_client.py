"""HD2 数据库客户端：装备（HD2Tool）+ 敌人（helldivers.wiki.gg Cargo）。

装备数据来自 SalmonC/HD2Tool (MIT) 的 catalog.json + community-aliases.json，
含武器/战备/护甲/手榴弹的官方中文译名、战斗数值和玩家俚语别名。
敌人数据来自 helldivers.wiki.gg 的 Cargo 查询（CC BY-NC-SA，需注明来源）。
缓存到 data/arsenal/，每 24 小时检查更新；GitHub 直连失败时回退镜像。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Final
from collections.abc import Callable

import aiohttp

from ..core.diagnostics import HEALTH
from ..core.persistence import finish_disk_call, write_json_atomic
from ..core.arsenal_merge import annotate_supplement, merge_equipment

from .._logging import logger

# 顺序对境内服务器有意义：前两路可直连成功，raw 直连作末位兜底
RAW_URLS: Final[tuple[str, ...]] = (
    "https://gh-proxy.org/https://raw.githubusercontent.com/SalmonC/HD2Tool/main/src/data/catalog.json",
    "https://cdn.jsdelivr.net/gh/SalmonC/HD2Tool@main/src/data/catalog.json",
    "https://raw.githubusercontent.com/SalmonC/HD2Tool/main/src/data/catalog.json",
)
ALIAS_URLS: Final[tuple[str, ...]] = (
    "https://gh-proxy.org/https://raw.githubusercontent.com/SalmonC/HD2Tool/main/"
    "src/data/community-aliases.json",
    "https://cdn.jsdelivr.net/gh/SalmonC/HD2Tool@main/src/data/community-aliases.json",
    "https://raw.githubusercontent.com/SalmonC/HD2Tool/main/"
    "src/data/community-aliases.json",
)
# wiki.gg 补充目录（dionatha 托管，每日同步，CC BY-NC-SA）：覆盖 HD2Tool 目录
# 未收录的新债券/新装备，查询未命中主目录时由同一索引兜底
DIONATHA_BASE_URL: Final[str] = "https://helldivers-api.dionatha.com.br"
DIONATHA_COLLECTIONS: Final[tuple[str, ...]] = (
    "weapons",
    "stratagems",
    "armors",
    "helmets",
    "capes",
    "boosters",
    "passives",
)
# dionatha 穿透字符串 → 游戏护甲等级（标签用 ARMOR_TIER_ZH）
_DIONATHA_PEN_MAP: Final[dict[str, int]] = {
    "unarmored": 0,
    "very_light": 1,
    "light": 2,
    "medium": 3,
    "heavy": 4,
    "anti_tank_i": 5,
    "anti_tank_ii": 6,
    "anti_tank_iii": 7,
    "anti_tank_iv": 8,
}
_MODEL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]+[-\s]?\d+[A-Za-z]*")
ENEMIES_API: Final[str] = (
    "https://helldivers.wiki.gg/api.php?action=cargoquery&tables=Enemies"
    "&fields=title,health,faction,size,class,damage&limit=500&format=json"
)
REFRESH_INTERVAL: Final[int] = 24 * 3600
# 下载体积上限：目录数据约 500KB，敌人/部位数据远小于此；16MB 上限只防
# 异常镜像返回超大响应拖垮进程内存。
MAX_DOWNLOAD_BYTES: Final[int] = 16 * 1024 * 1024
# 敌人数据下载失败后的退避间隔（秒）：避免 wiki.gg 不可用时每次查询都重试
ENEMIES_RETRY_BACKOFF: Final[int] = 15 * 60

# HD1 阵营精确值——命中的敌人条目跳过（Cyborgs/Bugs/HD1 Illuminate 等纯 HD1 老阵营）
# 用精确匹配而非子串匹配，避免误杀 Cyborg Legion / Appropriators 等 HD2 新阵营
_HD1_FACTION_EXACT: Final[frozenset[str]] = frozenset(
    {"cyborgs", "bugs", "hd1 illuminate", "super earth federation"}
)

_FACTION_ZH: Final[dict[str, str]] = {
    "Terminids": "终结族",
    "Automatons": "机器人",
    "Illuminate": "光能者",
    "Jet Brigade": "喷气旅",
    "Incineration Corps": "焚烧军团",
    "Spore Burst Strain": "孢子爆发株",
    "Rupture Strain": "破裂株",
    "Predator Strain": "掠食者株",
    "Vote Snatchers": "窃票者",
    "Cyborg Legion": "生化军团",
    "Appropriators": "掠夺者",
}

# wiki.gg Cargo Enemies 表遗漏的 HD2 敌人，手动补全
_MANUAL_ENEMIES: Final[tuple[tuple[str, str, str], ...]] = (
    ("Illuminate Overship", "Illuminate", ""),
)


def enemy_name_zh(name_en: str) -> str:
    """用官方敌人词表把英文名转中文，未收录时原样返回。"""
    try:
        from ..core.glossary import ENEMY_NAMES

        zh = ENEMY_NAMES.get(name_en.strip().casefold())
        if zh:
            return zh
    except Exception:
        pass
    return name_en


def parse_health(raw: str) -> list[tuple[str, str]]:
    """解析 wiki 敌人血量字段。

    "4,000 (Hull Main) <br> 2,100 (Turret Main)" -> [("4,000", "Hull Main"), ...]
    纯数字 "6,500" -> [("6,500", "")]
    """
    text = re.sub(r"<br\s*/?>", "\n", raw or "")
    text = re.sub(r"<[^>]+>", "", text)
    parts: list[tuple[str, str]] = []
    for chunk in text.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(
            r"([\d,\.]+)\s*(?:HP)?\s*(?:\+\s*[\d,\.]+\s*Constitution)?\s*(?:\(([^)]+)\))?",
            chunk,
        )
        if m:
            parts.append((m.group(1), (m.group(2) or "").strip()))
    return parts


# 敌人护甲类型中文映射
_ENEMY_CLASS_ZH: Final[dict[str, str]] = {
    "Light": "轻型",
    "Medium": "中型",
    "Heavy": "重型",
    "Superheavy": "超重型",
    "Miniboss": "首领",
    "Boss": "Boss",
}


# 敌人护甲等级中文映射（游戏内 0-10 护甲体系）
ARMOR_TIER_ZH: Final[dict[int, str]] = {
    0: "无甲",
    1: "超轻型",
    2: "轻型",
    3: "中型",
    4: "重型",
    5: "坦克 I",
    6: "坦克 II",
    7: "坦克 III",
    8: "坦克 IV",
    9: "坦克 V",
    10: "坦克 VI",
}


# 敌人部位名中文映射
_PART_NAME_ZH: Final[dict[str, str]] = {
    "Main": "主体",
    "Head": "头部",
    "Torso": "躯干",
    "Butt": "臀部",
    "Legs": "腿部",
    "Leg": "腿部",
    "Arms": "手臂",
    "Arm": "手臂",
    "Shoulders": "肩部",
    "Shoulder": "肩部",
    "Stomach": "腹部",
    "Belly": "腹部",
    "Pelvis": "骨盆",
    "Mouth": "口部",
    "Eye": "眼部",
    "Eyes": "眼部",
    "Weak Point": "弱点",
    "Tail": "尾部",
    "Wings": "翼部",
    "Sac": "囊袋",
    "Turret": "炮塔",
    "Hull": "车体",
    "Front": "前部",
    "Rear": "后部",
    "Side": "侧部",
    "Top": "顶部",
    "Bottom": "底部",
    "Inner": "内部",
    "Underside": "下腹",
    "Stratagem": "战备背包",
    "Cannon": "加农炮",
    "Fusion Gatling": "聚变机枪",
    "Chin Gun": "颚部机炮",
    "Mortar": "迫击炮",
    "Rocket Pods": "火箭巢",
    "Missile Pods": "导弹巢",
    "Gatling": "机枪",
    "Thruster": "推进器",
    "Eye Stem": "眼柄",
    "Pustules": "脓疱",
    "Legs (4)": "腿部",
    "Arms (2)": "手臂",
    "Legs (2)": "腿部",
    "Shoulders (2)": "肩部",
    "Abdomen": "腹部",
    "Thorax": "胸部",
    "Pilot": "驾驶员",
    "Weapon": "武器",
    "Glowing Orifice": "发光孔",
}


def armor_tier_label(value: int) -> str:
    """护甲等级数字转中文标签。"""
    return f"{value} {ARMOR_TIER_ZH.get(int(value), '未知')}"


def parse_anatomy_armor(wikitext: str) -> list[tuple[str, int]]:
    """从敌人页面的 wikitext 解析 {{Anatomy Row}} 部位护甲。

    返回 [(部位名, 护甲等级), ...]，如 [("Head", 1), ("Torso", 2)]。
    """
    parts: list[tuple[str, int]] = []
    for m in re.finditer(
        r"\{\{Anatomy Row(.*?)\}\}", wikitext or "", re.DOTALL
    ):
        block = m.group(1)
        name_m = re.search(r"\|\s*part_name\s*=\s*([^\n|]+)", block)
        av_m = re.search(r"\|\s*av\s*=\s*(\d+)", block)
        if not name_m or not av_m:
            continue
        name = name_m.group(1).strip()
        # 去掉 <br> 和括号备注
        name = re.sub(r"<br[^>]*>.*", "", name).strip()
        av = int(av_m.group(1))
        parts.append((name, av))
    return parts


def parse_damage(raw: str) -> list[tuple[str, str]]:
    """解析 wiki 敌人伤害字段，清洗维基标记。

    输入形如:
      <span class="DamageIcons">[[File:...]]</span>&nbsp;[[Damage#|60 Bile Spew +3/s Acid]]
    输出: [("60 Bile Spew +3/s Acid Burn (4s)", "酸"), ...]
    """
    text = raw or ""
    text = re.sub(r"\[\[File:[^\]]+\]\]", "", text)
    text = re.sub(r"\[\[[^|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    parts: list[tuple[str, str]] = []
    for chunk in re.split(r";\s*|\s{2,}", text):
        chunk = chunk.strip()
        if not chunk or len(chunk) < 2:
            continue
        dtype = ""
        for keyword, label in (
            ("Acid", "酸"),
            ("Ballistic", "弹道"),
            ("Explosion", "爆炸"),
            ("Melee", "近战"),
            ("Laser", "激光"),
            ("Fire", "火焰"),
            ("Gas", "毒气"),
            ("Arc", "电弧"),
            ("Energy", "能量"),
        ):
            if keyword.lower() in chunk.lower():
                dtype = label
                break
        parts.append((chunk[:60], dtype))
    return parts


def _edit_distance_le1(a: str, b: str) -> bool:
    """编辑距离是否 ≤1：改一字 / 多一字 / 少一字都算命中。

    用于中文错字容错：用户打错一个同音字（座/坐）、多打或少打
    一个字（无后坐力炮机 / 后坐力炮）仍能命中。
    """
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = sum(1 for x, y in zip(a, b) if x != y)
        return diff == 1
    # 长度差 1：双指针找插入/删除点
    if len(a) > len(b):
        a, b = b, a
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def normalize_query(text: str) -> str:
    """规范化查询/索引键：去标点 + 去空格 + 全角转半角 + 大小写折叠。

    让 "飞鹰500KG炸弹" 和 ""飞鹰"500KG炸弹"（含全角引号）都能匹配同一物品。
    """
    text = (text or "").strip()
    # 全角→半角（片假名/中文标点保留，只转 ASCII 范围的全角字符）
    text = text.translate(
        str.maketrans(
            {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
        )
    )
    # 去掉所有标点符号和空白（保留字母/数字/中文/连字符）
    text = re.sub(r"[\s\u3000\u201c\u201d\u2018\u2019\u00ab\u00bb\"'`~!@#$%^&*()+=\[\]{}|\\/:;<>,.\-_]", "", text)
    return text.casefold()

PRODUCT_KIND_ZH: Final[dict[str, str]] = {
    "primary-weapon": "主武器",
    "field-weapon": "野外武器",
    "secondary-weapon": "副武器",
    "support-weapon": "支援武器",
    "other-stratagem": "战备",
    "body-armor": "护甲",
    "grenade": "手榴弹",
    "helmet": "头盔",
    "cape": "披风",
    "booster": "增强器",
    "passive": "护甲被动",
}

FIELD_ZH: Final[dict[str, str]] = {
    "standardDamage": "伤害",
    "durableDamage": "耐久伤害",
    "armorPenetration": "穿甲",
    "dps": "秒伤",
    "innerRadius": "内半径",
    "outerRadius": "外半径",
    "demolitionForce": "拆除力",
    "stagger": "硬直",
    "push": "击退",
    "statusEffects": "状态效果",
}

COMPONENT_LABEL_ZH: Final[dict[str, str]] = {
    "Ballistic": "弹道",
    "Explosion": "爆炸",
    "Beam": "光束",
    "Melee": "近战",
    "Spray": "喷射",
    "Shrapnel": "破片",
    "Status": "状态",
}

HANDLING_ZH: Final[dict[str, str]] = {
    "magazine": "弹匣",
    "spareMagazines": "备用弹匣",
    "fireRate": "射速",
    "recoil": "后坐力",
    "firingModes": "射击模式",
    "reloadSeconds": "基础装填",
    "upgradedReloadSeconds": "升级后装填",
    "ammunitionType": "弹药机制",
}

ACQUISITION_ZH: Final[dict[str, str]] = {
    "warbond": "战争债券",
    "requisition": "申请单",
    "superstore": "超级商店",
    "default": "初始解锁",
    "event": "活动",
    "poi": "地图探索",
    "edition": "版本特典",
    "unavailable": "暂不可获取",
}


class ArsenalClient:
    """catalog.json 的下载、缓存与多路名称匹配。"""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        proxy_url: str = "",
        timeout: int = 30,
        user_agent: str = "astrbot_plugin_helldivers",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.proxy_url = proxy_url or None
        self.timeout_seconds = timeout
        self.user_agent = user_agent
        self._items: list[dict[str, Any]] = []
        self._enemies: list[dict[str, Any]] = []
        self._aliases: dict[str, list[str]] = {}
        self._index: dict[str, list[dict[str, Any]]] = {}
        self._merged_items: list[dict[str, Any]] = []
        self._refresh_task: asyncio.Task | None = None
        self._data_version: str = ""
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()
        self._enemies_lock = asyncio.Lock()
        self._enemies_failed_at: float = 0.0
        self._dionatha_items: list[dict[str, Any]] = []
        self._dionatha_loaded_at: float = 0.0
        self._dionatha_failed_at: float = 0.0
        self._dionatha_lock = asyncio.Lock()
        self._dionatha_task: asyncio.Task | None = None
        self._dionatha_snapshots: dict[str, dict[str, Any]] = {}
        self._dionatha_failures: set[str] = set()
        self._dionatha_disk_checked = False
        self._primary_failed_at = 0.0

    @property
    def available(self) -> bool:
        return bool(self._items or self._dionatha_items or self._enemies)

    @property
    def data_version(self) -> str:
        return self._data_version

    @property
    def item_count(self) -> int:
        return len(self._items)

    @property
    def dionatha_count(self) -> int:
        return len(self._dionatha_items)

    @property
    def enemy_count(self) -> int:
        return len(self._enemies)

    async def ensure_loaded(self, *, force: bool = False, refresh_supplement: bool = True) -> bool:
        """Keep usable caches while independently refreshing both equipment sources."""
        if refresh_supplement:
            self._maybe_start_dionatha()
        async with self._lock:
            catalog_path = self.cache_dir / "catalog.json"
            aliases_path = self.cache_dir / "aliases.json"
            if not self._items and self._primary_cache_path().is_file():
                self._load_from(catalog_path, aliases_path)
            if self._primary_cache_path().is_file():
                self._loaded_at = self._primary_cache_path().stat().st_mtime
            missing_aliases = not (self.cache_dir / "primary_snapshot.json").is_file() and not aliases_path.is_file()
            stale = force or not self._items or missing_aliases or time.time() - self._loaded_at >= REFRESH_INTERVAL
            backoff = time.time() - self._primary_failed_at < ENEMIES_RETRY_BACKOFF
            if stale and (force or not backoff):
                if await self._download(catalog_path, aliases_path):
                    self._load_from(catalog_path, aliases_path)
                    self._loaded_at = self._primary_cache_path().stat().st_mtime
                    self._primary_failed_at = 0.0
                else:
                    self._primary_failed_at = time.time()
                    # A catalog may have refreshed successfully while aliases failed.
                    if self._primary_cache_path().is_file():
                        self._load_from(catalog_path, aliases_path)
                        self._loaded_at = self._primary_cache_path().stat().st_mtime
            return self.available

    async def wait_for_supplement(self, timeout: float = 3.0) -> bool:
        """Wait briefly for the shared task without cancelling background refresh."""
        self._maybe_start_dionatha()
        task = self._dionatha_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except asyncio.TimeoutError:
                pass
        return bool(self._dionatha_items)

    @staticmethod
    def _validate_primary(body: bytes | str, *, aliases: bool = False) -> dict[str, Any]:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("primary payload must be an object")
        rows = payload.get("entries" if aliases else "items")
        if not isinstance(rows, list) or (not aliases and not rows):
            raise ValueError("invalid primary rows")
        if not isinstance(payload.get("meta", {}), (dict, type(None))):
            raise ValueError("invalid primary metadata")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("primary row must be an object")
            key = row.get("equipmentId" if aliases else "id")
            if not isinstance(key, str) or not key.strip():
                raise ValueError("invalid primary ID")
            if not aliases and key in seen:
                raise ValueError("duplicate primary ID")
            seen.add(key)
            names = row.get("aliases" if aliases else "alternateNames")
            if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
                raise ValueError("invalid primary alternate names")
            if not aliases:
                for field in ("handling", "deployment", "armor", "combat"):
                    if row.get(field) is not None and not isinstance(row[field], dict):
                        raise ValueError(f"invalid primary {field}")
        return payload

    @classmethod
    def _valid_primary(cls, body: bytes, *, aliases: bool = False) -> bool:
        try:
            cls._validate_primary(body, aliases=aliases)
            return True
        except (ValueError, AttributeError, TypeError):
            return False

    def _primary_candidate(self, catalog: dict, aliases: dict):
        candidate = copy.copy(self)
        candidate._items = catalog["items"]
        candidate._data_version = str((catalog.get("meta") or {}).get("dataVersion") or "")
        candidate._aliases = {row["equipmentId"]: list(row.get("aliases") or []) for row in aliases["entries"]}
        candidate._build_index()
        return candidate

    async def _download(self, catalog_path: Path, aliases_path: Path) -> bool:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": self.user_agent}, timeout=timeout, trust_env=False) as session:
                payloads = []
                for urls, is_alias in ((RAW_URLS, False), (ALIAS_URLS, True)):
                    body = await self._fetch_any(session, urls, proxy=self.proxy_url,
                        validate=lambda data, flag=is_alias: self._valid_primary(data, aliases=flag))
                    if body is None:
                        raise ValueError("目录或别名更新失败")
                    payloads.append(self._validate_primary(body, aliases=is_alias))
                self._primary_candidate(*payloads)
                # One atomic file is authoritative; never expose a half-updated pair.
                await finish_disk_call(write_json_atomic, self.cache_dir / "primary_snapshot.json",
                                       {"catalog": payloads[0], "aliases": payloads[1]})
            HEALTH.record_ok("hd2tool", detail="catalog+aliases 快照更新成功")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[HD2] arsenal refresh rejected: {type(exc).__name__}: {exc}; keeping last good snapshot")
            HEALTH.record_fail("hd2tool", detail=str(exc))
            return False

    @staticmethod
    async def _read_capped(resp: aiohttp.ClientResponse) -> bytes:
        """读取响应体，超过 MAX_DOWNLOAD_BYTES 时截断报错，防异常镜像 OOM。"""
        length = resp.content_length
        if length is not None and length > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"response too large: {length} > {MAX_DOWNLOAD_BYTES}"
            )
        chunks: list[bytes] = []
        received = 0
        async for chunk in resp.content.iter_chunked(256 * 1024):
            received += len(chunk)
            if received > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"response exceeded {MAX_DOWNLOAD_BYTES} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _fetch_any(
        session: aiohttp.ClientSession,
        urls: tuple[str, ...],
        *,
        proxy: str | None = None,
        validate: Callable[[bytes], bool] | None = None,
    ) -> bytes | None:
        """依次尝试多个镜像 URL，返回首个成功的响应体。

        proxy 必须显式传入：session 统一 trust_env=False，不读环境变量，
        境内用户靠 proxy_url 兜底 raw.githubusercontent.com 末位直连。
        """
        for url in urls:
            try:
                async with session.get(url, proxy=proxy) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"[HD2] arsenal download HTTP {resp.status}: {url}"
                        )
                        continue
                    body = await ArsenalClient._read_capped(resp)
                    if validate is None or validate(body):
                        return body
                    logger.warning(f"[HD2] arsenal mirror returned invalid data: {url}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[HD2] arsenal mirror failed ({url}): {type(exc).__name__}: {exc}"
                )
        return None

    # ---- wiki.gg 补充目录（dionatha）----

    @staticmethod
    def _dionatha_to_item(
        collection: str,
        raw: dict[str, Any],
        data_version: str,
        passives_by_id: dict[str, str],
    ) -> dict[str, Any] | None:
        """wiki.gg 补充条目 → HD2Tool 伪条目，复用既有卡片渲染与索引。"""

        name = str(raw.get("name") or "").strip()
        slug = str(raw.get("slug") or raw.get("id") or "").strip()
        if not name or not slug:
            return None
        model_m = _MODEL_RE.match(name)
        wiki = raw.get("wiki") if isinstance(raw.get("wiki"), dict) else {}
        item: dict[str, Any] = {
            "id": f"dionatha:{slug}",
            "slug": slug,
            "model": model_m.group(0).upper().replace(" ", "-") if model_m else "",
            "nameZh": "",
            "nameEn": name,
            "weaponType": "",
            "alternateNames": [a for a in raw.get("aliases") or [] if a],
            "wiki": {"url": wiki.get("url")},
            "description": str(raw.get("description") or "")[:200],
            "_source": "dionatha",
            "_footer": f"数据: wiki.gg 补充目录 (dionatha {data_version}) · CC BY-NC-SA",
        }

        if collection == "weapons":
            category = str(raw.get("category") or "")
            item["productKind"] = {
                "primary": "primary-weapon",
                "secondary": "secondary-weapon",
                "support": "support-weapon",
                "throwable": "grenade",
                "civilian": "field-weapon",
            }.get(category, "")
            if category == "civilian" and slug in {"cqc-72-entrenchment-tool", "sg-88-break-action-shotgun"}:
                item["productKind"] = "support-weapon"
            item["weaponType"] = str(raw.get("subcategory") or "").replace("_", " ").title()
        elif collection == "stratagems":
            kind = str(raw.get("kind") or "")
            item["productKind"] = "support-weapon" if kind == "support_weapon" else "other-stratagem"
            item["weaponType"] = kind.replace("_", " ").title()
        elif collection == "armors":
            item["productKind"] = "body-armor"
        elif collection == "helmets":
            item["productKind"] = "helmet"
        elif collection == "capes":
            item["productKind"] = "cape"
        elif collection == "boosters":
            item["productKind"] = "booster"
        elif collection == "passives":
            item["productKind"] = "passive"

        # 战斗组件：attacks[] → combat.components（主组件取首个）
        components: list[dict[str, Any]] = []
        attacks = raw.get("attacks") if isinstance(raw.get("attacks"), list) else []
        for index, attack in enumerate(attacks):
            if not isinstance(attack, dict):
                continue
            damage = attack.get("damage") if isinstance(attack.get("damage"), dict) else {}
            forces = attack.get("forces") if isinstance(attack.get("forces"), dict) else {}
            penetration = (
                attack.get("penetration")
                if isinstance(attack.get("penetration"), dict)
                else {}
            )
            fields: dict[str, Any] = {}
            if damage.get("standard") is not None:
                fields["standardDamage"] = damage["standard"]
            if damage.get("durable") is not None:
                fields["durableDamage"] = damage["durable"]
            pen_direct = str(penetration.get("direct") or "")
            if pen_direct:
                pen_value = _DIONATHA_PEN_MAP.get(pen_direct)
                fields["armorPenetration"] = {
                    "value": pen_value,
                    "labelZh": ARMOR_TIER_ZH.get(pen_value, pen_direct)
                    if pen_value is not None
                    else pen_direct,
                }
            area = attack.get("area") if isinstance(attack.get("area"), dict) else {}
            if area.get("innerRadiusM") is not None:
                fields["innerRadius"] = area["innerRadiusM"]
            if area.get("outerRadiusM") is not None:
                fields["outerRadius"] = area["outerRadiusM"]
            for src, dst in (
                ("demolition", "demolitionForce"),
                ("stagger", "stagger"),
                ("push", "push"),
            ):
                if forces.get(src) is not None:
                    fields[dst] = forces[src]
            if fields:
                components.append(
                    {
                        "id": f"{slug}-component-{index + 1}",
                        "type": str(attack.get("kind") or "projectile"),
                        "label": str(damage.get("type") or attack.get("name") or ""),
                        "fields": fields,
                    }
                )
        if components:
            item["combat"] = {
                "components": components,
                "primaryComponentId": components[0]["id"],
            }

        # 操控属性（枪械）
        firearm = raw.get("firearm") if isinstance(raw.get("firearm"), dict) else {}
        if firearm:
            handling: dict[str, Any] = {}
            if firearm.get("capacity") is not None:
                handling["magazine"] = firearm["capacity"]
            if firearm.get("spareMagazines") is not None:
                handling["spareMagazines"] = firearm["spareMagazines"]
            rpm = firearm.get("fireRateRpm")
            if isinstance(rpm, list) and rpm:
                handling["fireRate"] = rpm[0]
            if firearm.get("recoil") is not None:
                handling["recoil"] = firearm["recoil"]
            modes = firearm.get("firingModes")
            if isinstance(modes, list):
                handling["firingModes"] = [str(mode).title() for mode in modes if mode]
            if firearm.get("reloadTimeS") is not None:
                handling["reloadSeconds"] = firearm["reloadTimeS"]
            if handling:
                item["handling"] = handling

        # 战备部署（冷却/调用时间/代码）
        if collection == "stratagems":
            deployment: dict[str, Any] = {}
            if isinstance(raw.get("code"), list):
                deployment["code"] = raw["code"]
            if raw.get("cooldownS") is not None:
                deployment["cooldownSeconds"] = raw["cooldownS"]
            if raw.get("callInTimeS") is not None:
                deployment["callInSeconds"] = raw["callInTimeS"]
            if deployment:
                deployment["type"] = str(raw.get("kind") or "stratagem").replace("_", " ").title()
                item["deployment"] = deployment

        # 护甲数值 + 被动名解析
        if collection == "armors":
            armor: dict[str, Any] = {}
            if raw.get("armorRating") is not None:
                armor["rating"] = raw["armorRating"]
            if raw.get("speed") is not None:
                armor["speed"] = raw["speed"]
            if raw.get("staminaRegen") is not None:
                armor["staminaRegen"] = raw["staminaRegen"]
            passive = passives_by_id.get(str(raw.get("passiveId") or ""))
            if passive:
                armor["passive"] = passive
            if armor:
                item["armor"] = armor
        elif collection == "boosters":
            if raw.get("effect"):
                item["description"] = (
                    str(raw["effect"])[:160] + " " + str(item.get("description") or "")
                ).strip()

        return annotate_supplement(item, raw, collection, data_version)

    async def _fetch_dionatha(
        self, session: aiohttp.ClientSession, collection: str
    ) -> bytes | None:
        url = f"{DIONATHA_BASE_URL}/v1/{collection}.json"
        try:
            async with session.get(url, proxy=self.proxy_url) as resp:
                if resp.status != 200:
                    logger.warning(f"[HD2] dionatha {collection} HTTP {resp.status}")
                    return None
                return await self._read_capped(resp)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"[HD2] dionatha {collection} failed: {type(exc).__name__}: {exc}"
            )
            return None

    @staticmethod
    def _dionatha_snapshot(body: bytes, updated_at: float) -> dict[str, Any]:
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
            raise ValueError("invalid collection metadata")
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows or not all(
            isinstance(row, dict) and isinstance(row.get("name"), str)
            and row["name"].strip() and (row.get("slug") or row.get("id"))
            for row in rows
        ):
            raise ValueError("invalid or empty collection")
        for row in rows:
            names = row.get("aliases")
            if names is not None and (not isinstance(names, list) or not all(isinstance(name, str) for name in names)):
                raise ValueError("invalid supplemental aliases")
        return {"rows": rows, "version": str(payload["meta"].get("dataVersion") or "未知"), "updated_at": updated_at}

    @classmethod
    def _read_dionatha_disk(cls, cache_dir: Path) -> dict[str, dict[str, Any]]:
        """Load each last good collection, including expired caches after restart."""
        snapshots = {}
        for collection in DIONATHA_COLLECTIONS:
            path = cache_dir / f"dionatha_{collection}.json"
            try:
                snapshot = cls._dionatha_snapshot(path.read_bytes(), path.stat().st_mtime)
                for raw in snapshot["rows"]:
                    cls._dionatha_to_item(collection, raw, snapshot["version"], {})
                snapshots[collection] = snapshot
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                continue
        bundle_path = cache_dir / "dionatha_armor_snapshot.json"
        if bundle_path.is_file():
            try:
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                pair = {}
                for name in ("passives", "armors"):
                    if name not in bundle:
                        continue
                    entry = bundle[name]
                    body = json.dumps({"meta": {"dataVersion": entry["version"]}, "data": entry["rows"]}).encode()
                    snapshot = cls._dionatha_snapshot(body, float(entry["updated_at"]))
                    for row in snapshot["rows"]:
                        cls._dionatha_to_item(name, row, snapshot["version"], {})
                    pair[name] = snapshot
                if not cls._armor_references_valid(pair):
                    raise ValueError("invalid cached armor dependency")
                snapshots.update(pair)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                logger.warning(f"[HD2] armor snapshot unusable: {exc}")
        return snapshots

    @staticmethod
    def _armor_references_valid(snapshots: dict) -> bool:
        known = {str(row.get("id") or row.get("slug")) for row in snapshots.get("passives", {}).get("rows", [])}
        return all(not row.get("passiveId") or str(row["passiveId"]) in known
                   for row in snapshots.get("armors", {}).get("rows", []))

    def _publish_dionatha(self, snapshots: dict[str, dict[str, Any]]) -> None:
        passives = {str(row.get("id") or row.get("slug")): str(row["name"])
                    for row in snapshots.get("passives", {}).get("rows", [])}
        items = []
        for collection in DIONATHA_COLLECTIONS:
            snapshot = snapshots.get(collection)
            if snapshot is None:
                continue
            for raw in snapshot["rows"]:
                item = self._dionatha_to_item(collection, raw, snapshot["version"], passives)
                if item:
                    item["_collection"] = collection
                    item["_data_version"] = snapshot["version"]
                    item["_updated_at"] = snapshot["updated_at"]
                    items.append(item)
        # No await between publishing items and index: readers see a complete view.
        self._dionatha_snapshots = snapshots
        self._dionatha_items = items
        self._dionatha_loaded_at = min((v["updated_at"] for v in snapshots.values()), default=0)
        self._build_index()

    async def _try_load_dionatha_disk(self) -> bool:
        snapshots = await asyncio.to_thread(self._read_dionatha_disk, self.cache_dir)
        existing = getattr(self, "_dionatha_snapshots", {})
        for collection, snapshot in existing.items():
            if snapshot["updated_at"] > snapshots.get(collection, {}).get("updated_at", 0):
                snapshots[collection] = snapshot
        if snapshots:
            self._publish_dionatha(snapshots)
        self._dionatha_disk_checked = True
        return bool(self._dionatha_items)

    async def ensure_dionatha(self, *, force: bool = False) -> bool:
        """Refresh due collections; partial failure never drops last good rows."""
        async with self._dionatha_lock:
            if not self._dionatha_disk_checked:
                await self._try_load_dionatha_disk()
            now = time.time()
            snapshots = dict(self._dionatha_snapshots)
            due = [c for c in DIONATHA_COLLECTIONS if force or c not in snapshots
                   or now - snapshots[c]["updated_at"] >= REFRESH_INTERVAL
                   or c in self._dionatha_failures]
            if not due:
                return bool(self._dionatha_items)
            if not force and self._dionatha_failed_at and now - self._dionatha_failed_at < ENEMIES_RETRY_BACKOFF:
                return bool(self._dionatha_items)
            candidates: dict[str, tuple[dict[str, Any], bytes]] = {}
            failures = set()
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds * 2)
            try:
                async with aiohttp.ClientSession(headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                                               timeout=timeout, trust_env=False) as session:
                    for collection in due:
                        try:
                            body = await self._fetch_dionatha(session, collection)
                            if body is None:
                                raise ValueError("download failed")
                            snapshot = self._dionatha_snapshot(body, time.time())
                            # Validate conversion before touching disk or the current snapshot.
                            for raw in snapshot["rows"]:
                                if self._dionatha_to_item(collection, raw, snapshot["version"], {}) is None:
                                    raise ValueError("unusable row")
                            candidates[collection] = (snapshot, body)
                        except (ValueError, TypeError, AttributeError, KeyError) as exc:
                            failures.add(collection)
                            logger.warning(f"[HD2] dionatha {collection}: {exc}; keeping cached collection")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures.update(set(due) - candidates.keys())
                logger.warning(f"[HD2] dionatha refresh failed: {exc}")
            # Validate related candidates together. A rename must not be checked
            # against the old half of the pair. Publish the pair as one atomic file.
            related = {name: candidates.pop(name) for name in ("passives", "armors") if name in candidates}
            if related:
                pair = {name: snapshots[name] for name in ("passives", "armors") if name in snapshots}
                proposed = {**pair, **{name: value[0] for name, value in related.items()}}
                if not self._armor_references_valid(proposed):
                    # An invalid new armor must not block an independently safe passive update.
                    if "armors" in related:
                        failures.add("armors")
                        related.pop("armors")
                    proposed = {**pair, **{name: value[0] for name, value in related.items()}}
                    if not self._armor_references_valid(proposed):
                        failures.update(related)
                        related.clear()
                if related:
                    try:
                        await finish_disk_call(write_json_atomic, self.cache_dir / "dionatha_armor_snapshot.json", proposed)
                        snapshots.update(proposed)
                    except OSError as exc:
                        failures.update(related)
                        logger.warning(f"[HD2] armor snapshot write failed: {exc}")
            for collection, (snapshot, body) in candidates.items():
                try:
                    path = self.cache_dir / f"dionatha_{collection}.json"
                    await finish_disk_call(write_json_atomic, path, json.loads(body))
                    snapshot["updated_at"] = path.stat().st_mtime
                    snapshots[collection] = snapshot
                except OSError as exc:
                    failures.add(collection)
                    logger.warning(f"[HD2] dionatha {collection} cache write failed: {exc}")
            self._publish_dionatha(snapshots)
            self._dionatha_failures = failures
            self._dionatha_failed_at = time.time() if failures else 0.0
            if failures:
                HEALTH.record_fail("dionatha", detail=f"部分更新失败: {', '.join(sorted(failures))}；保留旧数据")
            else:
                HEALTH.record_ok("dionatha", detail=f"{len(snapshots)}/7 集合 {len(self._dionatha_items)} 条")
            return bool(self._dionatha_items)

    async def stop(self) -> None:
        """取消后台补充目录同步任务（插件卸载时调用）。"""
        tasks = [task for task in (self._refresh_task, self._dionatha_task) if task is not None]
        self._refresh_task = self._dionatha_task = None
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _maybe_start_dionatha(self) -> None:
        task = self._dionatha_task
        if task is not None and not task.done():
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._dionatha_task = asyncio.create_task(
            self.ensure_dionatha(), name="hd2-dionatha-sync"
        )

    def _load_from(self, catalog_path: Path, aliases_path: Path) -> bool:
        try:
            bundle_path = self.cache_dir / "primary_snapshot.json"
            if bundle_path.is_file():
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                catalog_body = json.dumps(bundle["catalog"])
                aliases_body = json.dumps(bundle["aliases"])
            else:
                catalog_body = catalog_path.read_text(encoding="utf-8")
                aliases_body = aliases_path.read_text(encoding="utf-8") if aliases_path.is_file() else '{"entries":[]}'
            catalog = self._validate_primary(catalog_body)
            aliases = self._validate_primary(aliases_body, aliases=True)
            candidate = self._primary_candidate(catalog, aliases)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning(f"[HD2] arsenal snapshot unusable: {exc}")
            return False
        # Publish only after both payloads and the complete index are usable.
        self._items, self._aliases = candidate._items, candidate._aliases
        self._data_version = candidate._data_version
        self._index, self._merged_items = candidate._index, candidate._merged_items
        return True

    def _primary_cache_path(self) -> Path:
        bundle = self.cache_dir / "primary_snapshot.json"
        return bundle if bundle.is_file() else self.cache_dir / "catalog.json"

    async def ensure_enemies(self, *, force: bool = False) -> None:
        """加载敌人数据（wiki.gg Cargo）；失败静默——装备查询不受影响。

        下载失败后进入 15 分钟退避，避免 wiki.gg 不可用时每次查询都
        阻塞等待超时；并发调用由锁串行化，防止重复下载与 tmp 文件竞争。
        """
        async with self._enemies_lock:
            await self._ensure_enemies_locked(force=force)

    async def _ensure_enemies_locked(self, *, force: bool = False) -> None:
        enemies_path = self.cache_dir / "enemies.json"
        # 判断是否需要重新下载：文件不存在 / TTL 过期 / 缓存版本旧（无 class/damage 字段）
        needs_refresh = force or not enemies_path.is_file()
        if not needs_refresh:
            age = time.time() - enemies_path.stat().st_mtime
            if age > REFRESH_INTERVAL:
                needs_refresh = True
            else:
                # 检查缓存是否包含 class/damage 字段（旧版 API 缓存没有）
                try:
                    cached = json.loads(enemies_path.read_text(encoding="utf-8"))
                    first = (
                        (cached.get("cargoquery") or [{}])[0].get("title", {})
                        if isinstance(cached, dict)
                        else {}
                    )
                    if "class" not in first or "damage" not in first:
                        needs_refresh = True
                except (OSError, ValueError, IndexError, TypeError):
                    needs_refresh = True
        if needs_refresh:
            # 失败退避：上次下载失败后 15 分钟内不再尝试
            if not force and time.time() - self._enemies_failed_at < ENEMIES_RETRY_BACKOFF:
                logger.debug("[HD2] enemies download in failure backoff")
            else:
                timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
                try:
                    async with aiohttp.ClientSession(
                        headers={"User-Agent": self.user_agent},
                        timeout=timeout,
                        trust_env=False,
                    ) as session:
                        async with session.get(
                            ENEMIES_API, proxy=self.proxy_url
                        ) as resp:
                            if resp.status == 200:
                                body = await self._read_capped(resp)
                                payload = json.loads(body)
                                if not isinstance(payload, dict) or not isinstance(payload.get("cargoquery"), list) or not payload["cargoquery"]:
                                    raise ValueError("invalid enemies response")
                                tmp = enemies_path.with_suffix(".tmp")
                                tmp.write_bytes(body)
                                tmp.replace(enemies_path)
                                self._enemies_failed_at = 0.0
                                HEALTH.record_ok("wiki", detail="enemies cargoquery")
                            else:
                                self._enemies_failed_at = time.time()
                                logger.warning(
                                    f"[HD2] enemies download HTTP {resp.status}"
                                )
                                HEALTH.record_fail(
                                    "wiki", detail=f"enemies HTTP {resp.status}"
                                )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._enemies_failed_at = time.time()
                    logger.warning(
                        f"[HD2] enemies download failed: {type(exc).__name__}: {exc}"
                    )
                    HEALTH.record_fail("wiki", detail=f"enemies: {exc}")
        try:
            raw = json.loads(enemies_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._enemies = []
        for row in (
            raw.get("cargoquery") or [] if isinstance(raw, dict) else []
        ):
            entry = row.get("title") if isinstance(row, dict) else None
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("title") or "").strip()
            faction = str(entry.get("faction") or "").strip()
            if not name or not faction:
                continue
            # 跳过 HD1 敌人（Cyborgs/Bugs/HD1 Illuminate 等老阵营）
            faction_key = faction.casefold()
            if faction_key in _HD1_FACTION_EXACT:
                continue
            self._enemies.append(
                {
                    "id": f"enemy:{name.casefold()}",
                    "productKind": "enemy",
                    "nameZh": enemy_name_zh(name),
                    "nameEn": name,
                    "faction": faction,
                    "factionZh": _FACTION_ZH.get(faction, faction),
                    "healthRaw": str(entry.get("health") or "").strip(),
                    "size": str(entry.get("size") or "").strip(),
                    "enemyClass": str(entry.get("class") or "").strip(),
                    "damageRaw": str(entry.get("damage") or "").strip(),
                    "wiki": {"url": f"https://helldivers.wiki.gg/wiki/{name.replace(' ', '_')}"},
                }
            )
        # wiki.gg Cargo 表遗漏的敌人条目手动补全
        cargo_names = {e["nameEn"] for e in self._enemies}
        for name, faction, health in _MANUAL_ENEMIES:
            if name not in cargo_names:
                self._enemies.append(
                    {
                        "id": f"enemy:{name.casefold()}",
                        "productKind": "enemy",
                        "nameZh": enemy_name_zh(name),
                        "nameEn": name,
                        "faction": faction,
                    "factionZh": _FACTION_ZH.get(faction, faction),
                    "healthRaw": health,
                    "size": "",
                    "enemyClass": "",
                    "damageRaw": "",
                    "wiki": {"url": f"https://helldivers.wiki.gg/wiki/{name.replace(' ', '_')}"},
                    }
                )
        self._build_index()

    def enemies_available(self) -> bool:
        return bool(getattr(self, "_enemies", None))

    async def fetch_enemy_armor(self, name_en: str) -> list[tuple[str, int]]:
        """按需拉取敌人页面的部位护甲（带缓存，TTL 与主数据一致）。

        返回 [(部位名, 护甲等级), ...]；页面无 Anatomy Row 或请求失败时为空。
        同一锁串行化并发查询，防止对同一敌人重复下载。
        """
        import urllib.parse

        cache_path = (
            self.cache_dir / f"armor_{hashlib.sha1(name_en.encode()).hexdigest()[:16]}.json"
        )
        async with self._enemies_lock:
            if cache_path.is_file() and (
                time.time() - cache_path.stat().st_mtime < REFRESH_INTERVAL
            ):
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(cached, list):
                        return [(str(p), int(a)) for p, a in cached]
                except (OSError, ValueError, TypeError):
                    pass
            api = (
                f"https://helldivers.wiki.gg/api.php?action=parse&page="
                f"{urllib.parse.quote(name_en, safe='')}&prop=wikitext&format=json"
            )
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            wikitext = ""
            try:
                async with aiohttp.ClientSession(
                    headers={"User-Agent": self.user_agent},
                    timeout=timeout,
                    trust_env=False,
                ) as session:
                    async with session.get(api, proxy=self.proxy_url) as resp:
                        if resp.status == 200:
                            data = json.loads(
                                await self._read_capped(resp)
                            )
                            wikitext = str(
                                (
                                    (data.get("parse") or {}).get("wikitext")
                                    or {}
                                ).get("*", "")
                                if isinstance(data, dict)
                                else ""
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[HD2] enemy armor fetch failed ({name_en}): {exc}"
                )
                return []
            parts = parse_anatomy_armor(wikitext)
            if parts:
                try:
                    tmp = cache_path.with_suffix(".tmp")
                    tmp.write_text(
                        json.dumps(parts, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    tmp.replace(cache_path)
                except OSError:
                    pass
            return parts

    async def load_cached(self) -> None:
        """Load diagnostics from disk without making network requests."""
        async with self._lock:
            path = self._primary_cache_path()
            if not self._items and path.is_file():
                self._load_from(path, self.cache_dir / "aliases.json")
                self._loaded_at = path.stat().st_mtime
        async with self._dionatha_lock:
            if not self._dionatha_disk_checked:
                await self._try_load_dionatha_disk()

    async def refresh_all(self) -> list[str]:
        """Coalesce concurrent administrator refreshes into one shared operation."""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_sources(), name="hd2-arsenal-refresh")
        return await asyncio.shield(self._refresh_task)

    async def _refresh_sources(self) -> list[str]:
        active = self._dionatha_task
        if active is not None and not active.done():
            await asyncio.shield(active)
        results = await asyncio.gather(self.ensure_loaded(force=True, refresh_supplement=False), self.ensure_dionatha(force=True),
                                       self.ensure_enemies(force=True), return_exceptions=True)
        for label, result in zip(("hd2tool", "dionatha", "wiki"), results):
            if isinstance(result, Exception):
                HEALTH.record_fail(label, detail=f"刷新异常: {type(result).__name__}")
        return self.diagnostic_lines()

    def diagnostic_lines(self) -> list[str]:
        from datetime import datetime, timezone
        def timestamp(value: float) -> str:
            return datetime.fromtimestamp(value, timezone.utc).strftime("%m-%d %H:%M UTC") if value else "无缓存"
        lines = [f"主目录 {self._data_version or '未知版本'} · {len(self._items)} 条 · 下载 {timestamp(self._loaded_at)}"]
        if self._primary_failed_at:
            lines.append("主目录最近刷新失败，继续使用可用数据")
        snapshots = self._dionatha_snapshots
        lines.append(f"补充目录 {len(snapshots)}/7 集合 · {len(self._dionatha_items)} 条 · 合并后 {len(self._merged_items)} 条")
        now = time.time()
        for collection in DIONATHA_COLLECTIONS:
            snapshot = snapshots.get(collection)
            if snapshot is None:
                lines.append(f"  {collection}: 无可用缓存")
                continue
            state = "刷新失败/保留旧数据" if collection in self._dionatha_failures else (
                "旧缓存" if now - snapshot["updated_at"] >= REFRESH_INTERVAL else "可用")
            lines.append(f"  {collection}: {snapshot['version']} · {len(snapshot['rows'])} 条 · {timestamp(snapshot['updated_at'])} · {state}")
        lines.append(f"敌人 {len(self._enemies)} 种" + (" · 刷新失败/保留旧数据" if self._enemies_failed_at else ""))
        conflicts = sum(bool(item.get("_conflicts")) for item in self._merged_items)
        if conflicts:
            lines.append(f"跨源字段差异 {conflicts} 项（含基础/升级口径差异）；卡片显示适用条件")
        return lines

    def _build_index(self) -> None:
        index: dict[str, list[dict[str, Any]]] = {}
        merged = merge_equipment(self._items, self._dionatha_items, getattr(self, "_data_version", ""))

        def put(key: str, item: dict[str, Any]) -> None:
            key = (key or "").strip().casefold()
            for candidate in dict.fromkeys((key, normalize_query(key))):
                if candidate:
                    bucket = index.setdefault(candidate, [])
                    if all(existing is not item for existing in bucket):
                        bucket.append(item)

        for item in [*merged, *getattr(self, "_enemies", [])]:
            kind = PRODUCT_KIND_ZH.get(str(item.get("productKind")), "")
            names = [str(item.get(field) or "") for field in ("nameZh", "nameEn", "model", "id", "slug")]
            names += [str(name) for name in item.get("alternateNames") or []]
            names += self._aliases.get(str(item.get("id") or ""), [])
            names += item.get("_search_ids") or []
            for name in names:
                put(name, item)
                if name and kind:
                    put(f"{name} {kind}", item)
                    put(f"{kind} {name}", item)
            if item.get("_collection") and item.get("slug"):
                put(f"dionatha:{item['_collection']}:{item['slug']}", item)
        for item in merged:
            if item.get("_collection") and item.get("slug"):
                selector = f"dionatha:{item['_collection']}:{item['slug']}"
            else:
                selector = f"hd2tool:{item.get('productKind', '')}:{item['id']}"
            put(selector, item)
            name = str(item.get("nameZh") or item.get("nameEn") or "?")
            kind = PRODUCT_KIND_ZH.get(str(item.get("productKind")), "装备")
            hint = f"{name} {kind}"
            matches = index.get(hint.casefold()) or index.get(normalize_query(hint)) or []
            item["_query_hint"] = hint if len(matches) == 1 and matches[0] is item else selector
        self._merged_items = merged
        self._index = index

    def aliases_for(self, item_id: str) -> list[str]:
        return self._aliases.get(str(item_id or ""), [])

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Exact names can return several categories; never hide helmet/armor twins."""
        q = str(query or "").strip().casefold()
        if not q:
            return []
        qn = normalize_query(q)
        exact = self._index.get(q) or self._index.get(qn)
        if exact:
            return exact[:limit]
        match = re.match(r"^([a-z]+[0-9]+)", qn)
        prefix = match[1] if match and len(match[1]) >= 3 else ""
        found: list[dict[str, Any]] = []
        seen: set[int] = set()
        for partial in (False, True):
            for key, bucket in self._index.items():
                matches = (q in key or (qn and qn in key) or (len(prefix) >= 4 and prefix in key)) if partial else (
                    key.startswith(q) or (qn and key.startswith(qn)) or (prefix and key.startswith(prefix)))
                if matches:
                    for item in bucket:
                        if id(item) not in seen:
                            seen.add(id(item))
                            found.append(item)
                if len(found) >= limit:
                    return found[:limit]
        if not found and len(q) >= 4 and re.search(r"[\u4e00-\u9fff]", q):
            for key, bucket in self._index.items():
                if abs(len(key) - len(q)) <= 1 and _edit_distance_le1(q, key):
                    for item in bucket:
                        if id(item) not in seen:
                            seen.add(id(item))
                            found.append(item)
                if len(found) >= limit:
                    break
        return found[:limit]


__all__ = [
    "ArsenalClient",
    "ACQUISITION_ZH",
    "ARMOR_TIER_ZH",
    "COMPONENT_LABEL_ZH",
    "FIELD_ZH",
    "HANDLING_ZH",
    "PRODUCT_KIND_ZH",
    "armor_tier_label",
    "enemy_name_zh",
    "normalize_query",
    "parse_anatomy_armor",
    "parse_damage",
    "parse_health",
]
