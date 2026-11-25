from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Markers that negate a keyword within a short window before it, e.g.
# "可别喊救命" must not count the "救命" evidence.
NEGATION_MARKERS = ("不", "没", "别", "无", "莫")
NEGATION_WINDOW = 3
# Keywords shorter than two characters (punctuation, single digits/hanzi) are
# weak evidence: they can support a strong match but never decide an emotion.
STRONG_KEYWORD_MIN_LEN = 2


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
CONTENT_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
KNOWN_EMOTIONS = {
    "happy",
    "comfort",
    "sad",
    "angry",
    "tired",
    "proud",
    "confused",
    "speechless",
    "surprised",
    "awkward",
    "teasing",
    "shy",
    "affection",
    "miss",
    "care",
    "thanks",
    "sorry",
    "encourage",
    "thinking",
    "celebrate",
    "food",
    "goodnight",
    "pout",
    "neutral",
    "unsorted",
}
EMOTION_PRIORITY = {
    "sad": 102,
    "comfort": 100,
    "angry": 99,
    "goodnight": 95,
    "tired": 90,
    "pout": 82,
    "shy": 78,
    "affection": 76,
    "miss": 74,
    "care": 72,
    "confused": 70,
    "speechless": 69,
    "surprised": 68,
    "teasing": 67,
    "awkward": 66,
    "food": 64,
    "thinking": 62,
    "encourage": 60,
    "proud": 58,
    "thanks": 56,
    "sorry": 54,
    "celebrate": 52,
    "happy": 50,
    "neutral": 0,
    "unsorted": -1,
}
DEFAULT_EMOTION_KEYWORDS = {
    "happy": ("开心", "好耶", "哈哈", "嘿嘿", "笑死", "喜欢", "成功", "太好了", "棒", "厉害"),
    "comfort": ("难受", "难过", "烦", "焦虑", "压力", "崩溃", "委屈", "哭", "emo", "不开心", "心累", "破防"),
    "sad": ("想哭", "低落", "失落", "心碎", "emo了", "没人懂", "孤独", "空落落"),
    "angry": ("生气", "气死", "火大", "恼火", "红温", "无语死", "受不了", "欺负我"),
    "tired": ("累", "困", "疲惫", "不想动", "睡觉", "熬夜", "撑不住"),
    "proud": ("高性能", "夸我", "厉害吧", "做到啦", "启动成功", "亚托莉", "主人真厉害"),
    "confused": ("怎么", "为什么", "不懂", "错了", "答非所问", "？", "?", "抽象", "逆天", "离谱", "6"),
    "speechless": ("无语", "嫌弃", "不想理", "沉默", "呆住", "呆滞", "冷漠", "麻了", "没眼看"),
    "surprised": ("真的假的", "震惊", "惊了", "啊这", "不会吧", "这么突然"),
    "awkward": ("尴尬", "社死", "汗流浃背", "绷不住了", "难绷", "救命"),
    "teasing": ("调侃", "逗你", "阴阳怪气", "我超怕的", "怕了吧", "就这", "略略", "才不是"),
    "shy": ("喜欢你", "想你", "爱你", "抱抱", "亲亲", "涩涩", "不准涩涩", "给我忘掉"),
    "affection": ("贴贴", "抱一下", "抱住", "亲密", "撒娇", "陪陪我"),
    "miss": ("想我了吗", "有没有想我", "想你了", "突然想你", "等你", "冷落我"),
    "care": ("喝水", "吃药", "注意身体", "别硬撑", "休息一下", "照顾好自己"),
    "thanks": ("谢谢", "感谢", "辛苦你了", "帮大忙", "有你真好"),
    "sorry": ("对不起", "抱歉", "我错了", "不是故意", "原谅我"),
    "encourage": ("加油", "冲", "努力", "开始做", "坚持", "打起精神", "可以做到"),
    "thinking": ("纠结", "考虑", "想想", "该不该", "要不要", "选哪个", "怎么选"),
    "celebrate": ("庆祝", "赢了", "完成了", "搞定", "通过了", "顺利", "开香槟"),
    "food": ("吃什么", "饿", "吃饭", "早餐", "午饭", "晚饭", "夜宵"),
    "goodnight": ("晚安", "睡了", "睡觉", "困了"),
    "pout": ("冷落", "忘了我", "终于想起我", "萝卜子", "哼哒"),
}
DEFAULT_EMOJIS = {
    # 除 pout 外一律不发文字短语；空元组让 _pick_emoji 返回 None。
    "happy": (),
    "comfort": (),
    "sad": (),
    "angry": (),
    "tired": (),
    "proud": (),
    "confused": (),
    "speechless": (),
    "surprised": (),
    "awkward": (),
    "teasing": (),
    "shy": (),
    "affection": (),
    "miss": (),
    "care": (),
    "thanks": (),
    "sorry": (),
    "encourage": (),
    "thinking": (),
    "celebrate": (),
    "food": (),
    "goodnight": (),
    "pout": ("哼哒",),
    "neutral": (),
}
DEFAULT_QQ_FACE_IDS = {
    # 每个情绪只挂真正贴合该情绪的系统脸；一个脸可被多个近义情绪复用。
    # 空元组表示该情绪不发系统脸，由 choose() 降级到文字短语。
    "happy": ("13", "28", "21"),
    "comfort": ("49",),
    "tired": ("8",),
    "proud": ("28", "13"),
    "confused": ("32",),
    "speechless": ("1", "97", "14"),
    "shy": (),
    "food": (),
    "goodnight": ("75", "8"),
    "pout": (),
    "sad": ("5", "9"),
    "angry": ("11",),
    "surprised": ("0",),
    "awkward": ("10",),
    "teasing": ("178", "13"),
    "affection": ("21",),
    "miss": (),
    "care": ("49",),
    "thanks": ("76",),
    "sorry": ("9",),
    "encourage": ("76", "144"),
    "thinking": ("32",),
    "celebrate": ("144", "76", "13"),
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]

