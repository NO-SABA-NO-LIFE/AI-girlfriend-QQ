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
