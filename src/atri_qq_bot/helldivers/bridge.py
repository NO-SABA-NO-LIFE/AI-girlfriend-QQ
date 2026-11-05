"""Helldivers → 亚托莉适配桥接器。

职责
----
1. 装配：按原 ``main.py`` 的 ``initialize`` 逻辑组装 HD2Client、缓存、翻译、
   各 Pillow 渲染器与 ``HelldiversService``（已移除 AstrBot 专有的 Star/filter、
   Companion Playwright 截图与 B站监听，这些留待后续阶段）。
2. 路由：识别以 ``/`` 开头的 Helldivers 指令，命中后调用对应 service 方法。
3. 转换：把 service 返回的图片路径 / 文本统一转成亚托莉 ``OutgoingMessage``，
   由消息管线既有的 ``send_reply`` 发出，无需自己实现 OneBot 发送。

边界：本类只负责"识别 + 取数据 + 转消息对象"，不直接操作 WebSocket。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..message_plan import OutgoingMessage
from ._logging import logger
from .clients.api_client import HD2Client
from .clients.arsenal_client import ArsenalClient
from .clients.bilibili_monitor import (
    BilibiliAuthenticationError,
    BilibiliMonitor,
    BilibiliPost,
    BilibiliRiskControlError,
    GALAXY_NEWS_UID,
    LEGACY_GALAXY_NEWS_UID,
)
from .clients.companion_news_monitor import CompanionNewsMonitor, news_fingerprint
from .clients.companion_screenshot import (
    CompanionConfigurationError,
    CompanionPlaywrightClient,
    CompanionScreenshotClient,
    CompanionTarget,
    CompanionTargetError,
    companion_dss_target,
    companion_homepage_target,
    companion_latest_news_target,
    parse_companion_planet_target,
)
from .clients.remote_image_cache import RemoteImageCache
from .clients.translator import TranslationCache, TranslationService
from .clients.wiki_client import WikiCache, WikiClient
from .config import HelldiversConfig
from .core.campaign_trends import CampaignTrendStore
from .core.cache import CacheService
from .core.diagnostics import (
    HEALTH,
    build_ping_report,
    humanize_age,
    humanize_uptime,
)
from .core.plugin_info import PLUGIN_VERSION, USER_AGENT
from .help_text import HDHELP_TEXT, HELP_TEXT
from .renderers.awaiting_orders_renderer import AwaitingOrdersRenderer
from .renderers.card_renderer import CardRenderer
from .renderers.dashboard_renderer import DashboardRenderer
from .renderers.dispatch_renderer import DispatchRenderer
from .renderers.dss_renderer import DSSRenderer
from .renderers.global_event_renderer import GlobalEventRenderer
from .renderers.major_order_renderer import MajorOrderRenderer
from .renderers.map_renderer import MapRenderer
from .renderers.stats_renderer import StatsRenderer
from .renderers.steam_renderer import SteamRenderer
from .renderers.visual_assets import configure_asset_manifest
from .renderers.warfront_renderer import WarfrontRenderer
from .services import HelldiversService

# 命令名（小写）→ 处理方法名。别名与原 main.py 的 @filter.command 对齐。
_COMMANDS: dict[str, str] = {
    "hd2": "_cmd_help",
    "helldivers": "_cmd_help",
    "hd2help": "_cmd_hdhelp",
    "hdhelp": "_cmd_hdhelp",
    "dashboard": "_cmd_dashboard",
    "总览": "_cmd_dashboard",
    "战况总览": "_cmd_dashboard",
    "hd2stats": "_cmd_stats",
    "order": "_cmd_order",
    "major_order": "_cmd_order",
    "最高命令": "_cmd_order",
    "map": "_cmd_map",
    "地图": "_cmd_map",
    "global_events": "_cmd_global_events",
    "全球事件": "_cmd_global_events",
    "events": "_cmd_global_events",
    "warfront": "_cmd_warfront",
    "战线": "_cmd_warfront",
    "steam": "_cmd_steam",
    "wiki": "_cmd_wiki",
    "维基": "_cmd_wiki",
    "百科": "_cmd_wiki",
    "hd2data": "_cmd_arsenal",
    "军需簿": "_cmd_arsenal",
    "数据库": "_cmd_arsenal",
    "装备": "_cmd_arsenal",
    "武器库": "_cmd_arsenal",
    "dss": "_cmd_dss",
    "空间站": "_cmd_dss",
    "民主空间站": "_cmd_dss",
    "planet": "_cmd_planet",
    "星球": "_cmd_planet",
    "hd2news": "_cmd_news",
    "hd2news订阅": "_cmd_news_sub",
    "news_subscribe": "_cmd_news_sub",
    "hd2news退订": "_cmd_news_unsub",
    "news_unsubscribe": "_cmd_news_unsub",
    "companion": "_cmd_companion",
    "首页": "_cmd_companion",
    "companion_home": "_cmd_companion",
    "hd2refresh": "_cmd_refresh",
    "刷新缓存": "_cmd_refresh",
    "hd2clear": "_cmd_refresh",
    "银河快报": "_cmd_galaxy_news",
    "银河快报推送": "_cmd_galaxy_news",
    "galaxy_news": "_cmd_galaxy_news",
    "银河快报订阅": "_cmd_galaxy_news_sub",
    "galaxy_news_subscribe": "_cmd_galaxy_news_sub",
    "银河快报退订": "_cmd_galaxy_news_unsub",
    "galaxy_news_unsubscribe": "_cmd_galaxy_news_unsub",
    "hd2ping": "_cmd_ping",
    "诊断": "_cmd_ping",
    "自检": "_cmd_ping",
}


def _planet_search_query(value: str) -> str:
    """planet 目标 value 可能带 `_(index)` 编号后缀，文本查询用纯星球名。"""
    name = value
    if name.endswith(")") and "_(" in name:
        name = name.rsplit("_(", 1)[0]
    return name.replace("_", " ")


# 主动推送回调：由 OneBot 侧注入，负责把消息发到指定会话，返回送达数。
# 第一个参数是会话标识（group:<群号> / private:<QQ号>），第二个是待发消息。
PushSender = Callable[[str, "list[OutgoingMessage]"], Awaitable[int]]


class HelldiversBridge:
    """亚托莉专属 Helldivers 适配桥接器。"""

    def __init__(self, config: HelldiversConfig) -> None:
        self.config = config
        self.service: HelldiversService | None = None
        self._started = False
        # Companion 截图与新闻订阅
        self.companion_backend = "playwright"
        self.companion_playwright: CompanionPlaywrightClient | None = None
        self.companion_screenshots: CompanionScreenshotClient | None = None
        self.companion_news_monitor: CompanionNewsMonitor | None = None
        self._companion_warmup_task: asyncio.Task[None] | None = None
        # B站银河快报
        self.bilibili_monitor: BilibiliMonitor | None = None
        self.bilibili_auto_enabled = False
        # 主动推送回调（框架注入；未注入时无法主动推送）
        self._push_sender: PushSender | None = None

    def set_push_sender(self, sender: PushSender | None) -> None:
        """注入主动推送能力，保持 helldivers 包不反向依赖 onebot。"""
        self._push_sender = sender

    @staticmethod
    def parse_session_key(session_key: str) -> tuple[str, str]:
        """把 ``group:123`` / ``private:456`` 解析成 (类型, 目标号)。"""
        kind, _, target = str(session_key or "").partition(":")
        return kind.strip(), target.strip()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """装配全部组件并启动后台缓存/轮换。幂等：重复调用不会重复装配。"""
        if self._started:
            return
        HEALTH.mark_boot()
        cfg = self.config
        data_dir = cfg.data_dir

        # ---- 装配 Companion 截图客户端（Playwright / Browserless）----
        companion_enabled = bool(cfg.get("enable_companion_screenshots", True))
        companion_backend = str(
            cfg.get("companion_render_backend", "playwright") or "playwright"
        ).strip().casefold()
        if companion_backend not in {"local", "playwright", "browserless"}:
            logger.warning(
                "[HD2] 无效的 companion_render_backend=%s，改用 playwright",
                companion_backend,
            )
            companion_backend = "playwright"
        self.companion_backend = companion_backend

        if companion_enabled and companion_backend == "playwright":
            try:
                self.companion_playwright = CompanionPlaywrightClient(
                    data_dir / "companion_screenshots",
                    timeout=cfg.get("companion_screenshot_timeout", 60),
                    wait_ms=cfg.get("companion_screenshot_wait_ms", 5000),
                    cache_ttl=cfg.get("companion_screenshot_cache_ttl", 300),
                    max_bytes=cfg.get(
                        "companion_screenshot_max_bytes", 12 * 1024 * 1024
                    ),
                    max_pixels=cfg.get(
                        "companion_screenshot_max_pixels", 20_000_000
                    ),
                    page_load_timeout=cfg.get("companion_page_load_timeout", 20.0),
                    translation_timeout=cfg.get(
                        "companion_translation_timeout", 60.0
                    ),
                    show_inactive=cfg.get("companion_show_inactive", False),
                )
                # 这里启动 Chromium；未安装内核时会抛配置异常并被降级为 None。
                await self.companion_playwright.start()
            except (CompanionConfigurationError, TypeError, ValueError) as e:
                logger.warning("[HD2] 本地 Playwright 不可用：%s", e)
                self.companion_playwright = None
        elif companion_enabled and companion_backend == "browserless":
            try:
                self.companion_screenshots = CompanionScreenshotClient(
                    data_dir / "companion_screenshots",
                    endpoint_url=cfg.get("companion_browserless_url", ""),
                    token=cfg.get("companion_browserless_token", ""),
                    proxy_url=cfg.get("companion_browserless_proxy_url", ""),
                    timeout=cfg.get("companion_screenshot_timeout", 60),
                    wait_ms=cfg.get("companion_screenshot_wait_ms", 5000),
                    cache_ttl=cfg.get("companion_screenshot_cache_ttl", 300),
                    max_bytes=cfg.get(
                        "companion_screenshot_max_bytes", 12 * 1024 * 1024
                    ),
                    max_pixels=cfg.get(
                        "companion_screenshot_max_pixels", 20_000_000
                    ),
                )
            except (CompanionConfigurationError, TypeError, ValueError) as e:
                logger.warning("[HD2] Browserless 截图不可用：%s", e)
                self.companion_screenshots = None

        client = HD2Client(
            base_url=cfg.get("api_base", "https://api.helldivers2.dev"),
            war_id=cfg.get("war_id", 801),
            client_name=cfg.get("api_client_name", "atri_helldivers"),
            contact=cfg.get("api_contact", ""),
            user_agent=cfg.get("api_user_agent", USER_AGENT),
            timeout=cfg.get("api_timeout", 20),
            proxy_url=cfg.get("proxy_url", ""),
            retry_base_delay=cfg.get("retry_base_delay", 3.0),
            retry_max_delay=cfg.get("retry_max_delay", 15.0),
            retry_max_attempts=cfg.get("retry_max_attempts", 2),
            min_request_interval=cfg.get("min_request_interval", 3.0),
            steam_appid=cfg.get("steam_appid", 553850),
            steam_news_count=cfg.get("steam_news_count", 10),
        )

        cache = CacheService(
            data_dir,
            update_interval=cfg.get("cache_update_interval", 300),
            request_delay=cfg.get("request_delay", 6),
            enable_background=cfg.get("enable_background_refresh", True),
        )

        enable_translation = cfg.get("enable_translation", True)
        translator = TranslationService(
            api_url=cfg.get(
                "translation_api_url",
                "https://uapis.cn/api/v1/ai/translate",
            ),
            timeout=cfg.get("translation_timeout", 20),
            target_lang=cfg.get("translation_target_lang", "zh"),
            user_agent=cfg.get("api_user_agent", USER_AGENT),
            proxy_url=cfg.get("proxy_url", ""),
            enabled=enable_translation,
            mode=cfg.get("translation_mode", "auto"),
            api_key=cfg.get("translation_api_key", ""),
            model=cfg.get("translation_model", "gpt-4o-mini"),
            stream=cfg.get("translation_stream", False),
            fallback_ttl=cfg.get("translation_retry_fallback_seconds", 180),
            error_cooldown=cfg.get("translation_error_cooldown_seconds", 120),
            max_tokens=cfg.get("translation_max_tokens", 1200),
            min_interval=cfg.get("translation_min_interval_seconds", 0),
        )
        logger.info("[HD2] translation provider: %s", translator.diagnostic())

        translation_cache = TranslationCache(
            data_dir,
            provider_fingerprint=translator.provider_fingerprint(),
            fallback_ttl=cfg.get("translation_retry_fallback_seconds", 180),
        )

        enable_wiki = cfg.get("enable_wiki", True)
        wiki_client: WikiClient | None = None
        if enable_wiki:
            wiki_cache = WikiCache(
                data_dir,
                page_ttl=cfg.get("wiki_cache_ttl", 21600),
                search_ttl=cfg.get("wiki_search_cache_ttl", 1800),
            )
            wiki_client = WikiClient(
                base_url=cfg.get("wiki_base", "https://helldivers.wiki.gg"),
                api_path=cfg.get("wiki_api_path", "/api.php"),
                user_agent=cfg.get("api_user_agent", USER_AGENT),
                timeout=cfg.get("api_timeout", 30),
                proxy_url=cfg.get("proxy_url", ""),
                search_limit=cfg.get("wiki_search_limit", 5),
                extract_chars=cfg.get("wiki_extract_chars", 900),
                full_extract_chars=cfg.get("wiki_full_extract_chars", 3000),
                prefer_hd2=cfg.get("wiki_prefer_hd2", True),
                retry_max_attempts=cfg.get("retry_max_attempts", 3),
                retry_base_delay=cfg.get("retry_base_delay", 2.0),
                cache=wiki_cache,
            )

        configure_asset_manifest(cfg.get("asset_manifest_path", "") or None)

        map_renderer = MapRenderer(
            data_dir / "maps",
            cache_ttl=cfg.get("map_cache_ttl", 600),
            label_top_n=cfg.get("map_label_top_n", 15),
            visual_mode=cfg.get("map_visual_mode", "uploaded_reference"),
        )
        card_renderer = CardRenderer(data_dir / "cards")
        dashboard_renderer = DashboardRenderer(data_dir / "dashboard")
        stats_renderer = StatsRenderer(data_dir / "stats")
        dss_renderer = DSSRenderer(data_dir / "dss")
        major_order_renderer = MajorOrderRenderer(data_dir / "orders")
        awaiting_orders_renderer = AwaitingOrdersRenderer(data_dir / "cards")
        dispatch_renderer = DispatchRenderer(data_dir / "dispatches")
        warfront_renderer = WarfrontRenderer(data_dir / "warfront")
        global_event_renderer = GlobalEventRenderer(data_dir / "global_events")
        steam_renderer = SteamRenderer(data_dir / "steam")
        remote_image_cache = RemoteImageCache(
            data_dir / "remote_images",
            max_bytes=cfg.get("remote_image_max_bytes", 8388608),
            max_pixels=cfg.get("remote_image_max_pixels", 16000000),
            max_cache_bytes=cfg.get("image_cache_max_mb", 150) * 1024 * 1024,
            user_agent=cfg.get("api_user_agent", USER_AGENT),
            proxy_url=cfg.get("proxy_url", ""),
        )
        campaign_trends = CampaignTrendStore(
            data_dir / "campaign_history.json",
            window_minutes=cfg.get("campaign_rate_window_minutes", 20),
            prune_hours=cfg.get("campaign_history_hours", 2),
        )

        self.service = HelldiversService(
            client,
            cache,
            translator,
            translation_cache,
            steam_max_content_length=cfg.get("steam_max_content_length", 2000),
            steam_balancing_limit=cfg.get("steam_balancing_limit", 800),
            steam_fixes_limit=cfg.get("steam_fixes_limit", 600),
            steam_issues_limit=cfg.get("steam_issues_limit", 300),
            steam_max_sections=cfg.get("steam_max_sections", 3),
            steam_image_enabled=cfg.get("steam_image_enabled", True),
            rotation_dispatches=cfg.get("rotation_dispatches", 300),
            rotation_orders=cfg.get("rotation_orders", 600),
            rotation_steam=cfg.get("rotation_steam", 900),
            enable_translation=enable_translation,
            wiki_client=wiki_client,
            enable_wiki=enable_wiki,
            translate_wiki=cfg.get("translate_wiki", True),
            map_renderer=map_renderer,
            enable_map=cfg.get("enable_map", True),
            card_renderer=card_renderer,
            dashboard_renderer=dashboard_renderer,
            stats_renderer=stats_renderer,
            dss_renderer=dss_renderer,
            major_order_renderer=major_order_renderer,
            awaiting_orders_renderer=awaiting_orders_renderer,
            dispatch_renderer=dispatch_renderer,
            warfront_renderer=warfront_renderer,
            global_event_renderer=global_event_renderer,
            steam_renderer=steam_renderer,
            remote_image_cache=remote_image_cache,
            campaign_trends=campaign_trends,
            dashboard_low_impact_threshold=cfg.get(
                "dashboard_low_impact_threshold", 800
            ),
            drop_untranslated_content=cfg.get("drop_untranslated_content", True),
            arsenal_client=(
                ArsenalClient(
                    data_dir / "arsenal",
                    proxy_url=cfg.get("proxy_url", ""),
                    timeout=cfg.get("api_timeout", 30),
                    user_agent=cfg.get("api_user_agent", USER_AGENT),
                )
                if cfg.get("enable_arsenal", True)
                else None
            ),
        )

        await self.service.start()

        # 给 Playwright 截图客户端挂上翻译回调，截图前先把页面片段译成中文。
        if self.companion_playwright is not None and enable_translation:
            self.companion_playwright.set_translate_callback(
                self.service.translate_companion_fragments,
                cache_namespace=translation_cache.provider_fingerprint,
            )

        # ---- 装配 B站银河快报监听 ----
        configured_bilibili_uid = str(
            cfg.get("bilibili_uid", GALAXY_NEWS_UID) or GALAXY_NEWS_UID
        ).strip()
        if configured_bilibili_uid == LEGACY_GALAXY_NEWS_UID:
            logger.warning(
                "[HD2] 自动修正银河快报旧 UID：%s -> %s",
                LEGACY_GALAXY_NEWS_UID, GALAXY_NEWS_UID,
            )
            configured_bilibili_uid = GALAXY_NEWS_UID
        self.bilibili_monitor = BilibiliMonitor(
            data_dir / "bilibili_galaxy_news",
            uid=configured_bilibili_uid,
            keyword=cfg.get("bilibili_keyword", "银河快报"),
            interval=cfg.get("bilibili_poll_interval", 300),
            cookie=cfg.get("bilibili_cookie", ""),
            proxy_url=cfg.get("proxy_url", ""),
            timeout=cfg.get("api_timeout", 30),
            max_image_bytes=cfg.get("bilibili_image_max_bytes", 16 * 1024 * 1024),
            send_post=self._send_bilibili_post,
            log_info=logger.info,
            log_warning=logger.warning,
            image_cache=RemoteImageCache(
                data_dir / "bilibili_galaxy_news" / "secure_images_v3",
                allowed_hosts=frozenset(
                    {
                        "i0.hdslb.com",
                        "i1.hdslb.com",
                        "i2.hdslb.com",
                        "archive.biliimg.com",
                        "album.biliimg.com",
                    }
                ),
                max_bytes=cfg.get("bilibili_image_max_bytes", 16 * 1024 * 1024),
                max_pixels=cfg.get("bilibili_image_max_pixels", 64_000_000),
                max_cache_bytes=cfg.get("image_cache_max_mb", 150) * 1024 * 1024,
                user_agent=cfg.get("api_user_agent", USER_AGENT),
                proxy_url=cfg.get("proxy_url", ""),
                negative_ttl=60,
                request_headers={"Referer": "https://www.bilibili.com/"},
            ),
        )
        self.bilibili_auto_enabled = bool(
            cfg.get("enable_bilibili_galaxy_news", False)
        )
        if self.bilibili_auto_enabled:
            if not self.bilibili_monitor.has_login_cookie:
                logger.warning(
                    "[HD2] 银河快报后台监听未启动：bilibili_cookie 缺少 SESSDATA，"
                    "请填写完整 B站 Cookie 后重启。"
                )
            else:
                await self.bilibili_monitor.start()
                logger.info(
                    "[HD2] B站银河快报监听已启用：uid=%s keyword=%s",
                    self.bilibili_monitor.uid, self.bilibili_monitor.keyword,
                )

        # ---- Companion 新闻订阅监控 + 后台预热 ----
        if not str(cfg.get("api_contact", "") or "").strip():
            logger.warning(
                "[HD2] 建议在配置中填写 api_contact（联系邮箱），以遵守 Helldivers API 礼仪"
            )
        self.companion_news_monitor = CompanionNewsMonitor(
            data_dir / "companion_news"
        )
        self._start_companion_warmup()

        self._started = True
        logger.info(
            "[HD2] bridge started %s, data_dir=%s", PLUGIN_VERSION, data_dir
        )

    async def stop(self) -> None:
        """停止后台任务并落盘缓存。"""
        if not self._started:
            return
        # 取消 Companion 截图预热循环
        if self._companion_warmup_task is not None:
            self._companion_warmup_task.cancel()
            try:
                await self._companion_warmup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._companion_warmup_task = None
        # 停止 B站监听
        if self.bilibili_monitor is not None:
            try:
                await self.bilibili_monitor.stop()
            except Exception as e:
                logger.warning("[HD2] B站监听停止出错：%s", e)
            self.bilibili_monitor = None
        self.bilibili_auto_enabled = False
        # 关闭 Playwright 浏览器
        if self.companion_playwright is not None:
            try:
                await self.companion_playwright.close()
            except Exception as e:
                logger.warning("[HD2] Playwright 停止出错：%s", e)
        self.companion_playwright = None
        self.companion_screenshots = None
        if self.service is not None:
            await self.service.stop()
        self._started = False
        logger.info("[HD2] bridge stopped")

    # ------------------------------------------------------------------
    # 指令识别
    # ------------------------------------------------------------------

    def parse(self, plain_text: str) -> tuple[str, list[str]] | None:
        """把文本解析为 (命令, 参数)；不是 Helldivers 命令则返回 None。

        第一阶段统一要求以 ``/`` 开头，避免与亚托莉的日常对话误触发；
        命令名大小写不敏感，与原 AstrBot 插件行为一致。
        """
        text = (plain_text or "").strip()
        if not text.startswith("/"):
            return None
        text = text[1:].strip()
        parts = text.split()
        if not parts:
            return None
        head = parts[0].lower()
        if head not in _COMMANDS:
            return None
        return head, parts[1:]

    async def handle(
        self, plain_text: str, session_key: str = ""
    ) -> list[OutgoingMessage] | None:
        """处理一条文本；返回待发送消息列表，None 表示非 Helldivers 命令。

        session_key 为当前会话标识（group:xxx / private:xxx），订阅/退订类
        命令需要它来记录要推送到哪个会话；普通查询命令会忽略它。
        """
        parsed = self.parse(plain_text)
        if parsed is None:
            return None
        command, args = parsed
        if self.service is None:
            return [
                OutgoingMessage("text", "Helldivers 情报服务还在启动中，请稍后再试。")
            ]
        handler = getattr(self, _COMMANDS[command])
        try:
            return await handler(args, session_key)
        except Exception as exc:  # 兜底：任何异常都不应打断亚托莉主流程
            logger.exception("[HD2] 处理 /%s 失败: %s", command, exc)
            return [OutgoingMessage("text", "情报获取失败，请稍后重试。")]

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _images_or_text(
        paths: list[Path], fallback: str
    ) -> list[OutgoingMessage]:
        """把图片路径列表转成消息；为空时返回兜底文本。"""
        if paths:
            return [OutgoingMessage("image", str(p)) for p in paths]
        return [OutgoingMessage("text", fallback)]

    # ------------------------------------------------------------------
    # 帮助
    # ------------------------------------------------------------------

    async def _cmd_help(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        return [OutgoingMessage("text", HELP_TEXT)]

    async def _cmd_hdhelp(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        return [OutgoingMessage("text", HDHELP_TEXT)]

    # ------------------------------------------------------------------
    # 图片卡片类
    # ------------------------------------------------------------------

    async def _cmd_dashboard(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        paths = await self.service.get_dashboard_image(realtime=True)
        return self._images_or_text(paths, "获取战争总览失败，请稍后重试。")

    async def _cmd_stats(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        paths = await self.service.get_stats_images(realtime=True)
        return self._images_or_text(paths, "获取银河战争统计失败，请稍后重试。")

    async def _cmd_order(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        paths = await self.service.get_order_images()
        return self._images_or_text(paths, "获取最高命令失败，请稍后重试。")

    async def _cmd_global_events(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        paths = await self.service.get_global_events_image()
        return self._images_or_text(paths, "获取全球事件失败，请稍后重试。")

    async def _cmd_dss(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        # local 后端：直接用 Pillow 渲染 DSS 卡片。
        if self.companion_backend == "local":
            paths = await self.service.get_dss_image()
            return self._images_or_text(
                paths, "获取民主空间站信息失败，请稍后重试。"
            )
        # playwright / browserless：截图 DSS 网页并附中文情报文本。
        target = companion_dss_target()
        try:
            path = await self._capture_companion_page(target)
        except Exception as e:
            logger.exception("[HD2] /dss 网页截图失败：%s", e)
            stale = self._stale_fallback_path(target)
            if stale is not None:
                path = stale
            else:
                # 没有缓存截图时，回退 Pillow 卡片，保证仍能拿到 DSS 情报。
                paths = await self.service.get_dss_image()
                if paths:
                    return self._images_or_text(paths, "")
                return [
                    OutgoingMessage("text", "Companion DSS 截图失败，请稍后重试。")
                ]
        try:
            message = await self.service.get_dss_message()
        except Exception as e:
            logger.warning("[HD2] /dss 中文文本获取失败：%s", e)
            message = "民主空间站中文情报暂时不可用。"
        return [
            OutgoingMessage("text", message),
            OutgoingMessage("image", str(path)),
        ]

    async def _cmd_map(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        caption, path = await self.service.get_map_result()
        if path is None:
            return [OutgoingMessage("text", caption)]
        return [
            OutgoingMessage("text", caption),
            OutgoingMessage("image", str(path)),
        ]

    async def _cmd_warfront(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        faction = " ".join(args).strip()
        err, paths = await self.service.get_warfront_image(faction)
        if err:
            return [OutgoingMessage("text", err)]
        return self._images_or_text(paths, "获取战线信息失败，请稍后重试。")

    async def _cmd_planet(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        query = " ".join(args).strip()
        if not query:
            return [
                OutgoingMessage(
                    "text",
                    "用法: /planet <星球名称、编号、slug 或 Companion 链接>",
                )
            ]

        # ---- 解析要截图的 Companion 目标，以及文本回退查询名 ----
        try:
            if "://" in query or query.lstrip("#/").casefold().startswith(
                "hellpad/planets/"
            ):
                target = parse_companion_planet_target(query)
                if target.value.endswith(")"):
                    fallback_query = _planet_search_query(target.value)
                else:
                    # 旧格式链接缺编号后缀，用战争数据补全后再构建目标。
                    err, _, slug = await self.service.resolve_companion_planet(
                        _planet_search_query(target.value)
                    )
                    if err or not slug:
                        return [
                            OutgoingMessage("text", err or "无法解析该星球。")
                        ]
                    target = parse_companion_planet_target(slug)
                    fallback_query = _planet_search_query(target.value)
            else:
                err, canonical_name, slug = await self.service.resolve_companion_planet(
                    query
                )
                if err or not canonical_name or not slug:
                    if query.isdigit():
                        return [
                            OutgoingMessage("text", err or "无法解析该星球。")
                        ]
                    try:
                        target = parse_companion_planet_target(query)
                    except CompanionTargetError:
                        return [
                            OutgoingMessage("text", err or "无法解析该星球。")
                        ]
                    fallback_query = _planet_search_query(target.value)
                else:
                    target = parse_companion_planet_target(slug)
                    fallback_query = canonical_name
        except CompanionTargetError as e:
            return [OutgoingMessage("text", str(e))]
        except Exception as e:
            logger.exception("[HD2] /planet 解析失败：%s", e)
            return [OutgoingMessage("text", "解析星球失败，请稍后重试。")]

        # local 后端：Pillow 星球卡片。
        if self.companion_backend == "local":
            error, paths = await self.service.get_planet_image(fallback_query)
            if error:
                return [OutgoingMessage("text", error)]
            return self._images_or_text(
                paths, "获取星球数据卡片失败，请稍后重试。"
            )

        # playwright / browserless：截图星球网页并附中文文本。
        try:
            path = await self._capture_companion_page(target)
            try:
                message = await self.service.get_planet_message(fallback_query)
            except Exception as message_error:
                logger.warning("[HD2] /planet 文本获取失败：%s", message_error)
                message = ""
            messages: list[OutgoingMessage] = []
            if message:
                messages.append(OutgoingMessage("text", message))
            messages.append(OutgoingMessage("image", str(path)))
            return messages
        except Exception as e:
            logger.exception("[HD2] /planet 网页截图失败：%s", e)
            stale = self._stale_fallback_path(target)
            if stale is not None:
                return [
                    OutgoingMessage(
                        "text",
                        "Companion 星球页暂时加载失败，返回上次缓存截图。",
                    ),
                    OutgoingMessage("image", str(stale)),
                ]
            # 无缓存截图时回退 Pillow 星球卡片。
            error, paths = await self.service.get_planet_image(fallback_query)
            if not error and paths:
                return self._images_or_text(paths, "")
            return [
                OutgoingMessage("text", "Companion 星球截图失败，请稍后重试。")
            ]

    # ------------------------------------------------------------------
    # 外部资讯（含文本回退）
    # ------------------------------------------------------------------

    async def _cmd_steam(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        paths = await self.service.get_steam_images()
        if paths:
            return self._images_or_text(
                paths, "Steam 新闻图片生成失败，请稍后重试。"
            )
        message = await self.service.get_steam_message()
        return [OutgoingMessage("text", message)]

    async def _cmd_wiki(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        full = False
        if "-f" in args or "--full" in args:
            full = True
            args = [a for a in args if a not in ("-f", "--full")]
        query = " ".join(args).strip()
        message = await self.service.get_wiki_message(query, full=full)
        return [OutgoingMessage("text", message)]

    async def _cmd_arsenal(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        query = " ".join(args).strip()
        if not query:
            return [
                OutgoingMessage(
                    "text",
                    "用法: /hd2data <名称>（武器/战备/护甲/敌人）\n"
                    "例如: /hd2data 磁轨炮 · /hd2data 电喷 · /hd2data 泰坦",
                )
            ]
        result = await self.service.get_arsenal_image(query)
        if isinstance(result, str):
            return [OutgoingMessage("text", result)]
        return self._images_or_text(result, "军需簿查询失败。")

    # ------------------------------------------------------------------
    # 自诊断
    # ------------------------------------------------------------------

    async def _cmd_ping(self, args: list[str], session_key: str) -> list[OutgoingMessage]:
        svc = self.service
        now = time.time()
        client = svc.client

        info_lines = [
            f"运行 {humanize_uptime(HEALTH.uptime_seconds)} · 战争 ID {client.war_id}"
            f" · 主API {client.base_url} · 代理 "
            f"{'已配置' if client.proxy_url else '未配置'}",
            (
                f"翻译: {'开(' + str(svc.translator.mode) + ')' if svc.enable_translation else '关'}"
                f" · 军需簿: {'开' if svc.arsenal_client is not None else '关'}"
            ),
        ]

        ages: list[str] = []
        for name in (
            svc.CACHE_WAR_STATUS,
            svc.CACHE_ORDERS,
            svc.CACHE_DISPATCHES,
            svc.CACHE_STEAM,
            svc.CACHE_PLANETS,
            svc.CACHE_CAMPAIGNS,
            svc.CACHE_SPACE_STATIONS,
        ):
            entry = await svc.cache.get_entry(name)
            age = humanize_age(now - entry.updated_at) if entry else "无缓存"
            ages.append(f"{name} {age}")
        cache_lines = [" · ".join(ages[:4]), " · ".join(ages[4:])]

        # 第一阶段只展示实际启用的数据源，避免出现 Companion/B站等"暂无记录"噪声。
        expected = ("hd2_api", "steam", "wiki", "hd2tool", "translate")
        report = build_ping_report(
            HEALTH.snapshot(),
            title=f"HD2 自诊断 {PLUGIN_VERSION}",
            info_lines=info_lines,
            cache_lines=cache_lines,
            expected_sources=expected,
            now=now,
        )
        return [OutgoingMessage("text", report)]

    # ------------------------------------------------------------------
    # 主动推送（框架注入的 sender）
    # ------------------------------------------------------------------

    async def _push_messages(
        self, session_key: str, messages: list[OutgoingMessage]
    ) -> int:
        """把消息主动推送到指定会话，返回送达数（0 或 1）。

        真正的发送动作由 OneBot 侧注入的 _push_sender 完成，本方法只做
        会话标识校验和异常兜底，保持 helldivers 包不依赖具体框架。
        """
        kind, target = self.parse_session_key(session_key)
        if kind not in {"group", "private"} or not target:
            logger.warning("[HD2] 无效的会话标识：%s", session_key)
            return 0
        if self._push_sender is None:
            logger.warning("[HD2] 未注入主动推送能力，无法发送到 %s", session_key)
            return 0
        try:
            return await self._push_sender(session_key, messages)
        except Exception as exc:
            logger.warning("[HD2] 推送到 %s 失败：%s", session_key, exc)
            return 0

    # ------------------------------------------------------------------
    # Companion 截图辅助
    # ------------------------------------------------------------------

    async def _capture_companion_page(
        self, target: CompanionTarget, *, force: bool = False
    ) -> Path:
        """根据当前可用后端截图一个 Companion 页面；都不可用则抛配置异常。"""
        if self.companion_playwright is not None:
            return await self.companion_playwright.capture(target, force=force)
        if self.companion_screenshots is not None:
            return await self.companion_screenshots.capture(target, force=force)
        raise CompanionConfigurationError(
            "Companion 网页截图尚未启用，或 Chromium 内核未就绪"
        )

    def _served_companion_stale(self, kind: str) -> bool:
        if self.companion_playwright is None:
            return False
        return kind in self.companion_playwright.served_stale_kinds()

    def _stale_fallback_path(
        self, target: CompanionTarget
    ) -> Path | None:
        """返回该目标的本地缓存截图，没有则 None。"""
        if self.companion_playwright is not None:
            path = self.companion_playwright.cached_output_path(target)
            return path if path.is_file() else None
        if self.companion_screenshots is not None:
            path = self.companion_screenshots.output_dir / target.cache_name
            return path if path.is_file() else None
        return None

    # ------------------------------------------------------------------
    # Companion 截图命令
    # ------------------------------------------------------------------

    async def _cmd_news(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        """截图 Companion 最新新闻；失败时回退到 Pillow 基础新闻卡。"""
        target = companion_latest_news_target()
        try:
            path = await self._capture_companion_page(target)
            messages: list[OutgoingMessage] = []
            if self._served_companion_stale(target.kind):
                messages.append(
                    OutgoingMessage(
                        "text",
                        "翻译快照（内容稍旧），后台正在刷新，稍后重试可获取最新翻译。",
                    )
                )
            messages.append(OutgoingMessage("image", str(path)))
            return messages
        except Exception as e:
            logger.exception("[HD2] /hd2news 截图失败：%s", e)
            try:
                _, paths = await self.service.get_news_image(1)
            except Exception as fallback_error:
                logger.warning("[HD2] 新闻回退失败：%s", fallback_error)
                paths = []
            if paths:
                return [
                    OutgoingMessage(
                        "text",
                        "Companion 翻译暂时繁忙，已为你展示基础新闻卡片，"
                        "稍后重试可获取完整翻译截图。",
                    ),
                    OutgoingMessage("image", str(paths[0])),
                ]
            return [
                OutgoingMessage("text", "Companion 最新新闻暂时不可用，请稍后重试。")
            ]

    async def _cmd_companion(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        """截图 Companion 首页；失败时尽量返回缓存截图。"""
        target = companion_homepage_target()
        try:
            path = await self._capture_companion_page(target)
            return [OutgoingMessage("image", str(path))]
        except Exception as e:
            logger.exception("[HD2] /companion 截图失败：%s", e)
            stale = self._stale_fallback_path(target)
            if stale:
                return [
                    OutgoingMessage(
                        "text", "Companion 首页暂时加载失败，返回上次缓存截图。"
                    ),
                    OutgoingMessage("image", str(stale)),
                ]
            return [OutgoingMessage("text", "Companion 首页截图失败，请稍后重试。")]

    async def _cmd_refresh(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        """强制刷新全部 Companion 截图缓存（跳过 TTL 重新截图）。"""
        if self.companion_playwright is None and self.companion_screenshots is None:
            return [
                OutgoingMessage("text", "当前未启用 Companion 截图，无需刷新。")
            ]
        targets = [
            companion_latest_news_target(),
            companion_dss_target(),
            companion_homepage_target(),
        ]
        results: list[str] = []
        for target in targets:
            try:
                await self._capture_companion_page(target, force=True)
                results.append(f"✅ {target.label}")
            except Exception as e:
                logger.warning("[HD2] 刷新 %s 失败：%s", target.kind, e)
                results.append(f"❌ {target.label}")
        return [
            OutgoingMessage("text", "Companion 缓存刷新完成：\n" + "\n".join(results))
        ]

    # ------------------------------------------------------------------
    # Companion 新闻订阅 / 退订
    # ------------------------------------------------------------------

    async def _cmd_news_sub(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        """在当前群订阅 Companion 新闻推送。"""
        monitor = self.companion_news_monitor
        if monitor is None:
            return [OutgoingMessage("text", "Companion 新闻推送尚未初始化。")]
        if self.companion_playwright is None:
            return [
                OutgoingMessage(
                    "text",
                    "需先启用 Companion Playwright 截图且 Chromium 可用，"
                    "才能订阅新闻推送。",
                )
            ]
        if not bool(self.config.get("companion_news_push_enabled", False)):
            return [
                OutgoingMessage(
                    "text",
                    "新闻推送总开关未启用，请在 .env 设置 HD2_NEWS_PUSH=true 后重启。",
                )
            ]
        if not bool(self.config.get("companion_warmup_enabled", True)):
            return [
                OutgoingMessage(
                    "text",
                    "Companion 后台预热未启用，新闻推送依赖预热检测变更，"
                    "请开启 HD2_COMPANION_WARMUP。",
                )
            ]
        self._start_companion_warmup()
        if self._companion_warmup_task is None or self._companion_warmup_task.done():
            return [OutgoingMessage("text", "新闻检测任务未运行，请检查日志后重启。")]
        kind, target = self.parse_session_key(session_key)
        if kind != "group" or not target:
            return [OutgoingMessage("text", "新闻订阅仅支持在群聊中使用。")]
        if monitor.subscribe(session_key):
            return [
                OutgoingMessage(
                    "text", "✅ 已订阅 Companion 新闻推送，有新新闻时自动同步到本群。"
                )
            ]
        return [OutgoingMessage("text", "本群已经订阅 Companion 新闻推送。")]

    async def _cmd_news_unsub(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        """取消当前群的 Companion 新闻推送。"""
        monitor = self.companion_news_monitor
        if monitor is None:
            return [OutgoingMessage("text", "Companion 新闻推送当前未启用。")]
        kind, target = self.parse_session_key(session_key)
        if kind == "group" and target and monitor.unsubscribe(session_key):
            return [OutgoingMessage("text", "✅ 已取消本群的 Companion 新闻推送。")]
        return [OutgoingMessage("text", "本群尚未订阅 Companion 新闻推送。")]

    # ------------------------------------------------------------------
    # Companion 后台预热 + 新闻变更检测
    # ------------------------------------------------------------------

    def _start_companion_warmup(self) -> None:
        """启动后台截图预热循环（仅 Playwright 后端可用）。"""
        if self._companion_warmup_task is not None and not self._companion_warmup_task.done():
            return
        if self.companion_playwright is None:
            return
        if not bool(self.config.get("companion_warmup_enabled", True)):
            return
        self._companion_warmup_task = asyncio.create_task(
            self._companion_warmup_loop(), name="hd2-companion-warmup"
        )
        logger.info("[HD2] Companion 截图后台预热已启动")

    def _companion_warmup_targets(self) -> list[CompanionTarget]:
        """根据配置构造要预热的页面目标列表。"""
        kinds_raw = str(
            self.config.get("companion_warmup_kinds", "news_latest,station")
        )
        kind_map = {
            "news_latest": companion_latest_news_target,
            "station": companion_dss_target,
            "homepage": companion_homepage_target,
        }
        targets: list[CompanionTarget] = []
        for token in kinds_raw.split(","):
            kind = token.strip().casefold()
            factory = kind_map.get(kind)
            if factory is not None and all(t.kind != kind for t in targets):
                targets.append(factory())
        if bool(self.config.get("companion_news_push_enabled", False)) and all(
            t.kind != "news_latest" for t in targets
        ):
            targets.insert(0, companion_latest_news_target())
        return targets

    async def _companion_warmup_loop(self) -> None:
        """周期性截图并翻译，同时驱动新闻变更检测与失败重试。"""
        interval = max(
            60, int(self.config.get("companion_warmup_interval", 300))
        )
        targets = self._companion_warmup_targets()
        if not targets:
            return
        await asyncio.sleep(15)  # 等其余组件先稳定
        while True:
            try:
                await self._deliver_pending_companion_news()
            except Exception as exc:
                logger.warning("[HD2] Companion 新闻重试失败：%s", exc)
            for target in targets:
                try:
                    push_enabled = bool(
                        self.config.get("companion_news_push_enabled", False)
                    )
                    force = (
                        target.kind == "news_latest"
                        and push_enabled
                        and self.companion_news_monitor is not None
                    )
                    path = await self.companion_playwright.capture(  # type: ignore[union-attr]
                        target, force=force
                    )
                    logger.info("[HD2] Companion 预热完成：%s", target.kind)
                    if target.kind == "news_latest":
                        await self._check_companion_news_change(path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log = (
                        logger.warning
                        if target.kind == "news_latest"
                        else logger.debug
                    )
                    log("[HD2] Companion 预热失败 %s：%s", target.kind, exc)
                await asyncio.sleep(5)  # 截图之间留间隔，避免突发
            await asyncio.sleep(interval)

    async def _check_companion_news_change(self, path: Path) -> None:
        """用文本指纹判断新闻是否变化，变化则排队推送给订阅群。"""
        monitor = self.companion_news_monitor
        client = self.companion_playwright
        if monitor is None or client is None:
            return
        texts = client.capture_texts("news_latest")
        if not texts:
            return
        fingerprint = news_fingerprint(texts)
        if not monitor.check_fingerprint(fingerprint):
            return
        if not monitor.sessions:
            logger.info("[HD2] Companion 新闻已更新，但无订阅群，跳过推送")
            monitor.commit_fingerprint(fingerprint)
            return
        if not bool(self.config.get("companion_news_push_enabled", False)):
            logger.info("[HD2] Companion 新闻已更新，但推送总开关未启用，跳过推送")
            monitor.commit_fingerprint(fingerprint)
            return
        # 翻译未完成时拿到的是旧缓存截图，先不推，等下一轮拿到新截图。
        if self._served_companion_stale("news_latest"):
            logger.info(
                "[HD2] Companion 新闻已更新，但截图为旧缓存，本轮跳过，下一轮重试"
            )
            return
        await monitor.enqueue(fingerprint, path)
        await self._deliver_pending_companion_news()

    async def _deliver_pending_companion_news(self) -> None:
        """重试投递已排队的新闻，按会话逐个确认送达。"""
        monitor = self.companion_news_monitor
        if monitor is None or not bool(
            self.config.get("companion_news_push_enabled", False)
        ):
            return
        monitor.prune_pending()
        for fingerprint, sessions in list(monitor.pending.items()):
            for session in list(sessions):
                if session not in sessions:
                    continue
                if session not in monitor.sessions:
                    monitor.mark_delivered(fingerprint, session)
                    continue
                try:
                    delivered = await self._push_companion_news(
                        monitor.pending_image(fingerprint), [session]
                    )
                except Exception as exc:
                    logger.warning("[HD2] Companion 新闻重试失败：%s", exc)
                    continue
                if delivered:
                    monitor.mark_delivered(fingerprint, session)
            monitor.finish_pending(fingerprint)

    async def _push_companion_news(
        self, path: Path, sessions: list[str]
    ) -> int:
        """把一条新闻截图推送到给定会话列表，返回送达数。"""
        if not path or not path.is_file():
            logger.warning("[HD2] Companion 新闻推送图片缺失：%s", path)
            return 0
        delivered = 0
        for session_key in sessions:
            messages = [
                OutgoingMessage("text", "📰 Companion 新闻更新"),
                OutgoingMessage("image", str(path)),
            ]
            delivered += await self._push_messages(session_key, messages)
        return delivered

    # ------------------------------------------------------------------
    # B站银河快报
    # ------------------------------------------------------------------

    async def _cmd_galaxy_news(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        """立即获取并发送最新一期 B站银河快报。"""
        monitor = self.bilibili_monitor
        if monitor is None:
            return [OutgoingMessage("text", "银河快报服务尚未初始化完成。")]
        try:
            posts = await monitor.fetch_posts()
            post = monitor.latest_matching(posts)
            if post is None:
                return [
                    OutgoingMessage(
                        "text",
                        f"最近的动态中没有找到关键字“{monitor.keyword}”。",
                    )
                ]
            paths = await monitor.download_images(post)
            messages = [
                OutgoingMessage("text", f"【{post.author}】\n{post.text}")
            ]
            for path in paths:
                messages.append(OutgoingMessage("image", str(path)))
            messages.append(OutgoingMessage("text", f"原动态：{post.url}"))
            return messages
        except (BilibiliAuthenticationError, BilibiliRiskControlError) as e:
            return [OutgoingMessage("text", str(e))]
        except Exception as e:
            logger.warning("[HD2] 手动获取银河快报失败：%s", e)
            return [OutgoingMessage("text", "获取银河快报失败，请查看日志。")]

    async def _cmd_galaxy_news_sub(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        """在当前群订阅 B站银河快报自动推送。"""
        if self.bilibili_monitor is None or not self.bilibili_auto_enabled:
            return [
                OutgoingMessage(
                    "text",
                    "银河快报监听未启用，请先在 .env 设置 HD2_BILIBILI_ENABLED=true 后重启。",
                )
            ]
        if not self.bilibili_monitor.has_login_cookie:
            return [
                OutgoingMessage(
                    "text",
                    "银河快报监听缺少有效 B站登录 Cookie（须含 SESSDATA），"
                    "请在 HD2_BILIBILI_COOKIE 填写后重启。",
                )
            ]
        kind, target = self.parse_session_key(session_key)
        if kind != "group" or not target:
            return [OutgoingMessage("text", "银河快报订阅仅支持在群聊中使用。")]
        if self.bilibili_monitor.subscribe(session_key):
            return [
                OutgoingMessage(
                    "text", "✅ 已订阅银河快报，新动态将自动同步到本群。"
                )
            ]
        return [OutgoingMessage("text", "本群已经订阅银河快报。")]

    async def _cmd_galaxy_news_unsub(
        self, args: list[str], session_key: str
    ) -> list[OutgoingMessage]:
        """取消当前群的 B站银河快报订阅。"""
        if self.bilibili_monitor is None:
            return [OutgoingMessage("text", "银河快报监听当前未启用。")]
        kind, target = self.parse_session_key(session_key)
        if kind == "group" and target and self.bilibili_monitor.unsubscribe(
            session_key
        ):
            return [OutgoingMessage("text", "✅ 已取消本群的银河快报订阅。")]
        return [OutgoingMessage("text", "本群尚未订阅银河快报。")]

    async def _send_bilibili_post(
        self,
        post: BilibiliPost,
        image_paths: list[Path],
        sessions: list[str],
    ) -> int:
        """BilibiliMonitor 的推送回调：把一条动态发给全部订阅群。"""
        if not sessions:
            logger.info(
                "[HD2] 匹配到 B站动态 %s，但无订阅群", post.dynamic_id
            )
            return 0
        delivered = 0
        for session_key in sessions:
            messages = [
                OutgoingMessage("text", f"【{post.author}】\n{post.text}")
            ]
            for path in image_paths:
                messages.append(OutgoingMessage("image", str(path)))
            messages.append(OutgoingMessage("text", f"原动态：{post.url}"))
            count = await self._push_messages(session_key, messages)
            if count:
                delivered += count
        if delivered == 0:
            raise RuntimeError(
                f"B站动态 {post.dynamic_id} 对所有订阅群推送失败"
            )
        return delivered
