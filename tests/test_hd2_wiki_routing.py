"""get_wiki_message 中文优先 / 英文兜底路由的单元测试。

不发真实网络请求：用 FakeWiki 替身模拟 WikiClient 的 direct/search/pick，
并通过 object.__new__ 跳过 HelldiversService 的重构造，只装配 wiki 相关属性。
异步用例统一用 asyncio.run 包裹（本项目无 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from atri_qq_bot.helldivers.services import HelldiversService


class FakeWiki:
    """WikiClient 替身：按预设脚本返回 direct 页 / 搜索结果 / 最佳标题。"""

    def __init__(
        self,
        *,
        base: str = "https://helldivers.wiki.gg",
        direct: dict[str, Any] | None = None,
        results: list[dict[str, Any]] | None = None,
        best: str | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self._direct = direct
        self._results = results or []
        self._best = best
        self._raise_on = raise_on
        self.page_calls: list[str] = []
        self.search_calls: list[str] = []

    def page_url(self, title: str) -> str:
        return f"{self.base}/wiki/{title.replace(' ', '_')}"

    def search_url(self, query: str) -> str:
        return f"{self.base}/index.php?search={query}"

    async def get_page_summary(
        self, title: str, *, full: bool = False
    ) -> dict[str, Any] | None:
        self.page_calls.append(title)
        if self._raise_on == "page":
            raise OSError("boom page")
        if self._direct is not None and title == self._direct.get("title"):
            return self._direct
        return None

    async def search(
        self, query: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self.search_calls.append(query)
        if self._raise_on == "search":
            raise OSError("boom search")
        return self._results

    def pick_best_title(
        self, query: str, results: list[dict[str, Any]]
    ) -> str | None:
        if self._raise_on == "pick":
            raise OSError("boom pick")
        return self._best


def _page(title: str, extract: str, url: str | None = None) -> dict[str, Any]:
    return {"title": title, "extract": extract, "url": url, "pageid": 1}


def _make_service(
    *,
    wiki_client: Any,
    wiki_zh_client: Any,
) -> HelldiversService:
    # translate_wiki=False：英文流程也不触发真实机翻，聚焦路由而非翻译。
    svc = object.__new__(HelldiversService)
    svc.enable_wiki = True
    svc.translate_wiki = False
    svc.wiki_client = wiki_client
    svc.wiki_zh_client = wiki_zh_client
    return svc


def _run(coro: Any) -> str:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 中文优先
# ---------------------------------------------------------------------------


def test_zh_direct_hit_uses_chinese_client_only() -> None:
    zh = FakeWiki(
        base="https://helldivers.wiki.gg/zh",
        direct=_page(
            "磁轨炮",
            "磁轨炮是一种充能式支援武器。",
            "https://helldivers.wiki.gg/zh/wiki/RS-422_磁轨炮",
        ),
    )
    en = FakeWiki()
    svc = _make_service(wiki_client=en, wiki_zh_client=zh)

    msg = _run(svc.get_wiki_message("磁轨炮"))

    assert "📖 Wiki | 磁轨炮" in msg
    assert "磁轨炮是一种充能式支援武器" in msg
    assert "/zh/" in msg
    # 中文 direct 命中后，英文站完全不被触碰
    assert en.page_calls == []
    assert en.search_calls == []


def test_zh_search_pick_returns_summary_without_english() -> None:
    zh = FakeWiki(
        base="https://helldivers.wiki.gg/zh",
        results=[{"title": "RS-422 磁轨炮", "snippet": "充能武器", "pageid": 1}],
        best="RS-422 磁轨炮",
        direct=_page("RS-422 磁轨炮", "磁轨炮正文。"),
    )
    en = FakeWiki()
    svc = _make_service(wiki_client=en, wiki_zh_client=zh)

    msg = _run(svc.get_wiki_message("磁轨炮"))

    assert "RS-422 磁轨炮" in msg
    assert "磁轨炮正文" in msg
    assert en.search_calls == []
    assert en.page_calls == []


def test_zh_candidate_list_does_not_fall_back() -> None:
    # 有候选但低置信（best=None）：展示中文列表，不回退英文站
    zh = FakeWiki(
        results=[{"title": "某个中文词条", "snippet": "摘要片段", "pageid": 1}]
    )
    en = FakeWiki()
    svc = _make_service(wiki_client=en, wiki_zh_client=zh)

    msg = _run(svc.get_wiki_message("某个词"))

    assert "Wiki 搜索" in msg
    assert "某个中文词条" in msg
    assert en.search_calls == []


# ---------------------------------------------------------------------------
# 英文兜底
# ---------------------------------------------------------------------------


def test_zh_empty_falls_back_to_english_client() -> None:
    # 中文站无 direct、无结果 → empty
    zh = FakeWiki(base="https://helldivers.wiki.gg/zh")
    en = FakeWiki(
        direct=_page(
            "rs-422 railgun",
            "english body",
            "https://helldivers.wiki.gg/wiki/RS-422_Railgun",
        )
    )
    svc = _make_service(wiki_client=en, wiki_zh_client=zh)

    msg = _run(svc.get_wiki_message("磁轨炮"))

    # 中文站确实被检索过
    assert zh.search_calls == ["磁轨炮"]
    # 英文流程：词表把“磁轨炮”转成 rs-422 railgun 并命中
    assert "rs-422 railgun" in msg
    assert "english body" in msg
    assert en.page_calls  # 英文站被调用


def test_zh_client_exception_falls_back_to_english() -> None:
    zh = FakeWiki(raise_on="page")
    en = FakeWiki(direct=_page("rs-422 railgun", "english body"))
    svc = _make_service(wiki_client=en, wiki_zh_client=zh)

    msg = _run(svc.get_wiki_message("磁轨炮"))

    assert "rs-422 railgun" in msg
    assert "english body" in msg


def test_zh_empty_and_glossary_miss_returns_hint() -> None:
    # 中文站无命中，且词表也没有“胆汁泰坦”，提示改用英文
    zh = FakeWiki()
    en = FakeWiki()
    svc = _make_service(wiki_client=en, wiki_zh_client=zh)

    msg = _run(svc.get_wiki_message("胆汁泰坦"))

    assert "未在官方译名词表中找到" in msg
    assert "胆汁泰坦" in msg


def test_zh_query_without_zh_client_uses_english_directly() -> None:
    en = FakeWiki(direct=_page("rs-422 railgun", "english body"))
    svc = _make_service(wiki_client=en, wiki_zh_client=None)

    msg = _run(svc.get_wiki_message("磁轨炮"))

    assert "rs-422 railgun" in msg
    assert "english body" in msg


# ---------------------------------------------------------------------------
# 英文查询与开关
# ---------------------------------------------------------------------------


def test_english_query_skips_chinese_client() -> None:
    zh = FakeWiki()
    en = FakeWiki(direct=_page("railgun", "english body"))
    svc = _make_service(wiki_client=en, wiki_zh_client=zh)

    msg = _run(svc.get_wiki_message("railgun"))

    assert "railgun" in msg
    assert "english body" in msg
    # 非 CJK 查询不碰中文站
    assert zh.page_calls == []
    assert zh.search_calls == []


def test_disabled_wiki_and_missing_client_are_guarded() -> None:
    svc = _make_service(wiki_client=FakeWiki(), wiki_zh_client=FakeWiki())
    svc.enable_wiki = False
    assert "关闭" in _run(svc.get_wiki_message("磁轨炮"))

    svc2 = _make_service(wiki_client=None, wiki_zh_client=None)
    assert "未初始化" in _run(svc2.get_wiki_message("磁轨炮"))


def test_blank_query_returns_usage() -> None:
    svc = _make_service(wiki_client=FakeWiki(), wiki_zh_client=FakeWiki())
    assert "Wiki 查询用法" in _run(svc.get_wiki_message("   "))
