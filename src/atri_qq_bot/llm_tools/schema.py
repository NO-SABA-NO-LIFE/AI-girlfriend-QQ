from __future__ import annotations

from typing import Any

from ..prompting import load_prompt
from ..voice.policy import load_voice_behavior
from .agent_protocol import AGENT_ACTION_PROMPT, final_answer_schema
from .hd2_intent import HD2_CANONICAL_COMMANDS, HD2_FRESHNESS_VALUES


# 工具选择原则由外部 Markdown 维护，避免模型能力规则和代码实现重复。
_TOOL_USAGE_INSTRUCTION_PROMPT = load_prompt("tool_use")
AUTOMATIC_TOOL_INSTRUCTION_PROMPT = load_prompt("tool_use")
TOOL_INSTRUCTION_PROMPT = load_prompt("tool_use")

_LEGACY_AUTOMATIC_TOOL_INSTRUCTION_PROMPT = (
    "你可以在确有需要时自主调用工具；普通闲聊、情绪回应和已有上下文足够的问题直接回答，"
    "不要为了调用而调用工具。\n"
    + _TOOL_USAGE_INSTRUCTION_PROMPT
)
# 保留旧拼装变量仅供兼容导入；实际对外使用上面的外部文档内容。


def available_tool_schemas(
    config: Any,
    hd2_nl_allowed: bool = False,
) -> list[dict[str, Any]]:
    if not bool(getattr(config, "llm_tools_enabled", True)):
        return []

    tools = [_current_time_schema()]
    tools.append(_poke_user_schema())
    if bool(getattr(config, "web_search_enabled", True)):
        tools.append(_weather_schema())
        tools.append(_web_search_schema(config))
        tools.append(_web_page_schema())
    if bool(getattr(config, "voice_tts_enabled", False)):
        tools.append(_voice_schema(config))
        if load_voice_behavior().get("calls_enabled", False):
            tools.append(_voice_call_schema())
    # Helldivers 自然语言查询：仅在本轮满足触发门（私聊直接 / 群里被 @）且
    # 配置开关打开时才提供，避免普通群聊被误触发情报查询。
    if hd2_nl_allowed and bool(
        getattr(config, "hd2_natural_language_enabled", True)
    ):
        tools.append(_hd2_query_schema())
    if bool(getattr(config, "llm_agent_protocol_enabled", False)):
        tools.append(final_answer_schema(getattr(config, "max_tokens", 1200) * 4))
    return tools


def _hd2_query_schema() -> dict[str, Any]:
    """自然语言 Helldivers 情报查询工具。

    模型把用户的自然语言请求映射到一个规范命令词，并把需要查询的实体放进
    argument；执行结果（图片/文本卡片）会随本轮回复一起发出。
    """
    return {
        "type": "function",
        "function": {
            "name": "query_helldivers",
            "description": (
                "查询《绝地潜兵2 / Helldivers 2》的银河战争情报：战况总览、星球详情、"
                "阵营战线、战争地图、最高命令、全球事件、民主空间站、Steam 新闻、"
                "Wiki、军需簿以及新闻/快报订阅等。当用户明确想了解这类游戏情报时调用；"
                "普通闲聊或与该游戏无关的问题不要调用。"
                "用户只是想再看一次刚才那份情报、或引用旧情报图做评论时，可以不调用；"
                "确实调用时用默认 freshness=auto 即可，系统会直接复用刚发出的同一结果，不会重复抓取。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": list(HD2_CANONICAL_COMMANDS),
                        "description": "要执行的规范命令词，对应一个情报功能。",
                    },
                    "argument": {
                        "type": "string",
                        "description": (
                            "查询实体：warfront 填阵营，同一阵营的多种说法都等价——"
                            "终结族=虫族=虫子=东线=Terminids，"
                            "机器人=自动机=铁卫=西线=Automaton，"
                            "光能者=光能=鱿鱼=南线=Illuminate，超级地球=人类=Humans；"
                            "planet 填星球名称/编号/链接（中文官方译名也可以），"
                            "wiki 填关键词，hd2data 填"
                            "武器/护甲/敌人名称；其它命令留空字符串。"
                        ),
                    },
                    "freshness": {
                        "type": "string",
                        "enum": list(HD2_FRESHNESS_VALUES),
                        "description": (
                            "默认 auto：本会话几分钟内刚发过同一条命令时，直接把那份结果"
                            "再发一次（用户说“再看一眼”也走 auto）。用户明确说要新的 /"
                            "换一张 / 刷新到最新数据时用 latest，会重新抓取并出图。"
                            "拿不准就别传这个字段，系统会按用户的原话判断。"
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }


def _poke_user_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "poke_user",
            "description": (
                "在群里戳一下正在和你对话的人。当你被惹生气、想傲娇抗议、炸毛或俏皮反击时，"
                "可以自主选择戳一下；也可以完全不戳、只用文字回应。普通闲聊、正经事或没有情绪时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


def _current_time_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前本地时间、日期和星期。用户问现在、今天、明天、几点、星期几，或建议依赖当前时间时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "目标时区，默认 Asia/Shanghai。当前仅稳定支持 Asia/Shanghai、UTC 和 UTC±小时。",
                    }
                },
            },
        },
    }


