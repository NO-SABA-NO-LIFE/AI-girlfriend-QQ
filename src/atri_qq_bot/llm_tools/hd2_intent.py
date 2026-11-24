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
