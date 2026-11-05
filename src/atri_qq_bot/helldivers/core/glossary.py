"""官方中文译名词表：敌人 / 星球 / 星系 / 战备。

词表数据存放在 ``assets/glossary/*.json``，均为扁平的 ``{英文casefold: 中文}``
映射。本模块负责加载、查询，并为翻译流程提供最长匹配优先的术语预替换。

归一化约定与 :mod:`i18n` 的 ``_token`` 一致：``strip().casefold()``。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Final

_GLOSSARY_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "assets" / "glossary"


def _load_json(name: str) -> dict[str, str]:
    """Load a glossary table, returning an empty dict on any failure."""

    path = _GLOSSARY_DIR / f"{name}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k).strip().casefold(): str(v).strip()
        for k, v in raw.items()
        if isinstance(v, str) and v.strip()
    }


def _token(value: object) -> str:
    return str(value).strip().casefold() if value is not None else ""


# ---- four canonical tables (loaded once at import) ----

ENEMY_NAMES: Final[dict[str, str]] = _load_json("enemies")
PLANET_NAMES: Final[dict[str, str]] = _load_json("planets")
SECTOR_NAMES: Final[dict[str, str]] = _load_json("sectors")
STRATAGEM_NAMES: Final[dict[str, str]] = _load_json("stratagems")

_TABLES: Final[dict[str, dict[str, str]]] = {
    "enemy": ENEMY_NAMES,
    "planet": PLANET_NAMES,
    "sector": SECTOR_NAMES,
    "stratagems": STRATAGEM_NAMES,
}


def lookup_term(category: str, value: object) -> str | None:
    """Return the official Chinese name for ``value`` in a category, or ``None``."""

    table = _TABLES.get(category)
    if table is None:
        return None
    return table.get(_token(value))


def all_terms() -> dict[str, str]:
    """Merge all four tables into one ``{en_lower: zh}`` mapping.

    On key collisions (a name appearing in multiple categories with different
    translations) the planet table wins, then enemy, sector, stratagem — planets
    are the most frequent and most important to get right in war text.
    """

    merged: dict[str, str] = {}
    # Apply in reverse precedence so planets win.
    for category in ("stratagems", "sector", "enemy", "planet"):
        merged.update(_TABLES[category])
    return merged


def _short_stratagem_names() -> dict[str, str]:
    """Map common (model-stripped) stratagem names, e.g. ``autocannon`` -> ``机炮``.

    The official table stores full designations like ``ac-8 autocannon``; war
    text often drops the model prefix. We strip a leading ``XX-`` / ``AX/`` style
    designator so the bare name also resolves.
    """

    short: dict[str, str] = {}
    # Leading designator patterns, applied in order of specificity:
    #   "a/ac-8 autocannon sentry" -> "autocannon sentry"
    #   "ac-8 autocannon"          -> "autocannon"
    #   "faf-14 spear"             -> "spear"
    #   "sh-20 ballistic shield"   -> "ballistic shield"
    #   "md-i4 incendiary mines"   -> "incendiary mines"
    #   "sta-x3 w.a.s.p. launcher" -> "w.a.s.p. launcher"
    designator = re.compile(
        r"^(?:[a-z]+/)?"      # optional vendor prefix like "a/" or "ax/"
        r"[a-z]+-[a-z0-9]+"   # letters-alnum model, e.g. "ac-8", "md-i4", "mls-4x", "sta-x3"
        r"(?:-[a-z0-9]+)?"    # optional suffix like "-xii" in "td-220"
        r"\s+"
    )
    for en, cn in STRATAGEM_NAMES.items():
        stripped = designator.sub("", en).strip()
        if stripped and stripped != en and stripped not in short:
            short[stripped] = cn
    return short


@lru_cache(maxsize=1)
def _merged_terms() -> dict[str, str]:
    """All canonical terms plus model-stripped stratagem aliases, built once."""

    terms = all_terms()
    terms.update(_short_stratagem_names())
    return terms


@lru_cache(maxsize=1)
def reverse_terms() -> dict[str, str]:
    """Inverted ``{中文: 英文}`` mapping for Chinese → English query translation.

    Built from :func:`_merged_terms` (so model-stripped stratagem aliases work in
    both directions). On collision the longest Chinese key wins, which is safe
    because the forward tables have no duplicate values.
    """

    reversed_map: dict[str, str] = {}
    for en, zh in _merged_terms().items():
        zh = zh.strip()
        if zh and zh not in reversed_map:
            reversed_map[zh] = en
    return reversed_map


def contains_cjk(text: str) -> bool:
    """True if ``text`` contains any CJK ideograph (used for wiki query routing)."""

    return bool(text and re.search(r"[\u4e00-\u9fff]", text))


def translate_query(query: str) -> str | None:
    """Translate a Chinese query to English wiki search terms via the glossary.

    Matching strategy (first hit wins):
    1. Exact ``中文 → 英文`` lookup (e.g. ``磁轨炮`` → ``rs-422 railgun``).
    2. Longest-first containment: extract every glossary Chinese term that
       appears inside the query (``轨道炮攻击怎么用`` contains ``轨道炮攻击``)
       and return the matched English terms joined by spaces.

    Returns ``None`` when nothing matched — caller keeps the original query.
    """

    query = (query or "").strip()
    if not query:
        return None
    table = reverse_terms()
    exact = table.get(query)
    if exact:
        return exact
    # Longest Chinese keys first so 轨道炮攻击 wins over shorter overlaps.
    matched: list[str] = []
    consumed = query
    for zh in sorted(table, key=len, reverse=True):
        if zh in consumed:
            matched.append(table[zh])
            consumed = consumed.replace(zh, " ")
    if matched:
        # Deduplicate while preserving order.
        seen: set[str] = set()
        ordered = [t for t in matched if not (t in seen or seen.add(t))]
        return " ".join(ordered)
    return None


@lru_cache(maxsize=1)
def _preapply_pattern() -> re.Pattern[str] | None:
    """Compile a single longest-match-first regex covering every known term.

    Terms are matched on word boundaries, case-insensitively, so ``Hunter`` does
    not clobber part of an unrelated word. Longer terms are tried first so
    ``Predator Hunter`` wins over ``Hunter``.
    """

    terms = _merged_terms()
    if not terms:
        return None
    # Sort longest-first so the alternation prefers the longest match.
    keys = sorted(terms, key=len, reverse=True)
    # re.escape each key; they are already casefolded ASCII/CJK.
    alternation = "|".join(re.escape(k) for k in keys if k)
    # Lookarounds guard against partial-token matches:
    #   (?<![A-Za-z0-9]) — don't start in the middle of an alphanumeric token
    #   (?![A-Za-z])     — don't match "Reinforce" inside "reinforcements"
    # CJK in terms/after is fine because CJK is not in these classes.
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z])", re.IGNORECASE
    )


def preapply_glossary(text: str) -> str:
    """Replace known English terms in ``text`` with their official Chinese names.

    Pure string transform, no LLM call. Longer matches win over shorter ones.
    Returns the original text unchanged if no term matches or no table loaded.
    """

    if not text:
        return text
    pattern = _preapply_pattern()
    if pattern is None:
        return text
    terms = _merged_terms()

    def _replace(match: re.Match[str]) -> str:
        key = match.group(0).strip().casefold()
        return terms.get(key, match.group(0))

    return pattern.sub(_replace, text)


__all__ = [
    "ENEMY_NAMES",
    "PLANET_NAMES",
    "SECTOR_NAMES",
    "STRATAGEM_NAMES",
    "all_terms",
    "contains_cjk",
    "lookup_term",
    "preapply_glossary",
    "reverse_terms",
    "translate_query",
]