def _web_search_schema(config: Any) -> dict[str, Any]:
    max_results = int(getattr(config, "web_search_max_results", 5) or 5)
    return {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "搜索一般网页和最新新闻。用于实时信息、用户明确要求搜索，或核验视觉分析中不确定的人物、作品、梗图出处。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。要具体，不要只传“最新消息”。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"返回结果数量，默认不超过 {max_results} 条。",
                        "minimum": 1,
                        "maximum": max(1, max_results),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "web", "news", "research"],
                        "description": "一般资料用 web，近期消息用 news，前沿或未解问题用 research。",
                    },
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 3,
                        "description": "可选的强相关网站域名，例如 github.com、arxiv.org 或项目官网；不要填写完整 URL。",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _web_page_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "open_web_page",
            "description": (
                "读取 search_web 返回的公开网页正文。用于核验具体事实、研究现状和原始来源；"
                "只打开搜索结果中确实出现的 URL。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "search_web 结果中的完整 http/https URL。",
                    }
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    }


def _weather_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市或地区的当前天气与未来三天预报。天气、温度、降雨、体感温度问题优先使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "明确的城市或地区名称，例如“湖北十堰”或“东京”。不要猜测用户所在地。",
                    }
                },
                "required": ["location"],
            },
        },
    }


def _voice_schema(config: Any) -> dict[str, Any]:
    max_chars = max(20, int(getattr(config, "voice_max_chars", 160) or 160))
    return {
        "type": "function",
        "function": {
            "name": "speak_as_atri",
            "description": "让亚托莉用角色语音说一小段话。仅在用户想听语音或语音明显比文字更合适时调用。点歌由独立的固定歌单会话处理，不通过此工具触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            f"要说出的完整口语文本，不含网址或舞台说明，最多 {max_chars} 字。"
                            "必须直接书写目标语言：中文用汉字，日语用假名/汉字，英文用英文；"
                            "禁止罗马音、拼音、语言标签和翻译说明。"
                        ),
                        "maxLength": max_chars,
                    },
                    "emotion": {
                        "type": "string",
                        "description": (
                            "这句话实际要表达的语气。neutral=平静，gentle=温柔安慰，"
                            "happy=开心/得意/玩笑，shy=害羞/撒娇，sad=低落，"
                            "serious=认真提醒，sleepy=困倦/晚安，surprised=惊讶。"
                        ),
                        "enum": [
                            "neutral",
                            "gentle",
                            "happy",
                            "shy",
                            "sad",
                            "serious",
                            "sleepy",
                            "surprised",
                        ],
                    },
                    "intensity": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "情绪强度，普通自然口语建议 0.45-0.65，"
                            "明显情绪建议 0.65-0.8，避免无故填满。"
                        ),
                    },
                    "language": {
                        "type": "string",
                        "enum": ["auto", "zh", "ja", "en"],
                    },
                    "reason": {
                        "type": "string",
                        "enum": [
                            "explicit_request",
                            "voice_reply",
                            "autonomous",
                            "proactive",
                        ],
                        "description": "本次发语音的真实触发原因。",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["speech"],
                        "description": "普通语音固定使用 speech。",
                    },
                },
                "required": ["text", "reason"],
            },
        },
    }


def _voice_call_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "offer_voice_call",
            "description": "向当前私聊用户发起浏览器实时语音通话邀请。仅在用户明确要求或亲密且适合连续交谈时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "这次想聊的简短主题，可留空。",
                        "maxLength": 120,
                    }
                },
            },
        },
    }
