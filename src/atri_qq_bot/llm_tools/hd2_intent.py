"""Helldivers 自然语言工具的意图归一与斜杠重建。

本模块只负责把 LLM 工具调用的结构化参数（command + argument）校验、归一，
并重建出一条标准斜杠命令文本，再交给已有的 ``HelldiversBridge.handle``
解析执行。这样自然语言链路与斜杠链路复用同一套命令分发、别名和参数校验，
不会出现两套实现漂移。

设计要点：
- ``HD2_CANONICAL_COMMANDS`` 每个功能只暴露一个"规范命令词"，避免模型在
  一堆别名里乱选；这些词都能在 bridge 的 ``_COMMANDS`` 中直接命中。
- 只有需要实体的 4 个功能（战线/星球/wiki/军需簿）允许带 argument，
  其余功能的 argument 必须为空。
"""

from __future__ import annotations

import re

from ..helldivers.core.glossary import PLANET_NAMES, normalize_planet_name
from ..helldivers.core.war_state import FACTION_ALIASES

# 22 个功能各自的规范命令词。顺序与 help_text 的分组大致对应，便于人工核对。
# 每个字符串都必须是 HelldiversBridge._COMMANDS 中真实存在的 key。
HD2_CANONICAL_COMMANDS: tuple[str, ...] = (
    # 帮助 / 系统
    "hd2",
    "hd2help",
    "hd2ping",
    # 战况图片卡片
    "dashboard",
    "hd2stats",
    "order",
    "map",
    "global_events",
    "warfront",
    "planet",
    "dss",
    "steam",
    # 文本 / 外部资讯
    "wiki",
    "hd2data",
    # 网页截图 / 快报
    "hd2news",
    "companion",
    "hd2refresh",
    "银河快报",
    # 订阅 / 退订
    "hd2news订阅",
    "hd2news退订",
    "银河快报订阅",
    "银河快报退订",
)

# 需要从自然语句里抽取实体的功能：参数缺失时无法执行，必须显式报错让模型补全。
# 对应 bridge 中读取 args 的命令：战线（阵营）、星球（名称/编号/链接）、
# wiki（关键词）、军需簿（武器/护甲/敌人名称）。
HD2_ARGUMENT_COMMANDS: frozenset[str] = frozenset(
    {"warfront", "planet", "wiki", "hd2data"}
)

# argument 允许的最大长度，防止模型把整段废话塞进实体里。
_MAX_ARGUMENT_LENGTH = 200

# 结果新鲜度：auto 允许复用几分钟内的同一条结果（用户"再看一眼"也发得出来），
# latest 强制重新出图（数据侧尽量取最新）。
HD2_FRESHNESS_VALUES: tuple[str, ...] = ("auto", "latest")


class HD2IntentError(ValueError):
    """自然语言工具参数无效时抛出，便于上层统一转成工具回执。"""


def normalize_hd2_arguments(arguments: dict[str, object]) -> tuple[str, str]:
    """把工具入参校验并归一为 ``(command, argument)``。

    - command 缺失、不是字符串或不在规范清单内 -> HD2IntentError；
    - 规范命令词大小写不敏感（统一转小写），中文命令词原样保留；
    - argument 统一 strip；需要实体的功能不允许为空，其余功能忽略多余参数。
    """
    if not isinstance(arguments, dict):
        raise HD2IntentError("参数必须是 JSON 对象")

    raw_command = arguments.get("command")
    if not isinstance(raw_command, str) or not raw_command.strip():
        raise HD2IntentError("缺少 command 字段")
    command = raw_command.strip().lower()
    if command not in HD2_CANONICAL_COMMANDS:
        raise HD2IntentError(f"不支持的命令：{command}")

    raw_argument = arguments.get("argument", "")
    argument = raw_argument.strip() if isinstance(raw_argument, str) else ""
    if len(argument) > _MAX_ARGUMENT_LENGTH:
        raise HD2IntentError("argument 过长，请只保留关键名称")

    if command in HD2_ARGUMENT_COMMANDS:
        if not argument:
            raise HD2IntentError(f"/{command} 需要一个名称作为参数")
    else:
        # 无参功能不接受实体，避免模型把闲聊内容误拼进命令。
        argument = ""

    return command, argument


def normalize_hd2_freshness(arguments: dict[str, object]) -> str:
    """读取结果新鲜度参数，缺省或非法值一律回退 ``auto``。

    独立于 ``normalize_hd2_arguments``：freshness 不是命令实体，缺失不能算错，
    更不能因为模型多塞了一个字段就让整条情报查询失败。
    """
    raw = arguments.get("freshness") if isinstance(arguments, dict) else None
    if not isinstance(raw, str):
        return "auto"
    value = raw.strip().lower()
    return value if value in HD2_FRESHNESS_VALUES else "auto"