def _image_files_in_dir(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return [
        item
        for item in path.iterdir()
        if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
    ]

def _normalize_emotion(emotion: str) -> str:
    emotion = str(emotion or "neutral").strip().lower()
    aliases = {
        "sad": "sad",
        "cry": "sad",
        "angry": "angry",
        "sleepy": "tired",
        "night": "goodnight",
        "question": "confused",
        "unamused": "speechless",
        "speechless": "speechless",
        "tease": "teasing",
        "teasing": "teasing",
        "love": "shy",
        "loving": "affection",
        "missing": "miss",
        "encouragement": "encourage",
        "think": "thinking",
        "celebration": "celebrate",
        "web": "web",
    }
    normalized = aliases.get(emotion, emotion)
    if normalized == "web":
        return normalized
    return normalized if normalized in KNOWN_EMOTIONS else "neutral"

def _is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - NEGATION_WINDOW):start]
    return any(marker in prefix for marker in NEGATION_MARKERS)


def collect_emotion_evidence(
    text: str,
    emotion_keywords: dict[str, tuple[str, ...]],
) -> dict[str, dict[str, int]]:
    """Count strong/weak keyword hits per emotion in one piece of text.

    Returns ``{emotion: {"strong": int, "weak": int}}``. Negated hits are
    skipped and weak keywords are recorded separately so callers can refuse to
    let them decide an emotion on their own.
    """
    evidence: dict[str, dict[str, int]] = {}
    if not text:
        return evidence

    for emotion, keywords in emotion_keywords.items():
        normalized = _normalize_emotion(emotion)
        if normalized in {"neutral", "unsorted", "web"}:
            continue
        bucket = evidence.setdefault(normalized, {"strong": 0, "weak": 0})
        for keyword in keywords:
            keyword = str(keyword).strip()
            if not keyword:
                continue
            for match in re.finditer(re.escape(keyword), text):
                if _is_negated(text, match.start()):
                    continue
                kind = "strong" if len(keyword) >= STRONG_KEYWORD_MIN_LEN else "weak"
                bucket[kind] += 1
    return evidence


def _score_emotion(
    text: str,
    emotion_keywords: dict[str, tuple[str, ...]],
    weight: int,
) -> dict[str, int]:
    """Backward-compatible weighted score (strong + weak hits)."""
    evidence = collect_emotion_evidence(text, emotion_keywords)
    return {
        emotion: (hits["strong"] + hits["weak"]) * weight
        for emotion, hits in evidence.items()
    }

def _looks_like_image(value: str) -> bool:
    return Path(value).suffix.lower() in IMAGE_EXTENSIONS

def _looks_like_url(value: Any) -> bool:
    if not value:
        return False
    parsed = urlparse(str(value))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

def _in_cooldown(profile: dict[str, Any] | None, cooldown_seconds: int) -> bool:
    if cooldown_seconds <= 0 or not profile:
        return False
    last_sticker_at = profile.get("last_sticker_at")
    try:
        if last_sticker_at is None:
            return False
        return time.time() - float(last_sticker_at) < cooldown_seconds
    except (TypeError, ValueError):
        return False

def _first_url(data: dict[str, Any]) -> str | None:
    for key in ("url", "file_url", "preview", "origin_url"):
        value = data.get(key)
        if _looks_like_url(value):
            return str(value)
    return None

def _suffix_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    return ".jpg"

def _write_capture_metadata(path: Path, url: str, emotion: str, content_type: str) -> None:
    metadata = {
        "source": "chat_history",
        "url": url,
        "emotion": emotion,
        "content_type": content_type,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    path.with_suffix(path.suffix + ".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
