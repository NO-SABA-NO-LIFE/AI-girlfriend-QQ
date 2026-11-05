"""Helldivers 移植模块配置。

设计要点
--------
- 配置通过亚托莉 ``.env`` 中的 ``HD2_*`` 环境变量承载（亚托莉启动时会把
  ``.env`` 加载进 ``os.environ``）；未设置的项使用与上游 AstrBot 插件一致的默认值。
- 对外暴露 :meth:`HelldiversConfig.get`，使装配代码与原 ``main.py`` 里的
  ``self._cfg(key, default)`` 一一对应，便于移植和后续同步上游。
- 数据目录由适配层显式传入（亚托莉 ``data/helldivers``），不依赖任何 AstrBot 路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.plugin_info import USER_AGENT


def _raw(name: str) -> str | None:
    """读取环境变量；空字符串与未设置同等对待。"""
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _coerce(raw: str | None, default: Any) -> Any:
    """按默认值的类型把环境变量字符串转换为对应 Python 类型。"""
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.lower() in {"1", "true", "yes", "on", "y"}
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


# 上游配置键 -> (环境变量名, 默认值)。默认值与 main.py 中的 _cfg 回退保持一致。
_SCHEMA: dict[str, tuple[str, Any]] = {
    # —— 总开关 ——
    "enabled": ("HD2_ENABLED", True),
    # —— 主 API（HD2Client）——
    "api_base": ("HD2_API_BASE", "https://api.helldivers2.dev"),
    "war_id": ("HD2_WAR_ID", 801),
    "api_client_name": ("HD2_API_CLIENT_NAME", "atri_helldivers"),
    "api_contact": ("HD2_API_CONTACT", ""),
    "api_user_agent": ("HD2_API_USER_AGENT", USER_AGENT),
    "api_timeout": ("HD2_API_TIMEOUT", 20),
    "proxy_url": ("HD2_PROXY_URL", ""),
    "retry_base_delay": ("HD2_RETRY_BASE_DELAY", 3.0),
    "retry_max_delay": ("HD2_RETRY_MAX_DELAY", 15.0),
    "retry_max_attempts": ("HD2_RETRY_MAX_ATTEMPTS", 2),
    "min_request_interval": ("HD2_MIN_REQUEST_INTERVAL", 3.0),
    "steam_appid": ("HD2_STEAM_APPID", 553850),
    "steam_news_count": ("HD2_STEAM_NEWS_COUNT", 10),
    # —— 缓存 ——
    "cache_update_interval": ("HD2_CACHE_UPDATE_INTERVAL", 300),
    "request_delay": ("HD2_REQUEST_DELAY", 6),
    "enable_background_refresh": ("HD2_BACKGROUND_REFRESH", True),
    # —— 翻译 ——
    "enable_translation": ("HD2_TRANSLATION", True),
    "translation_api_url": (
        "HD2_TRANSLATION_API_URL",
        "https://uapis.cn/api/v1/ai/translate",
    ),
    "translation_timeout": ("HD2_TRANSLATION_TIMEOUT", 20),
    "translation_target_lang": ("HD2_TRANSLATION_TARGET_LANG", "zh"),
    "translation_mode": ("HD2_TRANSLATION_MODE", "auto"),
    "translation_api_key": ("HD2_TRANSLATION_API_KEY", ""),
    "translation_model": ("HD2_TRANSLATION_MODEL", "gpt-4o-mini"),
    "translation_stream": ("HD2_TRANSLATION_STREAM", False),
    "translation_retry_fallback_seconds": ("HD2_TRANSLATION_FALLBACK_TTL", 180),
    "translation_error_cooldown_seconds": ("HD2_TRANSLATION_ERROR_COOLDOWN", 120),
    "translation_max_tokens": ("HD2_TRANSLATION_MAX_TOKENS", 1200),
    "translation_min_interval_seconds": ("HD2_TRANSLATION_MIN_INTERVAL", 0),
    # —— Wiki ——
    "enable_wiki": ("HD2_WIKI", True),
    "wiki_base": ("HD2_WIKI_BASE", "https://helldivers.wiki.gg"),
    "wiki_api_path": ("HD2_WIKI_API_PATH", "/api.php"),
    "wiki_cache_ttl": ("HD2_WIKI_CACHE_TTL", 21600),
    "wiki_search_cache_ttl": ("HD2_WIKI_SEARCH_CACHE_TTL", 1800),
    "wiki_search_limit": ("HD2_WIKI_SEARCH_LIMIT", 5),
    "wiki_extract_chars": ("HD2_WIKI_EXTRACT_CHARS", 900),
    "wiki_full_extract_chars": ("HD2_WIKI_FULL_EXTRACT_CHARS", 3000),
    "wiki_prefer_hd2": ("HD2_WIKI_PREFER_HD2", True),
    "translate_wiki": ("HD2_TRANSLATE_WIKI", True),
    # —— 素材清单 ——
    "asset_manifest_path": ("HD2_ASSET_MANIFEST_PATH", ""),
    # —— 地图 ——
    "enable_map": ("HD2_MAP", True),
    "map_cache_ttl": ("HD2_MAP_CACHE_TTL", 600),
    "map_label_top_n": ("HD2_MAP_LABEL_TOP_N", 15),
    "map_visual_mode": ("HD2_MAP_VISUAL_MODE", "uploaded_reference"),
    # —— 远程图片缓存 ——
    "remote_image_max_bytes": ("HD2_REMOTE_IMAGE_MAX_BYTES", 8388608),
    "remote_image_max_pixels": ("HD2_REMOTE_IMAGE_MAX_PIXELS", 16000000),
    "image_cache_max_mb": ("HD2_IMAGE_CACHE_MAX_MB", 150),
    # —— 战线趋势 ——
    "campaign_rate_window_minutes": ("HD2_CAMPAIGN_RATE_WINDOW_MINUTES", 20),
    "campaign_history_hours": ("HD2_CAMPAIGN_HISTORY_HOURS", 2),
    # —— Steam 卡片 ——
    "steam_max_content_length": ("HD2_STEAM_MAX_CONTENT_LENGTH", 2000),
    "steam_balancing_limit": ("HD2_STEAM_BALANCING_LIMIT", 800),
    "steam_fixes_limit": ("HD2_STEAM_FIXES_LIMIT", 600),
    "steam_issues_limit": ("HD2_STEAM_ISSUES_LIMIT", 300),
    "steam_max_sections": ("HD2_STEAM_MAX_SECTIONS", 3),
    "steam_image_enabled": ("HD2_STEAM_IMAGE_ENABLED", True),
    # —— 后台轮换 ——
    "rotation_dispatches": ("HD2_ROTATION_DISPATCHES", 300),
    "rotation_orders": ("HD2_ROTATION_ORDERS", 600),
    "rotation_steam": ("HD2_ROTATION_STEAM", 900),
    # —— 其他 ——
    "dashboard_low_impact_threshold": ("HD2_DASHBOARD_LOW_IMPACT_THRESHOLD", 800),
    "drop_untranslated_content": ("HD2_DROP_UNTRANSLATED", True),
    "enable_arsenal": ("HD2_ARSENAL", True),
    # —— Companion 网页截图（Playwright / Browserless）——
    "enable_companion_screenshots": ("HD2_COMPANION", True),
    "companion_render_backend": ("HD2_COMPANION_BACKEND", "playwright"),
    "companion_browserless_url": (
        "HD2_BROWSERLESS_URL", "http://browserless:3000"
    ),
    "companion_browserless_token": ("HD2_BROWSERLESS_TOKEN", ""),
    "companion_browserless_proxy_url": ("HD2_BROWSERLESS_PROXY", ""),
    "companion_screenshot_timeout": ("HD2_COMPANION_TIMEOUT", 60),
    "companion_screenshot_wait_ms": ("HD2_COMPANION_WAIT_MS", 5000),
    "companion_page_load_timeout": ("HD2_PAGE_LOAD_TIMEOUT", 20.0),
    "companion_translation_timeout": (
        "HD2_COMPANION_TRANSLATION_TIMEOUT", 60.0
    ),
    "companion_translation_batch_timeout": (
        "HD2_COMPANION_TRANSLATION_BATCH_TIMEOUT", 20.0
    ),
    "companion_screenshot_cache_ttl": ("HD2_COMPANION_CACHE_TTL", 300),
    "companion_screenshot_max_bytes": (
        "HD2_COMPANION_MAX_BYTES", 12 * 1024 * 1024
    ),
    "companion_screenshot_max_pixels": (
        "HD2_COMPANION_MAX_PIXELS", 20_000_000
    ),
    "companion_show_inactive": ("HD2_COMPANION_SHOW_INACTIVE", False),
    "companion_warmup_enabled": ("HD2_COMPANION_WARMUP", True),
    "companion_warmup_interval": ("HD2_COMPANION_WARMUP_INTERVAL", 300),
    "companion_warmup_kinds": (
        "HD2_COMPANION_WARMUP_KINDS", "news_latest,station"
    ),
    "companion_news_push_enabled": ("HD2_NEWS_PUSH", False),
    # —— B站银河快报监听 ——
    "enable_bilibili_galaxy_news": ("HD2_BILIBILI_ENABLED", False),
    "bilibili_uid": ("HD2_BILIBILI_UID", "3546777720457465"),
    "bilibili_keyword": ("HD2_BILIBILI_KEYWORD", "银河快报"),
    "bilibili_poll_interval": ("HD2_BILIBILI_INTERVAL", 300),
    "bilibili_cookie": ("HD2_BILIBILI_COOKIE", ""),
    "bilibili_image_max_bytes": (
        "HD2_BILIBILI_MAX_BYTES", 16 * 1024 * 1024
    ),
    "bilibili_image_max_pixels": ("HD2_BILIBILI_MAX_PIXELS", 64_000_000),
}


@dataclass
class HelldiversConfig:
    """运行期 Helldivers 配置；数据目录单独管理，其余通过 ``.get`` 读取。"""

    data_dir: Path
    _values: dict[str, Any]

    @classmethod
    def from_env(cls, data_dir: Path) -> "HelldiversConfig":
        """从 ``HD2_*`` 环境变量构建配置。"""
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        values: dict[str, Any] = {}
        for key, (env_name, default) in _SCHEMA.items():
            values[key] = _coerce(_raw(env_name), default)
        return cls(data_dir=data_dir, _values=values)

    def get(self, key: str, default: Any = None) -> Any:
        """读取配置项；与原 ``main.py`` 的 ``_cfg`` 行为一致。

        若该键在 schema 中，则返回环境变量值或 schema 默认值；
        若键未知（理论上不应发生），则回退到调用方传入的 ``default``。
        """
        return self._values.get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self._values.get("enabled", True))