def normalize_hd2_entity(command: str, argument: str) -> str:
    """把同一实体的不同写法归一到同一个键（缓存复用与“是不是同一张图”都靠它）。

    有规范实体的两个命令才做映射，且直接复用 bridge 自己的表，避免两份别名
    各自漂移（``warfront`` 用 ``FACTION_ALIASES``，``planet`` 用官方中文译名
    反查）；``wiki`` / ``hd2data`` 是模糊关键词检索、没有唯一实体，只做空白与
    大小写归一；其余命令的 argument 已被丢弃，走不到这里。
    """
    text = _compact_entity(argument)
    command = str(command or "")
    if command == "warfront":
        return _HD2_FACTION_KEYS.get(text, text)
    if command == "planet":
        # 星球另走官方归一：``erata_prime`` / ``Erata Prime`` / ``艾拉特主星`` /
        # Companion 链接同键。
        return _planet_entity_key(argument, text)
    return text


_PLANET_SLUG_RE = re.compile(r"planets/([^/?#]+)", re.IGNORECASE)
_PLANET_INDEX_SUFFIX_RE = re.compile(r"[_\s]*\(\s*\d+\s*\)$")
_PLANET_INDEX_ONLY_RE = re.compile(
    r"^(?:行星|星球|planet|#)?\s*(\d{1,6})(?:\s*号)?$", re.IGNORECASE
)


def _planet_entity_key(argument: str, fallback: str) -> str:
    """星球实体的归一键：先剥掉 Companion 链接与编号后缀，再查官方译名表。"""
    text = str(argument or "")
    match = _PLANET_SLUG_RE.search(text)
    if match:
        text = _PLANET_INDEX_SUFFIX_RE.sub("", match.group(1))
    key = _compact_entity(normalize_planet_name(text))
    index = _PLANET_INDEX_ONLY_RE.match(key)
    if index:
        # “行星283” / “283号” / “#283” 都是同一个编号查询。
        return index.group(1)
    return _HD2_PLANET_KEYS.get(key, fallback)


def _compact_entity(value: object) -> str:
    """去掉所有空白并统一大小写：``"Erata Prime"`` 与 ``"erata_prime"`` 同键。"""
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _build_faction_keys() -> dict[str, str]:
    """把 bridge 的阵营别名表压成 ``去空白小写写法 -> 规范阵营``。"""
    keys = {
        _compact_entity(alias): str(canonical).casefold()
        for alias, canonical in FACTION_ALIASES.items()
    }
    for standard in ("Terminids", "Automaton", "Illuminate", "Humans"):
        keys.setdefault(_compact_entity(standard), standard.casefold())
    return keys


def _build_planet_keys() -> dict[str, str]:
    """用官方中文译名表做双向归一键：中文名、英文名、``- Void`` 变体同一个键。

    中文译名不唯一时（同名星球/未启用副本，如 ``哈卓毕亚斯``）直接不映射，
    宁可少复用一次，也不能把两颗不同的星球并成一个键。
    """
    keys: dict[str, str] = {}
    conflicts: set[str] = set()
    for english, chinese in PLANET_NAMES.items():
        canonical = _compact_entity(normalize_planet_name(english))
        keys[canonical] = canonical
        chinese_key = _compact_entity(chinese)
        previous = keys.get(chinese_key)
        if previous is None:
            keys[chinese_key] = canonical
        elif previous != canonical:
            conflicts.add(chinese_key)
    for key in conflicts:
        keys.pop(key, None)
    return keys


_HD2_FACTION_KEYS: dict[str, str] = _build_faction_keys()
_HD2_PLANET_KEYS: dict[str, str] = _build_planet_keys()


def has_explicit_hd2_freshness(arguments: dict[str, object]) -> bool:
    """模型是否显式给了 freshness（哪怕给的是 auto）。

    显式表态优先：模型自己判断“这一轮该复用”时，不再用关键词改它的主意；
    只有模型根本没提这个字段，才由用户原话兜底。
    """
    if not isinstance(arguments, dict):
        return False
    raw = arguments.get("freshness")
    return isinstance(raw, str) and bool(raw.strip())


def build_hd2_slash(command: str, argument: str = "") -> str:
    """根据规范命令词与实体重建一条斜杠命令文本。

    例如 ("planet", "富源") -> "/planet 富源"；无参命令 -> "/dashboard"。
    这里只做字符串拼接，真正的合法性由 normalize_hd2_arguments 保证。
    """
    if argument:
        return f"/{command} {argument}"
    return f"/{command}"


