"""业务编排：缓存读取、翻译、格式化。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._logging import logger

from .clients.api_client import HD2Client
from .clients.arsenal_client import ArsenalClient
from .renderers.awaiting_orders_renderer import AwaitingOrdersRenderer
from .core.cache import CacheService
from .core.campaign_trends import CampaignTrendStore
from .renderers.card_renderer import CardRenderer
from .renderers.card_specs import (
    build_dashboard_card,
    build_dss_card,
    build_global_events_card,
    build_news_card,
    build_order_card,
    build_planet_candidates_card,
    build_planet_card,
    build_stats_card,
    build_warfront_card,
)
from .clients.companion_screenshot import CompanionTranslationResult
from .renderers.dashboard_renderer import DashboardRenderer
from .renderers.dispatch_renderer import DispatchRenderer
from .renderers.dss_renderer import DSSRenderer
from .core.formatters import (
    WIKI_USAGE,
    format_dss_message,
    format_global_events_message,
    format_map_caption,
    format_news_message,
    format_order_message,
    format_planet_candidates,
    format_planet_message,
    format_stats_message,
    format_steam_message,
    format_warfront_message,
    format_wiki_search_results,
    format_wiki_summary,
)
from .renderers.global_event_renderer import GlobalEventRenderer
from .core.glossary import contains_cjk, preapply_glossary, translate_query
from .core.i18n import known_planet_names, lookup_planet_name
from .renderers.major_order_renderer import MajorOrderRenderer
from .renderers.map_renderer import MapRenderer
from .clients.remote_image_cache import RemoteImageCache
from .core.stats_model import build_stats_view_model
from .renderers.stats_renderer import StatsRenderer
from .core.steam_parser import ParsedSteamContent, SteamBlock, parse_steam_content
from .renderers.steam_renderer import SteamRenderer
from .core.text_utils import clean_game_text
from .clients.translator import TranslationCache, TranslationService
from .core.war_state import WarState, normalize_faction, planet_index, planet_name
from .renderers.warfront_renderer import WarfrontRenderer
from .clients.wiki_client import WikiClient

_COMPANION_STATIC_TRANSLATIONS = {
    "review message from super earth": "查阅来自超级地球的消息",
    "provided by the ministry of truth, and our patriotic sponsors": "由真理部及爱国赞助商提供",
    "close": "关闭",
    "major order": "最高指令",
    "next ftl jump:": "下次超光速跃迁：",
    "estimated ready time:": "预计准备时间：",
    "available in:": "可用倒计时：",
    "defense campaigns cannot originate from this planet. the hellpod space optimization booster is automatically active for all missions.": "该星球不会触发防御战役；所有任务都会自动启用“地狱舱空间优化”强化。",
    "deploys periodic eagle airstrikes during missions. slows enemy progress in defense campaigns.": "任务期间将定期发动飞鹰空袭，并延缓敌军在防御战役中的推进。",
    "gives access to the orbital 380mm he barrage stratagem during missions. accelerates progress in liberation campaigns.": "任务期间可调用“轨道 380 毫米高爆弹幕”战略配备，同时加快解放战役进度。",
    "planet statistics": "星球统计",
    "helldivers active": "在线绝地潜兵",
    "missions completed": "已完成任务",
    "missions failed": "失败任务",
    "mission dive time": "任务投入时间",
    "shots fired": "射击次数",
    "projectile hits": "命中次数",
    "hits to shot ratio": "命中射击比",
    "kill count": "击杀数",
    "death count": "阵亡数",
    "sector": "扇区",
}

_ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
_PRESERVED_ENGLISH_TOKENS = {
    "api",
    "dss",
    "ftl",
    "hd2",
    "html",
    "id",
    "json",
    "png",
    "ui",
    "url",
    "utc",
    "xp",
}


def _has_translatable_english(text: str) -> bool:
    words = _ENGLISH_WORD_RE.findall(str(text or ""))
    return any(
        len(word) >= 4 and word.casefold() not in _PRESERVED_ENGLISH_TOKENS
        for word in words
    )


class HelldiversService:
    """Helldivers 查询服务。"""

    CACHE_WAR_SUMMARY = "war_summary"
    CACHE_WAR_V1 = "war_v1"
    CACHE_WAR_STATUS = "war_status"
    CACHE_ORDERS = "major_orders"
    # Bump cache key so any previous Companion/Steam news cache is never reused.
    CACHE_DISPATCHES = "dispatches_api_v1"
    CACHE_STEAM = "steam_updates"
    CACHE_PLANETS = "planets"
    CACHE_CAMPAIGNS = "campaigns"
    CACHE_PLANET_EVENTS = "planet_events"
    CACHE_SPACE_STATIONS = "space_stations"
    CACHE_ASSIGNMENTS_V1 = "assignments_v1"
    CACHE_GLOBAL_EVENTS = "global_events"  # 旧磁盘缓存兼容

    def __init__(
        self,
        client: HD2Client,
        cache: CacheService,
        translator: TranslationService,
        translation_cache: TranslationCache,
        *,
        steam_max_content_length: int = 2000,
        steam_balancing_limit: int = 800,
        steam_fixes_limit: int = 600,
        steam_issues_limit: int = 300,
        steam_max_sections: int = 3,
        steam_image_enabled: bool = True,
        rotation_dispatches: int = 300,
        rotation_orders: int = 600,
        rotation_steam: int = 900,
        enable_translation: bool = True,
        wiki_client: WikiClient | None = None,
        enable_wiki: bool = True,
        translate_wiki: bool = True,
        map_renderer: MapRenderer | None = None,
        enable_map: bool = True,
        card_renderer: CardRenderer | None = None,
        dashboard_renderer: DashboardRenderer | None = None,
        stats_renderer: StatsRenderer | None = None,
        dss_renderer: DSSRenderer | None = None,
        major_order_renderer: MajorOrderRenderer | None = None,
        awaiting_orders_renderer: AwaitingOrdersRenderer | None = None,
        dispatch_renderer: DispatchRenderer | None = None,
        warfront_renderer: WarfrontRenderer | None = None,
        global_event_renderer: GlobalEventRenderer | None = None,
        steam_renderer: SteamRenderer | None = None,
        remote_image_cache: RemoteImageCache | None = None,
        campaign_trends: CampaignTrendStore | None = None,
        dashboard_low_impact_threshold: int = 800,
        drop_untranslated_content: bool = True,
        companion_translation_batch_timeout: float = 9.0,
        companion_translation_budget: float = 18.0,
        arsenal_client: ArsenalClient | None = None,
    ) -> None:
        self.client = client
        self.cache = cache
        self.translator = translator
        self.translation_cache = translation_cache
        self.steam_max_content_length = steam_max_content_length
        self.steam_balancing_limit = steam_balancing_limit
        self.steam_fixes_limit = steam_fixes_limit
        self.steam_issues_limit = steam_issues_limit
        self.steam_max_sections = steam_max_sections
        self.steam_image_enabled = bool(steam_image_enabled)
        self.rotation_dispatches = max(60, int(rotation_dispatches))
        self.rotation_orders = max(60, int(rotation_orders))
        self.rotation_steam = max(60, int(rotation_steam))
        self.enable_translation = enable_translation
        self.wiki_client = wiki_client
        self.enable_wiki = enable_wiki
        self.translate_wiki = translate_wiki and enable_translation
        self.map_renderer = map_renderer
        self.enable_map = enable_map
        self.card_renderer = card_renderer
        self.dashboard_renderer = dashboard_renderer
        self.stats_renderer = stats_renderer
        self.dss_renderer = dss_renderer
        self.major_order_renderer = major_order_renderer
        self.awaiting_orders_renderer = awaiting_orders_renderer
        self.dispatch_renderer = dispatch_renderer
        self.warfront_renderer = warfront_renderer
        self.global_event_renderer = global_event_renderer
        self.steam_renderer = steam_renderer
        self.remote_image_cache = remote_image_cache
        self.campaign_trends = campaign_trends
        self.dashboard_low_impact_threshold = max(
            0, int(dashboard_low_impact_threshold or 800)
        )
        self.drop_untranslated_content = bool(drop_untranslated_content)
        self.companion_translation_batch_timeout = max(
            3.0, min(60.0, float(companion_translation_batch_timeout))
        )
        # Large pages can need multiple free-model rounds.  Cap high enough that
        # a 60–90s budget from config is actually honored.
        self.companion_translation_budget = max(
            self.companion_translation_batch_timeout,
            min(120.0, float(companion_translation_budget)),
        )
        self.arsenal_client = arsenal_client

        self._rotation_tasks: list[asyncio.Task] = []
        self._trend_task: asyncio.Task | None = None
        self._warmup_task: asyncio.Task | None = None
        self._companion_translation_tasks: set[asyncio.Task] = set()
        self._register_fetchers()

    async def _render_card(self, card) -> Path | None:
        if self.card_renderer is None or not self.card_renderer.available:
            return None
        return await asyncio.to_thread(self.card_renderer.render, card)

    def _register_fetchers(self) -> None:
        self.cache.register(self.CACHE_WAR_SUMMARY, self.client.fetch_war_summary)
        self.cache.register(self.CACHE_WAR_V1, self.client.fetch_war_v1)
        self.cache.register(self.CACHE_WAR_STATUS, self.client.fetch_war_status)
        self.cache.register(self.CACHE_ORDERS, self.client.fetch_major_orders)
        self.cache.register(self.CACHE_DISPATCHES, self.client.fetch_dispatches)
        self.cache.register(self.CACHE_STEAM, self.client.fetch_steam_updates)
        self.cache.register(self.CACHE_PLANETS, self.client.fetch_planets)
        self.cache.register(self.CACHE_CAMPAIGNS, self.client.fetch_campaigns)
        self.cache.register(self.CACHE_PLANET_EVENTS, self.client.fetch_planet_events)
        self.cache.register(self.CACHE_SPACE_STATIONS, self.client.fetch_space_stations)
        self.cache.register(self.CACHE_ASSIGNMENTS_V1, self.client.fetch_assignments_v1)

    async def start(self) -> None:
        await self.cache.start()
        # 顺序预热缺失缓存（不再并发，避免启动即 429）
        self._warmup_task = asyncio.create_task(
            self._warmup_cache(), name="hd2-cache-warmup"
        )
        if self.campaign_trends is not None:
            self._trend_task = asyncio.create_task(
                self._campaign_trend_loop(), name="hd2-campaign-trends"
            )
        if self.enable_translation:
            self._rotation_tasks = [
                asyncio.create_task(
                    self._rotation_loop("dispatches", self.rotation_dispatches),
                    name="hd2-tr-dispatches",
                ),
                asyncio.create_task(
                    self._rotation_loop("orders", self.rotation_orders),
                    name="hd2-tr-orders",
                ),
                asyncio.create_task(
                    self._rotation_loop("steam", self.rotation_steam),
                    name="hd2-tr-steam",
                ),
            ]
            logger.info("[HD2] translation rotation tasks started")

    async def _warmup_cache(self) -> None:
        """顺序预热：仅刷新磁盘/内存缺失的核心键。"""
        try:
            # 让后台 refresh_all 先起步一点，错开
            await asyncio.sleep(1.0)
            for name in (
                self.CACHE_WAR_SUMMARY,
                self.CACHE_WAR_STATUS,
                self.CACHE_ORDERS,
                self.CACHE_PLANETS,
                self.CACHE_CAMPAIGNS,
            ):
                # 已有缓存则跳过（soft_ttl 内）
                if await self.cache.get(name) is not None:
                    continue
                await self.cache.refresh_one(name, force=True)
                await asyncio.sleep(max(self.cache.request_delay, 2.0))
            logger.info("[HD2] cache warmup completed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[HD2] cache warmup failed: {e}")

    async def stop(self) -> None:
        if self._warmup_task and not self._warmup_task.done():
            self._warmup_task.cancel()
            await asyncio.gather(self._warmup_task, return_exceptions=True)
        self._warmup_task = None
        if self._trend_task and not self._trend_task.done():
            self._trend_task.cancel()
            await asyncio.gather(self._trend_task, return_exceptions=True)
        self._trend_task = None
        for task in self._rotation_tasks:
            if task and not task.done():
                task.cancel()
        if self._rotation_tasks:
            await asyncio.gather(*self._rotation_tasks, return_exceptions=True)
        self._rotation_tasks = []
        # 关停语义：集合里是在途后台翻译批次（预算到期后最长可跑数十秒），
        # 终止时必须 cancel 丢弃而不是 gather 等其自然完成，否则插件卸载
        # 会被在途 LLM 请求拖住。cancel 已完成的任务是无害空操作。
        companion_tasks = list(self._companion_translation_tasks)
        for task in companion_tasks:
            if not task.done():
                task.cancel()
        if companion_tasks:
            await asyncio.gather(*companion_tasks, return_exceptions=True)
        self._companion_translation_tasks.clear()
        if self.arsenal_client is not None:
            try:
                await self.arsenal_client.stop()
            except Exception as e:
                logger.warning(f"[HD2] arsenal client stop error: {e}")
        await self.cache.stop()
        if self.wiki_client is not None:
            try:
                await self.wiki_client.close()
            except Exception as e:
                logger.warning(f"[HD2] wiki client close error: {e}")
        if self.remote_image_cache is not None:
            try:
                await self.remote_image_cache.close()
            except Exception as e:
                logger.warning(f"[HD2] remote image cache close error: {e}")
        await self.translator.close()
        await self.client.close()
        await self.translation_cache.flush()

    async def _campaign_trend_loop(self) -> None:
        """定期记录缓存中的战役进度，不额外请求 API。"""
        await asyncio.sleep(8)
        while True:
            try:
                campaigns = await self.cache.get(self.CACHE_CAMPAIGNS)
                if isinstance(campaigns, list) and self.campaign_trends is not None:
                    state = WarState(campaigns=campaigns)
                    await asyncio.to_thread(
                        self.campaign_trends.record_rows, state.campaign_rows()
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[HD2] campaign trend sample failed: {e}")
            try:
                await asyncio.sleep(max(60, self.cache.update_interval))
            except asyncio.CancelledError:
                break

    async def _rotation_loop(self, kind: str, interval: int) -> None:
        # 稍等 API 缓存先填一轮
        await asyncio.sleep(min(10, interval // 10 or 1))
        while True:
            try:
                if kind == "dispatches":
                    await self.ensure_dispatches_translated()
                elif kind == "orders":
                    await self.ensure_orders_translated()
                elif kind == "steam":
                    await self.ensure_steam_translated()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(f"[HD2] translation rotation ({kind}) error")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    # ---- stats ----

    @staticmethod
    def _subtitle() -> str:
        from datetime import datetime

        return datetime.now().strftime("更新 %m-%d %H:%M")

    async def get_stats_images(self, *, realtime: bool = False) -> list[Path]:
        if realtime:
            await self._refresh_realtime(
                [
                    self.CACHE_WAR_SUMMARY,
                    self.CACHE_WAR_V1,
                    self.CACHE_PLANETS,
                    self.CACHE_WAR_STATUS,
                ]
            )
        summary = await self.cache.get_or_refresh(self.CACHE_WAR_SUMMARY)
        war_v1 = await self.cache.get_or_refresh(self.CACHE_WAR_V1)
        planets = await self.cache.get_or_refresh(self.CACHE_PLANETS)
        war_status = await self.cache.get_or_refresh(self.CACHE_WAR_STATUS)
        if not summary and not war_v1:
            return []
        if self.stats_renderer is not None:
            model = build_stats_view_model(
                summary,
                planets=planets if isinstance(planets, list) else [],
                war=war_v1 if isinstance(war_v1, dict) else {},
                status=war_status if isinstance(war_status, dict) else {},
            )
            distribution = model.get("distribution")
            if isinstance(distribution, dict):
                top_planets = distribution.get("top_planets")
                if isinstance(top_planets, list):
                    await self._localize_planet_rows(
                        [item for item in top_planets if isinstance(item, dict)],
                        include_english=False,
                    )
            p = await asyncio.to_thread(self.stats_renderer.render, model)
            return [p] if p else []
        card = build_stats_card(summary, war_v1, subtitle=self._subtitle())
        p = await self._render_card(card)
        return [p] if p else []

    async def get_stats_message(self) -> str:
        summary = await self.cache.get_or_refresh(self.CACHE_WAR_SUMMARY)
        war_v1 = await self.cache.get_or_refresh(self.CACHE_WAR_V1)
        if not summary and not war_v1:
            return "❌ 暂时无法获取银河战争统计，数据可能正在同步，请稍后重试。"
        legacy_summary = (
            summary.get("galaxy_stats")
            if isinstance(summary, dict)
            and isinstance(summary.get("galaxy_stats"), dict)
            else summary
        )
        return format_stats_message(legacy_summary, war_v1)

    # ---- orders ----

    async def ensure_orders_translated(self) -> None:
        orders = await self.cache.get(self.CACHE_ORDERS)
        if not orders:
            return
        ids = [str(o.get("id", i)) for i, o in enumerate(orders)]
        # order_field 键形如 `{id}:{field}:{digest}`，用前缀保留当前订单
        await self.translation_cache.prune_prefixes(
            "order_field", [f"{i}:" for i in ids]
        )
        for order in orders:
            await self._translate_order(order)
            await asyncio.sleep(0.1)

    async def _translate_order(self, order: dict[str, Any]) -> dict[str, str]:
        setting = order.get("setting") or {}
        item_id = str(order.get("id", 0))
        fields = {
            "title": str(setting.get("overrideTitle") or ""),
            "brief": str(setting.get("overrideBrief") or ""),
            "task": str(setting.get("taskDescription") or ""),
        }
        if not self.enable_translation:
            return fields

        result: dict[str, str] = {}
        for field, original in fields.items():
            result[field] = (
                await self._translate_cached_text(
                    "order_field", f"{item_id}:{field}", original
                )
                if original
                else ""
            )
        return result

    async def _load_orders(self) -> list[dict[str, Any]] | None:
        orders = await self.cache.get_or_refresh(self.CACHE_ORDERS)
        # 注意：API 可能返回空列表 []（当前无活跃最高命令），不能用 `not orders`
        if orders is None:
            orders = await self.cache.get_or_refresh(self.CACHE_ASSIGNMENTS_V1)
        if orders is None:
            return None
        if not isinstance(orders, list):
            return []
        return orders

    async def get_order_images(self) -> list[Path]:
        orders = await self._load_orders()
        if orders is None:
            return []
        if len(orders) == 0:
            if self.awaiting_orders_renderer is not None:
                p = await asyncio.to_thread(self.awaiting_orders_renderer.render)
                return [p] if p else []
            # 专用 renderer 不可用时才回退通用卡。
            from .renderers.card_renderer import FACTION_COLORS, Card

            card = Card(
                title="AW · 等待最高命令",
                accent=FACTION_COLORS["Humans"],
                emblem="super_earth",
                footer="",
            )
            card.text("等待超级地球最高指挥部的进一步指示")
            p = await self._render_card(card)
            return [p] if p else []
        paths: list[Path] = []
        for i, order in enumerate(orders, 1):
            fields = await self._translate_order(order)
            if self.major_order_renderer is not None:
                p = await asyncio.to_thread(
                    self.major_order_renderer.render,
                    order,
                    index=i,
                    title=fields["title"],
                    brief=fields["brief"],
                    task_desc=fields["task"],
                )
                if p:
                    paths.append(p)
                continue
            card = build_order_card(
                order,
                index=i,
                title=fields["title"],
                brief=fields["brief"],
                task_desc=fields["task"],
            )
            p = await self._render_card(card)
            if p:
                paths.append(p)
        return paths

    async def get_order_messages(self) -> list[str]:
        orders = await self._load_orders()
        if orders is None:
            return ["❌ 暂时无法获取最高命令，数据源请求失败，请稍后重试。"]
        if len(orders) == 0:
            return ["📋 当前没有活跃的最高命令"]

        messages: list[str] = []
        for i, order in enumerate(orders, 1):
            fields = await self._translate_order(order)
            messages.append(
                format_order_message(
                    order,
                    index=i,
                    title=fields["title"],
                    brief=fields["brief"],
                    task_desc=fields["task"],
                )
            )
        return messages

    # ---- news ----

    async def ensure_dispatches_translated(self) -> None:
        dispatches = await self.cache.get(self.CACHE_DISPATCHES)
        if not dispatches:
            return
        top = dispatches[:5]
        ids = [str(d.get("id", i)) for i, d in enumerate(top)]
        await self.translation_cache.clear_outdated("dispatches", ids)
        for d in top:
            await self._translate_dispatch(d)
            await asyncio.sleep(0.1)

    @staticmethod
    def _normalize_dispatch(dispatch: dict[str, Any]) -> dict[str, Any]:
        """Normalize community API dispatches the same way GWW does."""

        item = dict(dispatch)
        raw = str(item.get("message") or "")
        match = re.match(r"^<i=\d+>(.*?)</i>\s*", raw, re.DOTALL | re.IGNORECASE)
        title = ""
        body = raw
        if match:
            title = clean_game_text(match.group(1))
            body = raw[match.end() :]
        body = clean_game_text(body)
        item["title"] = str(item.get("title") or title or "超级地球快讯").strip()
        item["message"] = body or clean_game_text(raw)
        # Official dispatches never carry Steam CDN images.
        item.pop("image_url", None)
        item["source"] = "api.helldivers2.dev/api/v1/dispatches"
        return item

    async def _translate_dispatch(self, dispatch: dict[str, Any]) -> str:
        item = self._normalize_dispatch(dispatch)
        dispatch.update(item)
        item_id = str(item.get("id", 0))
        raw_message = str(item.get("message") or "")
        original, replacements = await self._dispatch_planet_placeholders(raw_message)
        if not original:
            return ""
        if not self.enable_translation:
            return self._restore_dispatch_planets(original, replacements)

        cached = await self.translation_cache.get("dispatches", item_id)
        if (
            cached
            and cached.get("original_text") == original
            and cached.get("translated_text")
        ):
            cached_text = str(cached.get("translated_text") or original)
            if (
                cached_text == original
                and self.drop_untranslated_content
                and _has_translatable_english(original)
            ):
                return ""
            return self._restore_dispatch_planets(
                cached_text, replacements
            )

        translated = await self.translator.translate_text(original)
        if translated and translated != original:
            await self.translation_cache.store(
                "dispatches",
                item_id,
                original,
                translated,
                metadata={
                    "published": item.get("published"),
                    "type": item.get("type"),
                    "source": item.get("source"),
                },
            )
            return self._restore_dispatch_planets(translated, replacements)

        if self._translation_rate_limited():
            # 限流/超时属瞬态失败：不写负缓存、不剔除，先回退原文，
            # 等下一个轮转周期补翻。
            return self._restore_dispatch_planets(original, replacements)

        # 失败也缓存原文，降低重复请求
        await self.translation_cache.store(
            "dispatches",
            item_id,
            original,
            original,
            success=False,
            fallback_reason="unchanged",
            metadata={
                "published": item.get("published"),
                "type": item.get("type"),
                "source": item.get("source"),
                "fallback": True,
            },
        )
        if self.drop_untranslated_content and _has_translatable_english(original):
            logger.warning(
                "[HD2] 丢弃未翻译快讯 item=%s text=%r",
                item_id,
                original[:80],
            )
            return ""
        return self._restore_dispatch_planets(original, replacements)

    async def _dispatch_planet_placeholders(
        self, text: str
    ) -> tuple[str, dict[str, str]]:
        """Protect current planet names from being translated as ``未知星球``."""

        candidates = set(known_planet_names())
        planets = await self.cache.get(self.CACHE_PLANETS)
        if isinstance(planets, list):
            for planet in planets:
                if not isinstance(planet, dict):
                    continue
                name = planet.get("name") or planet.get("planetName")
                if isinstance(name, str) and name.strip():
                    candidates.add(name.strip())
        replacements: dict[str, str] = {}
        result = text
        for candidate in sorted(candidates, key=len, reverse=True):
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            match = pattern.search(result)
            if match is None:
                continue
            placeholder = f"__HD2PLANET{len(replacements)}__"
            replacements[placeholder] = await self._localize_planet_name(match.group(0))
            result = pattern.sub(placeholder, result)
        return result, replacements

    @staticmethod
    def _restore_dispatch_planets(text: str, replacements: dict[str, str]) -> str:
        result = str(text)
        for placeholder, name in replacements.items():
            result = re.sub(re.escape(placeholder), lambda _: name, result, flags=re.IGNORECASE)
        return result

    async def get_news_message(self, index: int = 1) -> str:
        if index < 1 or index > 5:
            return "❌ 快讯序号需在 1-5 之间，例如: /news 1"
        dispatches = await self.cache.get_or_refresh(self.CACHE_DISPATCHES)
        if dispatches is None:
            return "❌ 暂时无法获取游戏快讯，数据源请求失败，请稍后重试。"
        if not isinstance(dispatches, list) or len(dispatches) == 0:
            return "📰 当前没有可显示的快讯。"
        if index > len(dispatches):
            return f"❌ 当前仅有 {len(dispatches)} 条快讯，请使用 1-{len(dispatches)}"

        dispatch = self._normalize_dispatch(dispatches[index - 1])
        text = await self._translate_dispatch(dispatch)
        title = str(dispatch.get("title") or "")
        if self.enable_translation and title:
            dispatch["title"] = await self._translate_cached_text(
                "dispatch_title", str(dispatch.get("id", 0)), title
            )
        return format_news_message(dispatch, index=index, message_text=text)

    async def get_news_image(self, index: int = 1) -> tuple[str, list[Path]]:
        if index < 1 or index > 5:
            return "❌ 快讯序号需在 1-5 之间，例如：/news 1", []
        dispatches = await self.cache.get_or_refresh(self.CACHE_DISPATCHES)
        if dispatches is None:
            return "❌ 暂时无法获取游戏快讯，数据源请求失败，请稍后重试。", []
        if not isinstance(dispatches, list) or len(dispatches) == 0:
            return "📰 当前没有可显示的快讯。", []
        if index > len(dispatches):
            return (
                f"❌ 当前仅有 {len(dispatches)} 条快讯，请使用 1-{len(dispatches)}",
                [],
            )
        dispatch = self._normalize_dispatch(dispatches[index - 1])
        text = await self._translate_dispatch(dispatch)
        title = str(dispatch.get("title") or "")
        if self.enable_translation and title:
            dispatch["title"] = await self._translate_cached_text(
                "dispatch_title", str(dispatch.get("id", 0)), title
            )
        if self.dispatch_renderer is not None:
            paths = await asyncio.to_thread(
                self.dispatch_renderer.render,
                dispatch,
                index=index,
                message_text=text,
                hero_path=None,
            )
            return "", paths
        card = build_news_card(dispatch, index=index, message_text=text)
        p = await self._render_card(card)
        return "", [p] if p else []

    async def get_companion_news_image(
        self, news_id: str
    ) -> tuple[str, list[Path]]:
        try:
            article_id = int(str(news_id).strip())
        except (TypeError, ValueError):
            return "❌ Companion 新闻编号无效。", []
        article = await self.client.fetch_companion_gww_news(article_id)
        if article is None:
            return f"❌ 未找到 Companion 新闻 #{article_id}。", []

        dispatch = self._normalize_dispatch(article)
        text = await self._translate_dispatch(dispatch)
        title = str(dispatch.get("title") or "")
        if self.enable_translation and title:
            dispatch["title"] = await self._translate_cached_text(
                "companion_news_title", str(article_id), title
            )
        card = build_news_card(
            dispatch,
            message_text=text,
            card_title=f"Strohmann News #{article_id}",
            footer="Helldivers Companion · 本地数据渲染",
        )
        path = await self._render_card(card)
        if path is None:
            return "❌ Companion 新闻图片生成失败，请检查 Pillow 和字体。", []
        return "", [path]

    # ---- steam ----

    async def ensure_steam_translated(self) -> None:
        updates = await self.cache.get(self.CACHE_STEAM)
        if not updates:
            return
        latest = updates[:1]
        ids = [str(u.get("id", i)) for i, u in enumerate(latest)]
        # steam_message / steam_card 键都带摘要后缀，按物品前缀保留
        steam_keep = [f"{i}:" for i in ids]
        await self.translation_cache.prune_prefixes("steam_message", steam_keep)
        await self.translation_cache.prune_prefixes("steam_card", steam_keep)
        for update in latest:
            if isinstance(update, dict):
                await self._prepare_steam_content(update)
            await asyncio.sleep(0.1)

    async def _translate_steam(self, update: dict[str, Any]) -> dict[str, str]:
        item_id = str(update.get("id", "") or "steam")
        original_title = update.get("title", "") or ""
        original_content = update.get("content", "") or ""
        result = {"title": original_title, "content": original_content}

        if not self.enable_translation:
            return result

        if original_title:
            result["title"] = await self._translate_cached_text(
                "steam_message", f"{item_id}:title", original_title
            )
        if original_content:
            result["content"] = await self._translate_cached_text(
                "steam_message", f"{item_id}:content", original_content
            )
        return result

    # steam 卡片批量送译的分块上限：单请求最多 8 段或 800 字符，避免
    # max_tokens 截断批量 JSON 触发逐条修复、重新打满每分钟配额。
    _STEAM_BATCH_MAX_ITEMS = 8
    _STEAM_BATCH_MAX_CHARS = 800

    async def _prepare_steam_content(
        self, update: dict[str, Any]
    ) -> tuple[str, ParsedSteamContent]:
        item_id = str(update.get("id") or "steam")
        title = str(update.get("title") or "Steam Update")
        content = str(update.get("content") or update.get("contents") or "")
        parsed = parse_steam_content(content)

        # 两段式翻译：先同步遍历一遍收集全部待译槽位（长度预算的推进只
        # 取决于源文本），未命中缓存的槽位批量送 LLM——一张卡从 20+ 个
        # 请求降到几个，避免触发 5 次/分钟类配额的 429——再按同一遍历
        # 组装译文。
        slots: dict[str, str] = {}
        statics: dict[str, str] = {}

        def collect(key: str, text: str) -> str:
            if not self.enable_translation or len(text.strip()) < 3:
                statics[key] = text
                return text
            # 与 _translate_cached_text 一致：术语预替换后的文本作为缓存
            # 原文与送译基线；替换后无剩余英文则预处理结果即终态。
            prepared = preapply_glossary(text)
            if prepared != text and not _has_translatable_english(prepared):
                statics[key] = prepared
                return prepared
            slots[key] = prepared
            return ""

        collect("title", title)
        self._walk_steam_content(parsed, collect)
        translations = await self._resolve_steam_slots(slots, item_id)
        translations.update(statics)

        def lookup(key: str, text: str) -> str:
            return translations.get(key, text)

        display_title = lookup("title", title)
        translated_blocks = self._walk_steam_content(parsed, lookup)
        return display_title, ParsedSteamContent(
            blocks=tuple(translated_blocks),
            image_candidates=parsed.image_candidates,
        )

    def _walk_steam_content(
        self,
        parsed: ParsedSteamContent,
        resolve: Callable[[str, str], str],
    ) -> list[SteamBlock]:
        """同步遍历 steam 卡片块，resolve(key, text) 返回该槽位最终文本。

        遍历结构与长度预算推进只取决于源文本、与译文无关，因此收集槽位与
        组装译文复用同一遍历。key 沿用旧缓存键的位置部分（"title"、
        "block:{index}"、"block:{index}:item:{item_index}"），保证旧缓存
        仍然命中。
        """
        drop = self.drop_untranslated_content
        translated_blocks: list[SteamBlock] = []
        remaining = max(0, self.steam_max_content_length)
        for index, block in enumerate(parsed.blocks[:28]):
            if block.kind == "list":
                source_items = tuple(block.items[:12])
                resolved_items: list[str] = []
                for item_index, item in enumerate(source_items):
                    if remaining <= 0:
                        if not drop:
                            resolved_items.append(item)
                        continue
                    translated = resolve(
                        f"block:{index}:item:{item_index}", item[:remaining]
                    )
                    if translated or not drop:
                        resolved_items.append(translated)
                    remaining -= len(item)
                if not resolved_items and drop:
                    continue
                translated_blocks.append(
                    SteamBlock(
                        "list",
                        level=block.level,
                        items=tuple(resolved_items) or source_items,
                        ordered=block.ordered,
                    )
                )
                continue
            if remaining <= 0:
                if not drop:
                    translated_blocks.append(block)
                continue
            source_text = block.text[:remaining]
            translated_text = resolve(f"block:{index}", source_text)
            suffix = block.text[len(source_text) :]
            if drop and _has_translatable_english(suffix):
                suffix = ""
            if not translated_text and drop:
                remaining = max(0, remaining - len(source_text))
                continue
            translated_blocks.append(
                SteamBlock(
                    block.kind,
                    text=translated_text + suffix,
                    level=block.level,
                )
            )
            remaining = max(0, remaining - len(source_text))
        return translated_blocks

    async def _resolve_steam_slots(
        self,
        slots: dict[str, str],
        item_id: str,
    ) -> dict[str, str]:
        """批量解析 steam 卡片槽位：先查缓存，未命中的打包送 LLM。

        返回 key -> 最终文本（"" 表示该槽位按配置剔除）。冷却中的失败属
        瞬态：不写负缓存、以英文原文兜底，等轮转下个周期补翻；只有 API
        正常响应且"译文等于原文"才持久记为失败。
        """
        resolved: dict[str, str] = {}
        pending: list[tuple[str, str, str]] = []  # (key, cache_id, text)
        for key, text in slots.items():
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            cache_id = f"{item_id}:{key}:{digest}"
            cached = await self.translation_cache.get("steam_card", cache_id)
            if cached and cached.get("original_text") == text:
                cached_text = str(cached.get("translated_text") or text)
                if cached_text != text or (
                    not self.drop_untranslated_content
                    or not _has_translatable_english(text)
                ):
                    resolved[key] = cached_text
                else:
                    resolved[key] = ""
                continue
            pending.append((key, cache_id, text))
        if not pending:
            return resolved

        unresolved: list[tuple[str, str, str]] = []
        chunk: list[tuple[str, str, str]] = []
        chunk_chars = 0

        async def flush() -> None:
            nonlocal chunk, chunk_chars
            if not chunk:
                return
            items, chunk = chunk, []
            chunk_chars = 0
            payload = [text for _, _, text in items]
            translated_values = await self.translator.translate_texts(payload)
            for position, (key, cache_id, text) in enumerate(items):
                got = (
                    translated_values[position]
                    if position < len(translated_values)
                    else ""
                )
                if got and got != text:
                    await self.translation_cache.store(
                        "steam_card", cache_id, text, got
                    )
                    resolved[key] = got
                else:
                    unresolved.append((key, cache_id, text))

        for entry in pending:
            text = entry[2]
            if chunk and (
                len(chunk) >= self._STEAM_BATCH_MAX_ITEMS
                or chunk_chars + len(text) > self._STEAM_BATCH_MAX_CHARS
            ):
                await flush()
            chunk.append(entry)
            chunk_chars += len(text)
        await flush()

        if not unresolved:
            return resolved
        if self._translation_rate_limited():
            logger.warning(
                "[HD2] 翻译限流中，steam 卡片 %d 段暂以英文显示，稍后自动补翻",
                len(unresolved),
            )
            for key, _cache_id, text in unresolved:
                resolved[key] = text
            return resolved
        for key, cache_id, text in unresolved:
            await self.translation_cache.store(
                "steam_card",
                cache_id,
                text,
                text,
                success=False,
                fallback_reason="unchanged",
            )
            if not self.drop_untranslated_content or not _has_translatable_english(
                text
            ):
                resolved[key] = text
            else:
                resolved[key] = ""
                logger.warning(
                    "[HD2] 丢弃未翻译文本 namespace=steam_card item=%s text=%r",
                    cache_id,
                    text[:80],
                )
        return resolved

    async def _steam_hero_path(
        self,
        parsed: ParsedSteamContent,
    ) -> Path | None:
        """下载帖子自带的头图；无图时由渲染器回退到捆绑的当前债券横幅。

        不再使用商店 appdetails 的 header_image：该接口在国内网络长期不可达，
        且商店美术与当前战争债券版本脱节，会把过期宣传图当背景。
        """
        if not self.steam_image_enabled or self.remote_image_cache is None:
            return None
        for url in list(parsed.image_candidates)[:6]:
            path = await self.remote_image_cache.get(url)
            if path is not None:
                return path
        return None

    async def get_steam_images(self) -> list[Path]:
        updates = await self.cache.get_or_refresh(self.CACHE_STEAM)
        if not isinstance(updates, list) or not updates:
            return []
        update = updates[0] if isinstance(updates[0], dict) else {}
        title, translated = await self._prepare_steam_content(update)
        hero = await self._steam_hero_path(translated)
        if self.steam_renderer is None:
            return []
        return await asyncio.to_thread(
            self.steam_renderer.render,
            update,
            title=title,
            translated_blocks=translated,
            hero_image=hero,
        )

    async def get_steam_message(self) -> str:
        updates = await self.cache.get_or_refresh(self.CACHE_STEAM)
        if not updates:
            return "❌ 暂时无法获取 Steam 更新日志，数据可能正在同步，请稍后重试。"
        update = updates[0]
        fields = await self._translate_steam(update)
        return format_steam_message(
            update,
            title=fields["title"],
            content=fields["content"],
            max_content_length=self.steam_max_content_length,
            balancing_limit=self.steam_balancing_limit,
            fixes_limit=self.steam_fixes_limit,
            issues_limit=self.steam_issues_limit,
            max_sections=self.steam_max_sections,
        )

    # ---- wiki ----

    async def _translate_wiki_extract(self, title: str, extract: str) -> str:
        if not extract or not self.translate_wiki:
            return extract
        item_id = title
        cached = await self.translation_cache.get("wiki", item_id)
        if (
            cached
            and cached.get("original_text") == extract
            and cached.get("translated_text")
        ):
            cached_text = str(cached["translated_text"])
            if (
                cached_text == extract
                and self.drop_untranslated_content
                and _has_translatable_english(extract)
            ):
                return ""
            return cached_text

        # 送译与判定的基线必须是同一个字符串：translator 失败时原样返回入参，
        # 若拿预处理前的 extract 对比，会把预处理文本误判成译文并永久缓存。
        preapplied = preapply_glossary(extract)
        if preapplied != extract and not _has_translatable_english(preapplied):
            # 术语预替换已覆盖全部英文，预处理结果即终态译文。
            await self.translation_cache.store(
                "wiki",
                item_id,
                extract,
                preapplied,
                metadata={"title": title, "fallback": False},
            )
            return preapplied
        translated = await self.translator.translate_text(preapplied)
        if translated and translated != preapplied:
            await self.translation_cache.store(
                "wiki",
                item_id,
                extract,
                translated,
                metadata={"title": title, "fallback": False},
            )
            return translated
        if self._translation_rate_limited():
            # 限流/超时属瞬态失败：不写负缓存，直接回退原文摘要。
            return extract
        final = "" if self.drop_untranslated_content and _has_translatable_english(extract) else extract
        await self.translation_cache.store(
            "wiki",
            item_id,
            extract,
            extract,
            metadata={"title": title, "fallback": True},
            success=False,
            fallback_reason="unchanged",
        )
        return final

    async def get_arsenal_image(self, query: str) -> list[Path] | str:
        """数据库查询（装备 + 敌人）。返回图片路径列表；文案提示时返回 str。"""
        if self.arsenal_client is None:
            return "❌ 数据库功能未启用。"
        loaded = await self.arsenal_client.ensure_loaded()
        if not loaded:
            return "❌ 装备数据加载失败（下载失败且无本地缓存），请稍后重试。"
        # 敌人数据独立加载，失败不影响装备查询
        try:
            await self.arsenal_client.ensure_enemies()
        except Exception as e:
            logger.warning(f"[HD2] enemies load error: {e}")
        from .renderers.card_specs import (
            build_arsenal_candidates_card,
            build_arsenal_card,
            build_enemy_card,
        )

        results = self.arsenal_client.search(query)
        if not results:
            card = build_arsenal_candidates_card(query, [])
            p = await self._render_card(card)
            return [p] if p else "❌ 未找到匹配条目。支持中文名、型号和俗称（电喷/次抛等）。"

        item = results[0]
        # 敌人条目优先选同词的敌人（"泰坦" 同时命中护甲和敌人时选敌人）
        for candidate in results:
            if candidate.get("productKind") == "enemy":
                item = candidate
                break
        if item.get("productKind") == "enemy":
            # 按需拉取部位护甲（带缓存；失败不阻塞卡片）
            armor_parts: list[tuple[str, int]] = []
            try:
                armor_parts = await self.arsenal_client.fetch_enemy_armor(
                    str(item.get("nameEn") or "")
                )
            except Exception as e:
                logger.warning(f"[HD2] enemy armor load error: {e}")
            card = build_enemy_card(item, armor_parts=armor_parts)
        elif len(results) > 1:
            # 多候选：展示列表卡让用户选（首个结果含在列表里）
            card = build_arsenal_candidates_card(query, results)
        else:
            aliases = self.arsenal_client.aliases_for(str(item.get("id") or ""))
            card = build_arsenal_card(item, aliases=aliases)
        p = await self._render_card(card)
        if p is None:
            return "❌ 卡片渲染不可用（Pillow 未安装）。"
        return [p]

    async def get_wiki_message(self, query: str, *, full: bool = False) -> str:
        if not self.enable_wiki:
            return "❌ Wiki 查询已在配置中关闭（enable_wiki=false）。"
        if self.wiki_client is None:
            return "❌ Wiki 客户端未初始化。"

        query = (query or "").strip()
        if not query:
            return WIKI_USAGE

        # 中文查询先经官方词表翻译成英文（wiki 是英文站，直接发中文会命中无关页）
        original_query = query
        translated_note = ""
        if contains_cjk(query):
            english = translate_query(query)
            if english and english.strip().lower() != query.lower():
                query = english.strip()
                translated_note = f"（已按官方译名转换为：{query}）"
            elif not english:
                # 词表没覆盖的中文词：搜索结果相关性不可信，直接提示用法
                return (
                    f"❓ 未在官方译名词表中找到“{original_query}”。\n"
                    "中文搜索依赖官方译名词表（战备/敌人/星球等 500+ 条），"
                    "请换用英文关键词重试，例如：\n"
                    "· /wiki railgun\n"
                    "· /wiki Bile Titan"
                )

        try:
            # 直接标题命中（仅英文词面；CJK 查询已在上方翻译成英文）
            direct = await self.wiki_client.get_page_summary(query, full=full)
            if direct and direct.get("extract"):
                extract = await self._translate_wiki_extract(
                    str(direct.get("title") or query),
                    str(direct.get("extract") or ""),
                )
                return format_wiki_summary(
                    title=str(direct.get("title") or query),
                    extract=extract,
                    url=str(direct.get("url") or self.wiki_client.page_url(query)),
                    note=translated_note,
                    full=full,
                )

            results = await self.wiki_client.search(query)
            if not results:
                return format_wiki_search_results(
                    query,
                    [],
                    search_url=self.wiki_client.search_url(query),
                )

            best = self.wiki_client.pick_best_title(query, results)
            if best:
                page = await self.wiki_client.get_page_summary(best, full=full)
                if page:
                    extract = await self._translate_wiki_extract(
                        str(page.get("title") or best),
                        str(page.get("extract") or ""),
                    )
                    return format_wiki_summary(
                        title=str(page.get("title") or best),
                        extract=extract,
                        url=str(page.get("url") or self.wiki_client.page_url(best)),
                        note=translated_note,
                        full=full,
                    )

            # 多候选：列表展示
            return format_wiki_search_results(
                query,
                results,
                search_url=self.wiki_client.search_url(query),
            )
        except Exception as e:
            logger.exception(f"[HD2] wiki query error: {e}")
            return "❌ Wiki 查询失败，请稍后重试。"

    # ---- GWW-style war queries ----

    async def _refresh_realtime(self, names: list[str]) -> None:
        """强制刷新指定缓存键（绕过 TTL），用于实时命令。

        refresh_one(force=True) 请求失败时不会覆盖缓存，天然回退旧数据。
        """
        for name in names:
            await self.cache.refresh_one(name, force=True)

    async def _build_war_state(self, *, realtime: bool = False) -> WarState:
        if realtime:
            await self._refresh_realtime(
                [
                    self.CACHE_PLANETS,
                    self.CACHE_CAMPAIGNS,
                    self.CACHE_WAR_V1,
                    self.CACHE_WAR_STATUS,
                ]
            )
        planets = await self.cache.get_or_refresh(self.CACHE_PLANETS)
        campaigns = await self.cache.get_or_refresh(self.CACHE_CAMPAIGNS)
        events = await self.cache.get_or_refresh(self.CACHE_PLANET_EVENTS)
        stations = await self.cache.get_or_refresh(self.CACHE_SPACE_STATIONS)
        war = await self.cache.get_or_refresh(self.CACHE_WAR_V1)
        war_status = await self.cache.get_or_refresh(self.CACHE_WAR_STATUS)
        global_events = (
            war_status.get("globalEvents")
            if isinstance(war_status, dict)
            and isinstance(war_status.get("globalEvents"), list)
            else await self.cache.get(self.CACHE_GLOBAL_EVENTS)
        )
        return WarState(
            planets=planets if isinstance(planets, list) else [],
            campaigns=campaigns if isinstance(campaigns, list) else [],
            planet_events=events if isinstance(events, list) else [],
            space_stations=stations if isinstance(stations, list) else [],
            war=war if isinstance(war, dict) else {},
            global_events=global_events if isinstance(global_events, list) else [],
        )

    @staticmethod
    def _station_with_planet(state: WarState) -> dict[str, Any] | None:
        """Attach the canonical planet payload when the DSS API exposes only its index."""

        station = state.dss_station()
        if station is None:
            return None
        view = copy.deepcopy(station)
        planet = view.get("planet") if isinstance(view.get("planet"), dict) else {}
        if planet.get("name") or view.get("planetName"):
            return view
        index = planet.get("index") or planet.get("planetIndex") or view.get("planetIndex")
        for candidate in state.planets:
            candidate_index = candidate.get("index") or candidate.get("planetIndex")
            if str(candidate_index) == str(index):
                view["planet"] = copy.deepcopy(candidate)
                return view
        return view

    async def _translate_cached_text(
        self,
        namespace: str,
        item_id: str,
        text: str,
        *,
        allow_source_fallback: bool = False,
    ) -> str:
        if not self.enable_translation or len(text.strip()) < 3:
            return text
        # 预替换官方术语（星球/敌人/战备/星系），LLM 只需翻句式，术语不会跑偏。
        preapplied = preapply_glossary(text)
        if preapplied != text:
            # 术语替换后若无剩余可翻译英文，直接当作译文，跳过 LLM 调用。
            if not _has_translatable_english(preapplied):
                return preapplied
            text = preapplied
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        cache_id = f"{item_id}:{digest}"
        cached = await self.translation_cache.get(namespace, cache_id)
        if cached and cached.get("original_text") == text:
            cached_text = str(cached.get("translated_text") or text)
            if cached_text != text:
                return cached_text
            if (
                allow_source_fallback
                or not self.drop_untranslated_content
                or not _has_translatable_english(text)
            ):
                return cached_text
            return ""
        translated = await self.translator.translate_text(text)
        if translated and translated != text:
            await self.translation_cache.store(namespace, cache_id, text, translated)
            return translated
        if self._translation_rate_limited():
            # 429/5xx 冷却属瞬态失败：不写负缓存（否则 TTL 内轮转命中失败
            # 条目会放弃重试），也不剔除内容，先以原文兜底，冷却结束后由
            # 轮转任务自然补翻。
            return text
        await self.translation_cache.store(
            namespace,
            cache_id,
            text,
            text,
            success=False,
            fallback_reason="unchanged",
        )
        if (
            allow_source_fallback
            or not self.drop_untranslated_content
            or not _has_translatable_english(text)
        ):
            return text
        logger.warning(
            "[HD2] 丢弃未翻译文本 namespace=%s item=%s text=%r",
            namespace,
            item_id,
            text[:80],
        )
        return ""

    def _translation_rate_limited(self) -> bool:
        """翻译接口是否处于瞬态失败窗口（429/5xx 冷却，或超时/网络异常）。"""

        for name in ("in_cooldown", "in_transient_failure"):
            checker = getattr(self.translator, name, None)
            if callable(checker) and checker():
                return True
        return False

    async def translate_companion_fragments(
        self,
        target: Any,
        texts: list[str],
    ) -> CompanionTranslationResult:
        """Translate visible Companion DOM fragments before Playwright capture."""
        all_unique = list(dict.fromkeys(text for text in texts if text.strip()))
        kind = str(getattr(target, "kind", "page"))
        # No artificial fragment cap: the DOM extractor already caps at 96
        # nodes, and every fragment deserves a chance to enter the disk
        # translation cache so background warmup eventually translates the
        # full page.  Capping here would permanently drop后排 fragments.
        unique = all_unique
        skipped_count = 0
        translations: dict[str, str] = {}
        pending: list[tuple[str, str]] = []
        unresolved: list[tuple[str, str]] = []
        cached_failures: list[tuple[str, str]] = []
        for source in unique:
            if not _has_translatable_english(source):
                continue
            normalized = " ".join(source.split()).casefold()
            static = _COMPANION_STATIC_TRANSLATIONS.get(normalized)
            if static:
                translations[source] = static
                continue
            official_planet = lookup_planet_name(source)
            if official_planet:
                translations[source] = official_planet
                continue
            # 术语预替换：把已知的星球/敌人/战备/星系名直接换成官方中文。
            preapplied = preapply_glossary(source)
            if preapplied != source and not _has_translatable_english(preapplied):
                # 整段都是已知术语，预替换即终态，无需 LLM。
                translations[source] = preapplied
                continue
            digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
            cache_id = f"{kind}:{normalized[:80]}:{digest}"
            cached = await self.translation_cache.get(
                "companion_page_v3", cache_id
            )
            if cached and cached.get("original_text") == source:
                translated = str(cached.get("translated_text") or source)
                if translated != source:
                    translations[source] = translated
                else:
                    cached_failures.append((source, cache_id))
                continue
            pending.append((source, cache_id))

        async def translate_pending(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
            failed: list[tuple[str, str]] = []
            if not items:
                return failed
            # 送 LLM 前先做术语预替换，让模型看到的是半中文文本。
            payload = [preapply_glossary(source) for source, _ in items]
            translated_values = await self.translator.translate_texts(payload)
            if len(translated_values) < len(items):
                failed.extend(items[len(translated_values) :])
            # 送译与判定基线一致：失败时 translator 返回入参（payload 元素），
            # 与 preapply 后的文本对比才能识别"未翻译"，避免把预处理文本当译文缓存。
            for (source, cache_id), sent, translated in zip(
                items, payload, translated_values, strict=False
            ):
                final = translated if translated and translated != sent else source
                if final != source:
                    await self.translation_cache.store(
                        "companion_page_v3",
                        cache_id,
                        source,
                        final,
                    )
                    translations[source] = final
                elif _has_translatable_english(source):
                    failed.append((source, cache_id))
            return failed

        # Translate all batches in parallel within a fixed foreground budget.
        # A single slow or rate-limited chunk no longer abandons later chunks;
        # every chunk that finishes in time contributes its translations, and
        # chunks still running when the budget expires keep going in the
        # background (their results are written to the disk translation cache,
        # so the next request benefits from them).
        if pending:
            batch_timeout = max(
                3.0,
                min(
                    30.0,
                    float(
                        getattr(
                            self,
                            "companion_translation_batch_timeout",
                            9.0,
                        )
                    ),
                ),
            )
            translation_budget = max(
                batch_timeout,
                min(
                    45.0,
                    float(
                        getattr(self, "companion_translation_budget", 18.0)
                    ),
                ),
            )
            # Split into chunks and launch concurrently.  Larger batches mean
            # fewer API calls under the translator's concurrency limit:
            # 16 items/batch → 96 fragments needs only 6 calls instead of 12.
            batch_size = 16
            chunks: list[list[tuple[str, str]]] = [
                pending[start : start + batch_size]
                for start in range(0, len(pending), batch_size)
            ]
            batch_tasks: list[asyncio.Task] = [
                asyncio.create_task(
                    translate_pending(chunk),
                    name=f"hd2-companion-batch-{kind}-{i * batch_size}",
                )
                for i, chunk in enumerate(chunks)
            ]

            background_tasks: set[asyncio.Task] = getattr(
                self, "_companion_translation_tasks", None
            )
            if background_tasks is None:
                background_tasks = set()
                self._companion_translation_tasks = background_tasks
            # 任务创建后立即登记并挂轻量回收回调：正常完成/预算到期/
            # 外部取消三条退出路径都会自动从集合移除，不存在游离任务。
            background_tasks.update(batch_tasks)

            def _discard_when_done(completed: asyncio.Task) -> None:
                background_tasks.discard(completed)

            for bt in batch_tasks:
                bt.add_done_callback(_discard_when_done)

            def finish_background_batch(
                completed: asyncio.Task,
                *,
                task_set: set[asyncio.Task] = background_tasks,
                page_kind: str = kind,
            ) -> None:
                task_set.discard(completed)
                if completed.cancelled():
                    return
                try:
                    failed = completed.result()
                except Exception as exc:  # noqa: BLE001 - task boundary
                    logger.warning(
                        "[HD2] Companion %s 后台翻译批次失败: %s",
                        page_kind,
                        exc,
                    )
                    return
                logger.info(
                    "[HD2] Companion %s 后台翻译批次完成 unresolved=%s",
                    page_kind,
                    len(failed),
                )

            # asyncio.wait 只等待、不接管任务：超时后子任务照常运行。
            # 不能用 gather + cancel——GatheringFuture.cancel() 会级联
            # 取消全部子任务，在途 LLM 请求会被中断、缓存预热落空。
            done, running = await asyncio.wait(
                batch_tasks, timeout=translation_budget
            )
            if running:
                # 预算到期：在途批次转后台写入缓存，未收集片段视为未译。
                # 带日志的回调只挂在在途批次上；预算内已完成批次的回收
                # 已由 _discard_when_done 处理，不会滞留在集合里。
                logger.warning(
                    "[HD2] Companion %s 翻译预算 %.1fs 到期，已完成的批次生效，"
                    "剩余批次转入后台继续写入缓存",
                    kind,
                    translation_budget,
                )
                for bt in running:
                    bt.add_done_callback(finish_background_batch)
                # Rebuild the unresolved list from scratch to avoid
                # double-counting: completed batches may have already added
                # failed items.  Take the union of (a) items already in
                # unresolved and (b) items not yet translated, deduplicated
                # by source text.
                resolved_sources = set(translations.keys())
                unresolved_set: set[str] = {src for src, _ in unresolved}
                for src, cid in pending:
                    if src not in resolved_sources and src not in unresolved_set:
                        unresolved.append((src, cid))
                        unresolved_set.add(src)
            else:
                # 全部批次在预算内完成：收集各批次结果。
                for bt in done:
                    if bt.cancelled():
                        continue
                    try:
                        result = bt.result()
                    except Exception as exc:  # noqa: BLE001 - task boundary
                        # Unexpected error inside translate_pending: treat the
                        # whole chunk as unresolved so the screenshot keeps the
                        # original English for those fragments.
                        logger.warning(
                            "[HD2] Companion %s 翻译批次异常: %s",
                            kind,
                            exc,
                        )
                        continue
                    unresolved.extend(result)

        untranslated_count = len(cached_failures) + len(unresolved) + skipped_count
        # Distinguish "translation API is rate-limiting us" (transient) from
        # "some fragments genuinely have no translation" (persistent): the caller
        # can surface a clearer "翻译繁忙" message instead of a generic failure.
        rate_limited = untranslated_count > 0 and self._translation_rate_limited()
        return CompanionTranslationResult(
            translations=translations,
            cacheable=untranslated_count == 0,
            complete=untranslated_count == 0,
            untranslated_count=untranslated_count,
            rate_limited=rate_limited,
        )

    async def _localize_planet_name(
        self, value: Any, *, include_english: bool = True
    ) -> str:
        """Prefer an official name; otherwise retain English beside API translation."""

        original = " ".join(str(value or "").split())
        if not original:
            return "未知星球"
        official = lookup_planet_name(original)
        if official:
            return official
        if not self.enable_translation or re.search(r"[\u3400-\u9fff]", original):
            return original
        translated = await self._translate_cached_text(
            "planet_name", original.casefold(), original
        )
        translated = " ".join(str(translated or "").split())
        if translated and translated != original:
            return f"{original}（{translated}）" if include_english else translated
        return original

    async def _localize_planet_rows(
        self, rows: list[dict[str, Any]], *, include_english: bool = True
    ) -> None:
        """Localize the small set of names that a report can actually display."""

        localized: dict[str, str] = {}
        for row in rows:
            planet = row.get("planet") if isinstance(row.get("planet"), dict) else None
            containers = [row] + ([planet] if planet is not None else [])
            for container in containers:
                for key in ("name", "planet_name", "planetName"):
                    value = container.get(key)
                    if not isinstance(value, str) or not value.strip():
                        continue
                    token = value.casefold()
                    if token not in localized:
                        localized[token] = await self._localize_planet_name(
                            value, include_english=include_english
                        )
                    container[key] = localized[token]

    async def _prepare_dss_station(
        self,
        station: dict[str, Any] | None,
        *,
        include_actions: bool = True,
    ):
        if not station:
            return None
        view = copy.deepcopy(station)
        await self._localize_planet_rows([view], include_english=False)
        planet = view.get("planet") if isinstance(view.get("planet"), dict) else {}
        sector = planet.get("sector")
        if isinstance(sector, str) and len(sector.strip()) >= 3:
            planet["sector"] = await self._translate_cached_text(
                "dss_sector", sector.casefold(), sector
            )
        actions = view.get("tacticalActions") or view.get("tactical_actions") or []
        if not include_actions or not isinstance(actions, list):
            return view
        try:
            from .core.i18n import format_dss_action
        except ImportError:  # pragma: no cover - direct module execution
            from i18n import format_dss_action  # type: ignore

        for i, action in enumerate(actions[:6]):
            if not isinstance(action, dict):
                continue
            item_id = str(action.get("id32") or action.get("mediaId32") or i)
            for field, display_field in (
                ("name", "display_name"),
                ("description", "display_description"),
                ("strategicDescription", "display_strategic_description"),
            ):
                text = action.get(field)
                if isinstance(text, str):
                    static = _COMPANION_STATIC_TRANSLATIONS.get(
                        " ".join(text.split()).casefold()
                    )
                    action[display_field] = static or await self._translate_cached_text(
                        "dss_action", f"{item_id}:{field}", text
                    )
            if isinstance(action.get("name"), str):
                action["display_name"] = format_dss_action(
                    action["name"], action.get("display_name")
                )
                icon_by_name = {
                    "飞鹰风暴": "tactical/eagle_storm.png",
                    "轨道封锁": "tactical/orbital_blockade.png",
                    "重型军械分发": "tactical/heavy_ordnance.png",
                }
                icon_asset = icon_by_name.get(str(action["display_name"]))
                if icon_asset:
                    action["icon_asset"] = icon_asset
        return view

    async def get_dss_message(self) -> str:
        state = await self._build_war_state()
        station = await self._prepare_dss_station(
            self._station_with_planet(state),
            include_actions=False,
        )
        return format_dss_message(station)

    async def get_dss_image(self) -> list[Path]:
        state = await self._build_war_state()
        station = await self._prepare_dss_station(self._station_with_planet(state))
        if self.dss_renderer is not None:
            p = await asyncio.to_thread(self.dss_renderer.render, station)
            return [p] if p else []
        card = build_dss_card(station)
        p = await self._render_card(card)
        return [p] if p else []

    def _attach_trends_to_sections(self, sections: dict[str, Any]) -> None:
        if self.campaign_trends is None:
            return
        containers: list[list[dict[str, Any]]] = [
            sections.get("urgent") or [],
            sections.get("defending") or [],
        ]
        for block in (sections.get("attacking") or {}).values():
            if isinstance(block, dict):
                containers.extend([block.get("main") or [], block.get("low") or []])
        rows = [row for container in containers for row in container]
        enriched = self.campaign_trends.attach(rows)
        by_identity = {str(row.get("identity")): row for row in enriched}
        for container in containers:
            for index, row in enumerate(container):
                replacement = by_identity.get(str(row.get("identity")))
                if replacement is not None:
                    container[index] = replacement

    async def get_dashboard_image(self, *, realtime: bool = False) -> list[Path]:
        state = await self._build_war_state(realtime=realtime)
        if not state.campaigns and not state.planets:
            return []
        sections = state.dashboard_sections(
            low_impact_threshold=self.dashboard_low_impact_threshold
        )
        report_rows: list[dict[str, Any]] = [
            row
            for key in ("urgent", "defending")
            for row in sections.get(key) or []
            if isinstance(row, dict)
        ]
        for block in (sections.get("attacking") or {}).values():
            if not isinstance(block, dict):
                continue
            for key in ("main", "low"):
                report_rows.extend(
                    row for row in block.get(key) or [] if isinstance(row, dict)
                )
        await self._localize_planet_rows(report_rows)
        self._attach_trends_to_sections(sections)
        sections["dss"] = await self._prepare_dss_station(self._station_with_planet(state))
        dss_planet = sections["dss"].get("planet") if isinstance(sections.get("dss"), dict) else None
        sections["dss_planet"] = dss_planet if isinstance(dss_planet, dict) else {}
        if self.dashboard_renderer is not None:
            p = await asyncio.to_thread(
                self.dashboard_renderer.render,
                sections,
                subtitle=self._subtitle(),
            )
            return [p] if p else []
        card = build_dashboard_card(
            sections,
            subtitle=self._subtitle(),
            low_impact_threshold=self.dashboard_low_impact_threshold,
        )
        p = await self._render_card(card)
        return [p] if p else []

    async def get_warfront_message(self, faction: str) -> str:
        faction = (faction or "").strip()
        if not faction:
            return (
                "❌ 用法: /warfront <阵营>\n"
                "可选: Terminids/Automaton/Illuminate（终结族/机器人/光能者）"
            )
        norm = normalize_faction(faction)
        if norm not in {"Terminids", "Automaton", "Illuminate"}:
            return (
                "❌ 未知阵营。可选: Terminids / Automaton / Illuminate\n"
                "中文: 终结族 / 机器人 / 光能者"
            )
        state = await self._build_war_state()
        if not state.campaigns and not state.planets:
            return "❌ 暂时无法获取战线数据，请稍后重试。"
        campaigns = state.campaigns_for_faction(norm)
        return format_warfront_message(norm, campaigns)

    async def get_warfront_image(self, faction: str) -> tuple[str, list[Path]]:
        faction = (faction or "").strip()
        if not faction:
            return (
                "❌ 用法: /warfront <阵营>\n"
                "可选: Terminids/Automaton/Illuminate（终结族/机器人/光能者）",
                [],
            )
        norm = normalize_faction(faction)
        if norm not in {"Terminids", "Automaton", "Illuminate"}:
            return (
                "❌ 未知阵营。可选: Terminids / Automaton / Illuminate\n中文: 终结族 / 机器人 / 光能者",
                [],
            )
        state = await self._build_war_state()
        if not state.campaigns and not state.planets:
            return "❌ 暂时无法获取战线数据，请稍后重试。", []
        rows = state.warfront_rows(norm)
        if self.campaign_trends is not None:
            rows = self.campaign_trends.attach(rows)
        await self._localize_planet_rows(rows)
        if self.warfront_renderer is not None:
            defense_count = sum(1 for row in rows if row.get("is_defense"))
            faction_label = {
                "Terminids": "终结族",
                "Automaton": "机器人",
                "Illuminate": "光能者",
                "Humans": "超级地球",
            }.get(norm, norm)
            p = await asyncio.to_thread(
                self.warfront_renderer.render,
                rows,
                faction=norm,
                title=f"{faction_label}战线",
                subtitle=f"玩家数与解放进度各 Top 10 · {self._subtitle()}",
                defense_summary=(
                    f"当前有 {defense_count} 条防御战线；结果按最近采样净进度估算"
                    if defense_count
                    else None
                ),
            )
            return "", [p] if p else []
        campaigns = state.campaigns_for_faction(norm)
        card = build_warfront_card(norm, campaigns)
        p = await self._render_card(card)
        return "", [p] if p else []

    async def get_planet_message(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "❌ 用法: /planet <星球名称或编号>\n例如: /planet Heeth  或  /planet 112"
        state = await self._build_war_state()
        if not state.planets:
            return "❌ 暂时无法获取星球数据，请稍后重试。"
        matches = state.find_planets(query, limit=8)
        if not matches:
            return format_planet_candidates(query, [])
        if len(matches) == 1:
            return format_planet_message(await self._prepare_planet(matches[0]))
        # 精确名唯一
        ql = query.lower()
        exact = [
            p
            for p in matches
            if str(p.get("name") or "").lower() == ql
            or str(planet_index_safe(p)) == query
        ]
        if len(exact) == 1:
            return format_planet_message(await self._prepare_planet(exact[0]))
        candidates = copy.deepcopy(matches)
        await self._localize_planet_rows(candidates, include_english=False)
        return format_planet_candidates(query, candidates)

    async def _prepare_planet(self, planet: dict[str, Any]) -> dict[str, Any]:
        view = copy.deepcopy(planet)
        await self._localize_planet_rows([view], include_english=False)
        for field in ("sector", "biomeName"):
            text = view.get(field)
            if isinstance(text, str) and len(text.strip()) >= 3:
                view[field] = await self._translate_cached_text(
                    "planet_detail", field, text
                )
        biome = view.get("biome")
        if isinstance(biome, dict):
            for field in ("name", "description"):
                text = biome.get(field)
                if isinstance(text, str) and len(text.strip()) >= 3:
                    biome[field] = await self._translate_cached_text(
                        "planet_biome", field, text
                    )
        hazards = view.get("hazards")
        if isinstance(hazards, list):
            for index, hazard in enumerate(hazards[:8]):
                if not isinstance(hazard, dict):
                    continue
                for field in ("name", "description"):
                    text = hazard.get(field)
                    if isinstance(text, str) and len(text.strip()) >= 3:
                        hazard[field] = await self._translate_cached_text(
                            "planet_hazard", f"{index}:{field}", text
                        )
        return view

    async def resolve_companion_planet(
        self, query: str
    ) -> tuple[str, str | None, str | None]:
        """Resolve user input to a canonical planet name and Companion slug."""
        query = (query or "").strip()
        if not query:
            return "❌ 用法: /planet <星球名称、编号、slug 或 Companion 链接>", None, None

        # 用户可能直接输入带下划线的 slug（如 super_earth），统一按空格匹配
        normalized = query.replace("_", " ")
        query_folded = normalized.casefold()
        for source_name in known_planet_names():
            translated = lookup_planet_name(source_name)
            if translated and translated.casefold() == query_folded:
                normalized = source_name
                break

        state = await self._build_war_state()
        if not state.planets:
            return "❌ 暂时无法获取星球数据，请稍后重试。", None, None
        matches = state.find_planets(normalized, limit=8)
        if not matches:
            return format_planet_candidates(query, []), None, None

        selected: dict[str, Any] | None = None
        if len(matches) == 1:
            selected = matches[0]
        else:
            exact_name = normalized.casefold()
            exact = [
                planet
                for planet in matches
                if planet_name(planet).casefold() == exact_name
                or str(planet_index(planet)) == normalized
            ]
            if len(exact) == 1:
                selected = exact[0]
        if selected is None:
            return format_planet_candidates(query, matches), None, None

        canonical_name = planet_name(selected).strip()
        if not canonical_name:
            return "❌ 星球数据缺少名称，无法打开 Companion 页面。", None, None
        slug = re.sub(r"\s+", "_", canonical_name.casefold())
        # 2026-08 网站改版：详情路由要求 `_{(index)}` 编号后缀，缺后缀会被
        # 网站弹回首页。index 取战争数据中的星球编号。
        index = planet_index_safe(selected)
        if index is None:
            return "❌ 星球数据缺少编号，无法打开新版 Companion 星球页。", None, None
        slug = f"{slug}_({index})"
        return "", canonical_name, slug

    async def get_planet_image(self, query: str) -> tuple[str, list[Path]]:
        query = (query or "").strip()
        if not query:
            return "❌ 用法: /planet <星球名称或编号>", []
        state = await self._build_war_state()
        if not state.planets:
            return "❌ 暂时无法获取星球数据，请稍后重试。", []
        matches = state.find_planets(query, limit=8)
        if not matches:
            card = build_planet_candidates_card(query, [])
            p = await self._render_card(card)
            return "", [p] if p else []
        if len(matches) == 1:
            card = build_planet_card(await self._prepare_planet(matches[0]))
            p = await self._render_card(card)
            return "", [p] if p else []
        ql = query.lower()
        exact = [
            p
            for p in matches
            if str(p.get("name") or "").lower() == ql
            or str(planet_index_safe(p)) == query
        ]
        if len(exact) == 1:
            card = build_planet_card(await self._prepare_planet(exact[0]))
            p = await self._render_card(card)
            return "", [p] if p else []
        candidates = copy.deepcopy(matches)
        await self._localize_planet_rows(candidates, include_english=False)
        card = build_planet_candidates_card(query, candidates)
        p = await self._render_card(card)
        return "", [p] if p else []

    async def get_companion_planet_image(
        self, slug: str
    ) -> tuple[str, list[Path]]:
        query = str(slug or "").strip().replace("_", " ")
        return await self.get_planet_image(query)

    async def _prepare_global_events(
        self, events: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for i, source in enumerate(events[:limit]):
            if not isinstance(source, dict):
                continue
            event = copy.deepcopy(source)
            item_id = str(event.get("eventId") or event.get("id32") or i)
            for field, display_field in (
                ("title", "display_title"),
                ("message", "display_message"),
            ):
                text = event.get(field)
                if isinstance(text, str):
                    cleaned = clean_game_text(text)
                    event[display_field] = await self._translate_cached_text(
                        "global_event", f"{item_id}:{field}", cleaned
                    )
                    if (
                        not event[display_field]
                        and self.drop_untranslated_content
                        and _has_translatable_english(cleaned)
                    ):
                        event[field] = ""
            prepared.append(event)
        return prepared

    async def get_global_events_message(self) -> str:
        state = await self._build_war_state()
        events = await self._prepare_global_events(state.global_events(), 5)
        for event in events:
            event["title"] = event.get("display_title") or event.get("title")
            event["message"] = event.get("display_message") or event.get("message")
        return format_global_events_message(events)

    async def get_global_events_image(self) -> list[Path]:
        state = await self._build_war_state()
        events = await self._prepare_global_events(state.global_events(), 4)
        war_status = await self.cache.get_or_refresh(self.CACHE_WAR_STATUS)
        war_time = war_status.get("time") if isinstance(war_status, dict) else None
        if self.global_event_renderer is not None:
            paths: list[Path] = []
            for event in events:
                translated = {
                    "display_title": event.get("display_title"),
                    "display_message": event.get("display_message"),
                }
                p = await asyncio.to_thread(
                    self.global_event_renderer.render,
                    event,
                    war_time=war_time,
                    translated=translated,
                )
                if p:
                    paths.append(p)
            return paths
        legacy = []
        for event in events:
            item = dict(event)
            item["title"] = item.get("display_title") or item.get("title")
            item["message"] = item.get("display_message") or item.get("message")
            legacy.append(item)
        card = build_global_events_card(legacy)
        p = await self._render_card(card)
        return [p] if p else []

    async def get_map_result(self) -> tuple[str, Path | None]:
        """返回 (说明文字, 图片路径或 None)。"""
        if not self.enable_map:
            return "❌ 地图功能已在配置中关闭（enable_map=false）。", None
        state = await self._build_war_state()
        if not state.planets:
            return "❌ 暂时无法获取星球数据，无法生成地图。", None
        points = state.map_points()
        displayed_points = sorted(
            [
                point
                for point in points
                if point.get("active")
                or point.get("dss")
                or point.get("defense")
                or point.get("event")
            ],
            key=lambda point: (
                bool(point.get("dss")),
                bool(point.get("defense")),
                bool(point.get("event")),
                int(point.get("players") or 0),
            ),
            reverse=True,
        )[:15]
        await self._localize_planet_rows(displayed_points, include_english=False)
        caption = format_map_caption(
            active_campaigns=len(state.campaigns),
            planets=len(state.planets),
        )
        if self.map_renderer is None or not self.map_renderer.available:
            # 文本兜底：活跃战役 Top 列表
            lines = [caption, "（Pillow 未安装，以下为文本战线摘要）"]
            for c in state.campaigns[:15]:
                p = c.get("planet") if isinstance(c.get("planet"), dict) else {}
                name = p.get("name") or "?"
                players = (p.get("statistics") or {}).get("playerCount", 0)
                lines.append(f"▎{name}  在线:{players}")
            return "\n".join(lines), None
        from datetime import datetime

        subtitle = datetime.now().strftime("更新 %Y-%m-%d %H:%M")
        path = await asyncio.to_thread(
            self.map_renderer.render, points, subtitle=subtitle
        )
        if path is None:
            return caption + "\n❌ 地图绘制失败，请稍后重试。", None
        return caption, path


def planet_index_safe(planet: dict[str, Any]) -> int | None:
    from .core.war_state import planet_index

    return planet_index(planet)
