from __future__ import annotations

import re

# 强标点：句子边界。
_STRONG_PUNCT = "。！？!?；;\n"

# 单个 Unicode 表情字符（含表情、象形、交通、国旗、杂项符号等）。
_EMOJI_CHAR = (
    r"[\U0001F1E6-\U0001F1FF"
    r"\U0001F300-\U0001FAFF"
    r"\u2600-\u27BF"
    r"\u2B00-\u2BFF"
    r"\u24C2\u2139\u2300-\u23FF\u25A0-\u25FF]"
)
# 完整表情序列：支持 ZWJ 连接、肤色修饰和变体选择符（如 👨‍👩‍👧、❤️）。
_EMOJI_RE = re.compile(
    rf"{_EMOJI_CHAR}(?:[\u200D\ufe0f]+{_EMOJI_CHAR})*[\ufe0f]?"
)

# 受保护的不可拆分整体：网址和邮箱，内部再长也不能截断。
_EMAIL_PATTERN = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
_URL_PATTERN = r"https?://[^\s，。！？；、]+"
_PROTECTED_RE = re.compile(
    rf"(?P<emoji>{_EMOJI_RE.pattern})"
    rf"|(?P<url>{_URL_PATTERN})"
    rf"|(?P<email>{_EMAIL_PATTERN})"
)


def split_reply_text(text: str, max_chars: int = 120, max_parts: int = 2) -> list[str]:
    text = _compact_reply_text(text)
    if not text:
        return []

    # 1. 切成带标签的原子：emoji / 受保护整体 / 普通文本。
    atoms = _build_atoms(text, max_chars)
    # 2. 贪心打包：emoji 始终附着在文字上，受保护整体不截断，尽量不产生孤立气泡。
    segments = _pack_atoms(atoms, max_chars)
    # 3. 限制段数：超出时把剩余内容并入最后一段，保持完整。
    if len(segments) <= max_parts:
        return segments
    kept = segments[: max_parts - 1]
    kept.append("".join(segments[max_parts - 1 :]))
    return kept


def _compact_reply_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text.strip())
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _build_atoms(text: str, max_chars: int) -> list[tuple[str, str]]:
    """把文本切成 (内容, 标签) 原子；emoji 与受保护整体保持完整。"""
    atoms: list[tuple[str, str]] = []
    pos = 0
    for match in _PROTECTED_RE.finditer(text):
        if match.start() > pos:
            atoms.extend(_split_plain(text[pos : match.start()], max_chars))
        if match.group("emoji"):
            atoms.append((match.group("emoji"), "emoji"))
        else:
            atoms.append((match.group(0), "protected"))
        pos = match.end()
    if pos < len(text):
        atoms.extend(_split_plain(text[pos:], max_chars))
    return atoms


def _split_plain(plain: str, max_chars: int) -> list[tuple[str, str]]:
    """普通文本（不含 emoji/受保护整体）先按强标点切句，长句再在软标记处换行。"""
    chunks = re.findall(rf"[^{_STRONG_PUNCT}]+[{_STRONG_PUNCT}]?", plain)
    result: list[tuple[str, str]] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if len(chunk) <= max_chars:
            result.append((chunk, "normal"))
        else:
            for part in _soft_wrap_plain(chunk, max_chars):
                result.append((part, "normal"))
    if result:
        return result
    stripped = plain.strip()
    return [(stripped, "normal")] if stripped else []


def _soft_wrap_plain(text: str, max_chars: int) -> list[str]:
    """普通长文本在软标记（逗号、顿号、空格）处换行，避免生硬硬切。"""
    parts: list[str] = []
    remaining = text
    soft_marks = "，,、 "
    while len(remaining) > max_chars:
        split_at = max(remaining.rfind(mark, 0, max_chars + 1) for mark in soft_marks)
        if split_at < max_chars // 2:
            split_at = max_chars
        part = remaining[: split_at + 1].strip()
        remaining = remaining[split_at + 1 :].strip()
        if part:
            parts.append(part)
    if remaining:
        parts.append(remaining)
    return parts


def _is_emoji_only(text: str) -> bool:
    return bool(text) and all(
        _EMOJI_RE.fullmatch(piece) for piece in text if piece.strip()
    )


def _pack_atoms(
    atoms: list[tuple[str, str]], max_chars: int
) -> list[str]:
    """贪心打包。emoji 附着在相邻文字上，受保护整体不截断，普通块按字数换行。"""
    segments: list[str] = []
    current = ""
    for atom, kind in atoms:
        if not current:
            current = atom
            continue
        if kind == "emoji":
            # 句尾 emoji：哪怕略微超字数也并到前文，绝不单独成气泡。
            current = f"{current}{atom}"
        elif _is_emoji_only(current):
            # 句首 emoji：把后续文字并进来，避免 emoji 孤立。
            current = f"{current}{atom}"
        elif len(current) + len(atom) <= max_chars:
            current = f"{current}{atom}"
        else:
            segments.append(current.strip())
            current = atom
    if current:
        segments.append(current.strip())
    return segments


def _ends_with_strong_punctuation(text: str) -> bool:
    return text.endswith(("。", "！", "？", "!", "?", "；", ";"))


def _truncate_tail(text: str, limit: int) -> str:
    """Backward-compatible helper that deliberately keeps complete content."""
    return text

# End of file.