# 成功发卡后，最终文本若仍声称查不到/做不到，即与已就绪的卡片自相矛盾。
# 仅在本轮确实有成功的 HD2 卡片时才据此判定，避免误伤普通闲聊。
_HD2_INCAPABILITY_MARKERS: tuple[str, ...] = (
    "查不到", "查不了", "找不到", "做不到", "没法", "无法",
    "连不上", "没连", "未连接", "没连着", "不能查", "手头又没",
)


def has_hd2_capability_contradiction(text: str) -> bool:
    """成功卡片已就绪时，文本却声称无法查询，返回 True。"""
    normalized = re.sub(r"\s+", "", str(text or ""))
    if not normalized:
        return False
    return any(marker in normalized for marker in _HD2_INCAPABILITY_MARKERS)


# 自然语句里的强命令词 -> 规范命令。先长后短，确保更具体的词优先命中。
# 用于在允许的轮次注入确定性提示，提高模型对缩写/别名的召回；提示不强制
# 调用，模型仍结合上下文判断，避免普通聊天被误触发。
_HD2_KEYWORD_HINTS: tuple[tuple[str, str], ...] = (
    ("民主空间站", "dss"),
    ("空间站", "dss"),
    ("dss", "dss"),
    ("战况总览", "dashboard"),
    ("总览", "dashboard"),
    ("dashboard", "dashboard"),
    ("最高命令", "order"),
    ("major_order", "order"),
    ("order", "order"),
    ("全球事件", "global_events"),
    ("global_events", "global_events"),
    ("军需簿", "hd2data"),
    ("武器库", "hd2data"),
    ("数据库", "hd2data"),
    ("hd2data", "hd2data"),
    ("银河快报", "银河快报"),
    ("galaxy_news", "银河快报"),
    ("战线", "warfront"),
    ("warfront", "warfront"),
    ("维基", "wiki"),
    ("百科", "wiki"),
    ("wiki", "wiki"),
    ("地图", "map"),
    ("map", "map"),
    ("星球", "planet"),
    ("planet", "planet"),
    ("steam", "steam"),
    ("companion", "companion"),
    ("刷新缓存", "hd2refresh"),
    ("hd2refresh", "hd2refresh"),
    ("自检", "hd2ping"),
    ("诊断", "hd2ping"),
    ("hd2ping", "hd2ping"),
    ("hd2stats", "hd2stats"),
)

_ASCII_TOKEN_RE = re.compile(r"^[a-z0-9_]+$")

# 用户明确要“新的一张 / 最新数据”时，不能拿几分钟前那份复用结果糊弄。
# 命中即按 latest 处理（多抓一次也认），模型没传 freshness 也不影响用户诉求。
_HD2_REFRESH_HINTS: tuple[str, ...] = (
    "刷新",
    "最新",
    "新的",
    "新图",
    "新的一张",
    "重新查",
    "重查",
    "再查一次",
    "实时",
    "现在多少",
    "现在怎么样",
    "更新一下",
    "更新下",
    "换一张",
    "换张",
    "来张新",
    "来一张新",
    "重做一张",
)

# 用户明确要“再看一眼 / 再来一张”时，图必须发得出来（只是不必重新抓）。
# 用于区分“对着图吐槽”和“想再看图”：后者不能因为引用而把图吞掉。
_HD2_REDISPLAY_HINTS: tuple[str, ...] = (
    "再看",
    "再来一张",
    "再发一张",
    "再发一遍",
    "再发一次",
    "发一张",
    "发张",
    "看一眼",
    "翻出来",
    "调出来",
    "重新发",
)


def _normalize_hd2_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def detect_hd2_refresh_hint(text: str) -> bool:
    """用户这句话是否明确要求“新的一张 / 最新数据”。"""
    normalized = _normalize_hd2_text(text)
    if not normalized:
        return False
    return any(hint in normalized for hint in _HD2_REFRESH_HINTS)


def detect_hd2_redisplay_hint(text: str) -> bool:
    """用户这句话是否明确要求“再看一眼 / 再来一张图”。"""
    normalized = _normalize_hd2_text(text)
    if not normalized:
        return False
    return any(hint in normalized for hint in _HD2_REDISPLAY_HINTS)


def detect_hd2_command_hint(text: str) -> str | None:
    """从自然语句里识别强命令词，返回规范命令，无可靠命中返回 None。

    ASCII 词按词边界匹配（避免 dss 命中包含它的更长单词），中文按子串匹配；
    关键词表已按先长后短排列，命中第一个即返回。
    """
    normalized = str(text or "").lower()
    if not normalized.strip():
        return None
    for keyword, command in _HD2_KEYWORD_HINTS:
        if _ASCII_TOKEN_RE.match(keyword):
            if re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", normalized):
                return command
        elif keyword in normalized:
            return command
    return None
