"""Screenshots for trusted Helldivers Companion routes."""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from PIL import Image, UnidentifiedImageError

from .._logging import logger

from ..core.diagnostics import HEALTH

COMPANION_ORIGIN = "https://helldiverscompanion.com/"
COMPANION_HOST = "helldiverscompanion.com"

_NEWS_ROUTE_RE = re.compile(r"^hellpad/gww/news/(?P<id>\d{1,10})$")
# 2026-08 网站改版：详情路由要求带星球编号后缀 `_{(index)}`，不带编号的
# 旧格式（如 #hellpad/planets/luxuriant）会被弹回首页。
_PLANET_ROUTE_RE = re.compile(
    r"^hellpad/planets/(?P<slug>[a-z0-9][a-z0-9_-]{0,63}?)(?:_\((?P<index>\d{1,6})\))?$"
)
_PLANET_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}(?:_\(\d{1,6}\))?$")

_CLEANUP_SCRIPT = r"""
(() => {
  const phrases = [
    "A NEW MESSAGE has been received",
    "A NEW MAJOR ORDER has been issued",
    "A NEW DISPATCH has been broadcast"
  ];
  const hideToasts = () => {
    const nodes = Array.from(document.querySelectorAll("div"));
    const items = nodes.filter((node) => {
      const text = (node.textContent || "").trim();
      return phrases.some((phrase) => text.startsWith(phrase));
    });
    for (const item of items) {
      const container = item.parentElement;
      if (container && container.getBoundingClientRect().width <= 420) {
        container.style.setProperty("display", "none", "important");
      }
    }
  };
  hideToasts();
  new MutationObserver(hideToasts).observe(document.documentElement, {
    childList: true,
    subtree: true
  });
  setInterval(hideToasts, 250);
})();
""".strip()

_PREPARE_CAPTURE_SCRIPT = r"""
({ kind, clip }) => {
  document.querySelectorAll("[data-hd2-capture-root]").forEach((node) => {
    node.removeAttribute("data-hd2-capture-root");
  });

  let root = null;
  if (kind === "news_latest") {
    const relativeTime = /(?:JUST NOW|MINUTES? AGO|HOURS? AGO|DAYS? AGO|TODAY|YESTERDAY)/i;
    const candidates = Array.from(document.querySelectorAll(
      "article, div.inline-block, [class*='news'], [class*='News'], [data-news]"
    )).filter((node) => {
      const text = (node.innerText || "").trim();
      const rect = node.getBoundingClientRect();
      return text.length > 200 &&
             relativeTime.test(text) &&
             rect.width >= 500 &&
             rect.height >= 250 &&
             rect.width > 0 &&
             rect.height > 0;
    });
    // The DOM is ordered newest-first. Do not sort by area: nested elements
    // from older cards are often smaller and were incorrectly selected.
    root = candidates.sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return ar.top - br.top || br.width * br.height - ar.width * ar.height;
    })[0] || null;
  } else if (kind === "station") {
    const candidates = Array.from(document.querySelectorAll("div.flex.flex-col.gap-2"))
      .filter((node) => {
        const text = (node.innerText || "").toUpperCase();
        return text.includes("NEXT FTL JUMP") &&
               text.includes("ORBITAL BLOCKADE") &&
               text.includes("EAGLE STORM");
      });
    const details = candidates.sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return ar.width * ar.height - br.width * br.height;
    })[0] || null;
    root = details?.closest(".noselect.inner") || details;
  } else if (kind === "planet") {
    // 2026-08 网站改版后 .entire_page/.page_wrapper 已不存在，
    // 星球卡片文本随整页一起提取即可。
  }
  root ||= kind === "news_latest"
    ? (document.querySelector("main") || document.body)
    : document.body;
  root.setAttribute("data-hd2-capture-root", "true");

  const nodes = [];
  const unique = new Set();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const viewportBottom = clip ? clip.y + clip.height : window.innerHeight;
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const parent = node.parentElement;
    const text = (node.nodeValue || "").trim();
    if (!parent || !text || text.length > 1400 || !/[A-Za-z]{3}/.test(text)) continue;
    const style = getComputedStyle(parent);
    const rect = parent.getBoundingClientRect();
    if (style.display === "none" || style.visibility === "hidden" || rect.width <= 0 || rect.height <= 0) continue;
    if ((kind === "planet" || kind === "station") &&
        (rect.bottom < clip.y || rect.top > viewportBottom)) continue;
    nodes.push(node);
    unique.add(text);
    if (unique.size >= 96) break;
  }
  window.__hd2TranslationNodes = nodes;
  return Array.from(unique);
}
""".strip()

_APPLY_TRANSLATIONS_SCRIPT = r"""
(translations) => {
  const nodes = window.__hd2TranslationNodes || [];
  for (const node of nodes) {
    const raw = node.nodeValue || "";
    const source = raw.trim();
    const translated = translations[source];
    if (!translated || translated === source) continue;
    const leading = raw.match(/^\s*/)?.[0] || "";
    const trailing = raw.match(/\s*$/)?.[0] || "";
    node.nodeValue = `${leading}${translated}${trailing}`;
  }
}
""".strip()

# Toggle the "Inactive Campaigns" setting (low-activity card visibility) to Shown
# via in-app hash navigation. Runs inside the page, returns true on success.
# The site stores the choice in localStorage["C-04"]; values are non-ASCII /
# obfuscated, so we drive the real toggle control rather than forging storage.
_COMPANION_SHOW_INACTIVE_SCRIPT = r"""
async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const hasInactiveText = () =>
    Array.from(document.querySelectorAll("*")).some(
      (el) => (el.textContent || "").trim() === "Inactive Campaigns"
    );

  try {
    // 1. go to settings and wait for the row to render
    location.hash = "settings";
    for (let i = 0; i < 75; i++) {
      if (hasInactiveText()) break;
      await sleep(200);
    }
    if (!hasInactiveText()) return false;

    // 2. find the "Shown" icon (right-arrow polygon "8 16...") and click it
    const icNode = Array.from(document.querySelectorAll("*")).find(
      (el) => (el.textContent || "").trim() === "Inactive Campaigns"
    );
    let row = icNode;
    for (let i = 0; i < 5 && row; i++) {
      row = row.parentElement;
      if ((row?.className || "").toString().includes("min-h-[60px]")) break;
    }
    if (!row) return false;
    const icons = Array.from(row.querySelectorAll(".cursor-pointer") || []);
    const target = icons.find(
      (ic) =>
        (ic.querySelector("polygon")?.getAttribute("points") || "").startsWith(
          "8 16"
        )
    );
    if (!target) return false;
    const r = target.getBoundingClientRect();
    target.dispatchEvent(
      new MouseEvent("click", {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: r.x + r.width / 2,
        clientY: r.y + r.height / 2,
      })
    );
    await sleep(500); // let localStorage persist the change

    // 3. wait for the fuller card list
    for (let i = 0; i < 75; i++) {
      const count = document.querySelectorAll(".planets-list > *").length;
      if (count > 10) return true;
      await sleep(200);
    }
    return true; // still return true even if count threshold not met
  } finally {
    // CRITICAL: always return to the homepage, even on failure — otherwise
    // the screenshot captures the settings page.
    location.hash = "";
    await sleep(500);
  }
}
""".strip()

class CompanionScreenshotError(RuntimeError):
    """Base exception for Companion screenshot failures."""


class CompanionConfigurationError(CompanionScreenshotError):
    """Raised when the Browserless endpoint is unavailable or invalid."""


class CompanionTargetError(CompanionScreenshotError, ValueError):
    """Raised when a command target is not an allowed Companion route."""


@dataclass(frozen=True, slots=True)
class CompanionTarget:
    kind: Literal["news", "news_latest", "planet", "station", "homepage"]
    value: str
    url: str
    label: str
    viewport: tuple[int, int]
    clip: tuple[int, int, int, int]

    @property
    def cache_name(self) -> str:
        return f"companion_{self.kind}_{self.value}.png"


TranslationCallback = Callable[
    [CompanionTarget, list[str]], Awaitable["CompanionTranslationResult"]
]


@dataclass(frozen=True, slots=True)
class CompanionTranslationResult:
    translations: dict[str, str]
    cacheable: bool = True
    complete: bool = True
    untranslated_count: int = 0
    rate_limited: bool = False


def _fragment_from_value(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        return raw.removeprefix("#").strip("/")

    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CompanionTargetError("Companion 链接端口格式无效") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != COMPANION_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise CompanionTargetError("只允许 helldiverscompanion.com 的 HTTPS 链接")
    return parsed.fragment.strip("/")


def parse_companion_news_target(value: str) -> CompanionTarget:
    fragment = _fragment_from_value(value)
    if fragment is None:
        raise CompanionTargetError("请提供新闻编号或 Companion 新闻链接")
    if fragment.isdigit():
        if _NEWS_ROUTE_RE.fullmatch(f"hellpad/gww/news/{fragment}") is None:
            raise CompanionTargetError("新闻链接格式应为 #hellpad/gww/news/<编号>")
        news_id = fragment
    else:
        match = _NEWS_ROUTE_RE.fullmatch(fragment.casefold())
        if match is None:
            raise CompanionTargetError("新闻链接格式应为 #hellpad/gww/news/<编号>")
        news_id = match.group("id")
    return CompanionTarget(
        kind="news",
        value=news_id,
        url=f"{COMPANION_ORIGIN}#hellpad/gww/news/{news_id}",
        label=f"Helldivers Companion 新闻 #{news_id}",
        viewport=(800, 1000),
        clip=(15, 87, 770, 841),
    )


def companion_latest_news_target() -> CompanionTarget:
    return CompanionTarget(
        kind="news_latest",
        value="latest",
        url=f"{COMPANION_ORIGIN}#news",
        label="Helldivers Companion 最新新闻",
        viewport=(1280, 900),
        clip=(268, 154, 744, 644),
    )


def companion_dss_target() -> CompanionTarget:
    return CompanionTarget(
        kind="station",
        value="dss",
        url=f"{COMPANION_ORIGIN}#hellpad/stations",
        label="Helldivers Companion 民主空间站",
        viewport=(1280, 900),
        clip=(209, 82, 862, 734),
    )


def companion_homepage_target() -> CompanionTarget:
    # Companion 首页是 overflow:hidden 的应用外壳，设计宽度 1920px；
    # 视口窄于 1920 时主面板会响应式压缩（planets-list 1680→1000），故用设计宽度截全屏。
    return CompanionTarget(
        kind="homepage",
        value="home",
        url=COMPANION_ORIGIN,
        label="Helldivers Companion 首页",
        viewport=(1920, 1080),
        clip=(0, 0, 1920, 1080),
    )


def parse_companion_planet_target(value: str) -> CompanionTarget:
    fragment = _fragment_from_value(value)
    if fragment is None:
        raise CompanionTargetError("请提供星球 slug 或 Companion 星球链接")
    match = _PLANET_ROUTE_RE.fullmatch(fragment.casefold())
    if match is not None:
        slug = match.group("slug")
        if match.group("index"):
            slug = f"{slug}_({match.group('index')})"
    else:
        slug = re.sub(r"\s+", "_", fragment.casefold().strip())
        if _PLANET_SLUG_RE.fullmatch(slug) is None:
            raise CompanionTargetError(
                "星球参数仅支持英文 slug 或带编号的星球链接，"
                "例如 meissa、super_earth 或 luxuriant_(268)"
            )
    return CompanionTarget(
        kind="planet",
        value=slug,
        # 2026-08 网站改版：详情路由要求 `_{(index)}` 编号后缀，index 由
        # services.resolve_companion_planet 从战争数据补全后再传入。
        url=f"{COMPANION_ORIGIN}#hellpad/planets/{slug}",
        label=f"Helldivers Companion 星球：{slug.upper()}",
        viewport=(2560, 1440),
        clip=(0, 0, 2560, 1440),
    )


def build_browserless_endpoint(endpoint_url: str, token: str = "") -> str:
    raw = str(endpoint_url or "").strip()
    if not raw:
        raise CompanionConfigurationError("尚未配置 Browserless 截图服务地址")
    parsed = urlsplit(raw)
    try:
        parsed.port
    except ValueError as exc:
        raise CompanionConfigurationError("Browserless 地址端口格式无效") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CompanionConfigurationError("Browserless 地址必须是有效的 HTTP(S) URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/screenshot"):
        path += "/screenshot"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if token:
        query["token"] = token
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, urlencode(query), "")
    )


def build_browserless_payload(
    target: CompanionTarget,
    *,
    timeout_ms: int,
    wait_ms: int,
) -> dict:
    width, height = target.viewport
    x, y, clip_width, clip_height = target.clip
    return {
        "url": target.url,
        "gotoOptions": {
            "waitUntil": "networkidle2",
            "timeout": max(10_000, int(timeout_ms)),
        },
        "viewport": {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
        },
        "waitForTimeout": max(0, int(wait_ms)),
        "addScriptTag": [{"content": _CLEANUP_SCRIPT}],
        "options": {
            "type": "png",
            "fullPage": False,
            "captureBeyondViewport": False,
            "clip": {
                "x": x,
                "y": y,
                "width": clip_width,
                "height": clip_height,
            },
        },
    }


def _browserless_connection_error(endpoint: str, exc: BaseException) -> str:
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        host = urlsplit(endpoint).hostname or "配置的主机"
        return (
            f"无法解析 Browserless 主机 {host}。如果使用 http://browserless:3000，"
            "请确认 AstrBot 与 Browserless 在同一个 Docker 网络；如果 AstrBot 在"
            "宿主机/Windows 运行，请将 Browserless 映射到本机端口，并把 "
            "companion_browserless_url 改为 http://127.0.0.1:3000。"
        )
    return f"连接 Browserless 失败 ({type(exc).__name__})"


class CompanionScreenshotClient:
    def __init__(
        self,
        output_dir: Path,
        *,
        endpoint_url: str,
        token: str = "",
        proxy_url: str = "",
        timeout: int = 90,
        wait_ms: int = 5_000,
        cache_ttl: int = 300,
        max_bytes: int = 12 * 1024 * 1024,
        max_pixels: int = 20_000_000,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.endpoint = build_browserless_endpoint(endpoint_url, token)
        self.proxy_url = str(proxy_url or "").strip()
        self.timeout = max(15, int(timeout))
        self.wait_ms = max(0, min(30_000, int(wait_ms)))
        self.cache_ttl = max(0, int(cache_ttl))
        self.max_bytes = max(1024, int(max_bytes))
        self.max_pixels = max(1_000_000, int(max_pixels))
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_refs: dict[str, int] = {}

    async def capture(self, target: CompanionTarget, *, force: bool = False) -> Path:
        output = self.output_dir / target.cache_name
        if not force and self._cache_is_fresh(output):
            return output
        key = target.cache_name
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_refs[key] = self._lock_refs.get(key, 0) + 1
        try:
            async with lock:
                if not force and self._cache_is_fresh(output):
                    return output
                await self._capture_uncached(target, output)
                self._cleanup()
                return output
        finally:
            remaining = self._lock_refs.get(key, 1) - 1
            if remaining <= 0:
                self._lock_refs.pop(key, None)
                if self._locks.get(key) is lock and not lock.locked():
                    self._locks.pop(key, None)
            else:
                self._lock_refs[key] = remaining

    def _cache_is_fresh(self, path: Path) -> bool:
        try:
            return (
                self.cache_ttl > 0
                and path.is_file()
                and time.time() - path.stat().st_mtime <= self.cache_ttl
            )
        except OSError:
            return False

    async def _capture_uncached(self, target: CompanionTarget, output: Path) -> None:
        timeout_ms = self.timeout * 1000
        payload = build_browserless_payload(
            target,
            timeout_ms=timeout_ms,
            wait_ms=self.wait_ms,
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.post(
                    self.endpoint,
                    json=payload,
                    proxy=self.proxy_url or None,
                ) as response:
                    if response.status != 200:
                        detail = (await response.content.read(1024)).decode(
                            "utf-8", errors="replace"
                        )
                        raise CompanionScreenshotError(
                            f"Browserless 返回 HTTP {response.status}: {detail[:300]}"
                        )
                    content_type = response.headers.get("Content-Type", "").split(
                        ";", 1
                    )[0].casefold()
                    if content_type not in {
                        "image/png",
                        "image/jpeg",
                        "image/webp",
                        "application/octet-stream",
                    }:
                        raise CompanionScreenshotError(
                            f"Browserless 返回了非图片内容: {content_type or 'unknown'}"
                        )
                    total = 0
                    with temporary.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise CompanionScreenshotError("网页截图超过允许的文件大小")
                            handle.write(chunk)
            self._validate_image(temporary)
            temporary.replace(output)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CompanionScreenshotError(
                _browserless_connection_error(self.endpoint, exc)
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_image(self, path: Path) -> None:
        try:
            with Image.open(path) as image:
                if image.width * image.height > self.max_pixels:
                    raise CompanionScreenshotError("网页截图像素数量超过安全限制")
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise CompanionScreenshotError("Browserless 返回了不支持的图片格式")
                image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise CompanionScreenshotError("Browserless 返回的图片无法解码") from exc

    def _cleanup(self, max_age: int = 86_400) -> None:
        cutoff = time.time() - max(3600, int(max_age))
        try:
            for path in self.output_dir.glob("companion_*.png"):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            pass


class CompanionPlaywrightClient:
    """Render Companion pages with Chromium inside the AstrBot container."""

    def __init__(
        self,
        output_dir: Path,
        *,
        timeout: int = 90,
        wait_ms: int = 5_000,
        cache_ttl: int = 300,
        max_bytes: int = 12 * 1024 * 1024,
        max_pixels: int = 20_000_000,
        concurrency: int = 2,
        translate_callback: TranslationCallback | None = None,
        page_load_timeout: float = 20.0,
        translation_timeout: float = 20.0,
        show_inactive: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = max(15, int(timeout))
        self.wait_ms = max(0, min(30_000, int(wait_ms)))
        self.cache_ttl = max(0, int(cache_ttl))
        self.max_bytes = max(1024, int(max_bytes))
        self.max_pixels = max(1_000_000, int(max_pixels))
        self.translate_callback = translate_callback
        self.page_load_timeout = max(5.0, min(120.0, float(page_load_timeout)))
        # Free-tier models often need 40–90s for large pages (planet/homepage).
        # Keep a hard ceiling, but raise it so partial Chinese is not cut off early.
        self.translation_timeout = max(5.0, min(120.0, float(translation_timeout)))
        self.show_inactive = bool(show_inactive)
        self._cache_namespace = "source"
        self._playwright = None
        self._browser = None
        self._start_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, min(4, int(concurrency))))
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_refs: dict[str, int] = {}
        self._translation_tasks: set[
            asyncio.Task[CompanionTranslationResult]
        ] = set()
        # Tracks which page kinds were last served from a stale cache, so
        # command handlers can annotate the message ("翻译快照").
        self._served_stale: set[str] = set()
        # Text fragments extracted during the most recent real capture of each
        # page kind (keyed by target.kind).  Populated in _capture_uncached;
        # NOT updated when the TTL cache short-circuits.  Used by the warmup
        # loop to compute a content fingerprint for news-change detection.
        self._last_capture_texts: dict[str, list[str]] = {}

    def capture_texts(self, kind: str) -> list[str] | None:
        """Return the text fragments from the most recent real capture of *kind*.

        Returns ``None`` if no capture has performed text extraction yet.
        When a capture serves a stale cache before extracting text (early
        loading-failure returns), the previous successful texts are retained,
        so the returned value may be older than the last ``capture()`` call.
        """
        return self._last_capture_texts.get(kind)

    def set_translate_callback(
        self,
        callback: TranslationCallback | None,
        *,
        cache_namespace: str = "",
    ) -> None:
        self.translate_callback = callback
        if callback is None:
            self._cache_namespace = "source"
            return
        fingerprint = str(cache_namespace or "translated")
        digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:10]
        self._cache_namespace = f"translated_{digest}"

    def _finish_translation_task(
        self,
        task: asyncio.Task[CompanionTranslationResult],
    ) -> None:
        self._translation_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            logger.warning(
                "[HD2] Companion 后台翻译失败: %s: %s",
                type(exc).__name__,
                exc,
            )

    def _cache_key(self, target: CompanionTarget) -> str:
        """Cache file basename that also varies by show_inactive state.

        Otherwise enabling the toggle still serves the stale 6-card screenshot
        because both states share ``companion_homepage_home.png``.
        """

        suffix = "_inactive" if self.show_inactive else ""
        return f"{target.cache_name}{suffix}"

    async def start(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise CompanionConfigurationError(
                    "未安装 Playwright，请在 AstrBot 容器中安装 playwright"
                ) from exc
            try:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except Exception as exc:
                try:
                    await playwright.stop()  # type: ignore[possibly-undefined]
                except Exception:
                    pass
                raise CompanionConfigurationError(
                    f"本地 Chromium 启动失败: {type(exc).__name__}: {exc}"
                ) from exc
            self._playwright = playwright
            self._browser = browser

    async def close(self) -> None:
        translation_tasks = list(self._translation_tasks)
        self._translation_tasks.clear()
        for task in translation_tasks:
            task.cancel()
        if translation_tasks:
            await asyncio.gather(*translation_tasks, return_exceptions=True)
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()


    def served_stale_kinds(self) -> set[str]:
        """Kinds whose last capture served a stale cache (push gating)."""

        return set(self._served_stale)

    def cached_output_path(self, target: CompanionTarget) -> Path:
        """On-disk path of the cached screenshot for *target*.

        单一来源：main.py 的 stale 兜底等外部逻辑必须经此方法取缓存
        路径，禁止自行拼接文件名（客户端改名时外部会静默失配）。
        """

        return self.output_dir / (
            f"playwright_v9_{self._cache_namespace}_{self._cache_key(target)}"
        )

    async def capture(self, target: CompanionTarget, *, force: bool = False) -> Path:
        output = self.cached_output_path(target)
        # Reset stale-tracking at the start of each capture call; it is set
        # to True inside _capture_uncached when a stale cache is served.
        self._served_stale.discard(target.kind)
        if not force and self._cache_is_fresh(output):
            HEALTH.record_ok("screenshot", detail=f"{target.label} 缓存命中")
            return output
        try:
            async with asyncio.timeout(self.timeout):
                captured = await self._capture_within_timeout(
                    target, output, force=force
                )
        except TimeoutError as exc:
            HEALTH.record_fail(
                "screenshot", detail=f"{target.label} 超时>{int(self.timeout)}s"
            )
            raise CompanionScreenshotError(
                f"Companion 截图总耗时超过 {self.timeout} 秒"
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            HEALTH.record_fail("screenshot", detail=f"{target.label}: {exc}")
            raise
        HEALTH.record_ok("screenshot", detail=str(target.label))
        return captured

    async def _capture_within_timeout(
        self,
        target: CompanionTarget,
        output: Path,
        *,
        force: bool,
    ) -> Path:
        await self.start()
        key = self._cache_key(target)
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_refs[key] = self._lock_refs.get(key, 0) + 1
        try:
            async with lock:
                if not force and self._cache_is_fresh(output):
                    return output
                async with self._semaphore:
                    captured = await self._capture_uncached(target, output)
                self._cleanup()
                return captured
        finally:
            remaining = self._lock_refs.get(key, 1) - 1
            if remaining <= 0:
                self._lock_refs.pop(key, None)
                if self._locks.get(key) is lock and not lock.locked():
                    self._locks.pop(key, None)
            else:
                self._lock_refs[key] = remaining

    async def _capture_uncached(self, target: CompanionTarget, output: Path) -> Path:
        browser = self._browser
        if browser is None or not browser.is_connected():
            raise CompanionScreenshotError("本地 Chromium 尚未启动")
        width, height = target.viewport
        x, y, clip_width, clip_height = target.clip
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        context = None
        failed_requests: list[str] = []
        cacheable = True
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                # planet 是整页 2560×1440 截图
                device_scale_factor=1,
                locale="en-US",
                ignore_https_errors=True,
            )
            page = await context.new_page()
            page.on(
                "requestfailed",
                lambda request: failed_requests.append(
                    f"{request.url} ({request.failure or 'failed'})"
                ),
            )
            # 海外 Companion 站点首字节响应慢且抖动大（实测 5-18 秒），
            # 原 15 秒上限在慢网络下会误判失败，放宽到 30 秒并仍受总超时约束。
            goto_timeout = min(30_000, self.timeout * 1000)
            await page.goto(
                target.url,
                wait_until="commit",
                timeout=goto_timeout,
            )
            try:
                load_timeout = min(
                    int(self.page_load_timeout * 1000),
                    max(
                        5_000,
                        (self.timeout - goto_timeout / 1000) * 1000,
                    ),
                )
                await page.wait_for_function(
                    """
                    ({ kind, expected }) => {
                      const text = (document.body?.innerText || "").toUpperCase();

                      // Detect loading overlays by checking for visible
                      // full-screen overlay/spinner elements.  We must NOT
                      // match text substrings like "RELOADING" that appear
                      // in normal content, so we rely on element-based checks.
                      // NOTE: .pulse_station is a PERMANENT decorative element
                      // on the DSS page, NOT a loading spinner — do not treat
                      // it as a loading indicator.
                      const overlays = document.querySelectorAll(
                        ".backdrop.pulse-2, [class*='splash'], [class*='Splash']"
                      );
                      for (const el of overlays) {
                        const s = getComputedStyle(el);
                        if (s.display === "none" || s.visibility === "hidden") continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 200 && r.height > 200) {
                          return false;
                        }
                      }

                      if (text.length <= 200) {
                        return false;
                      }
                      if (kind === "news_latest") {
                        // Require a relative-time marker (JUST NOW / … AGO)
                        // to prove the actual news feed rendered, not just
                        // the chrome (header/footer/menus) which can exceed
                        // 200 chars on its own.
                        return /(?:JUST NOW|MINUTES? AGO|HOURS? AGO|DAYS? AGO|TODAY|YESTERDAY)/i.test(text);
                      }
                      if (kind === "news") {
                        return text.includes(`ID ${expected}`) ||
                               (text.includes("STROHMANN") && text.includes("NEWS"));
                      }
                      if (kind === "station") {
                        // Require ALL three DSS tactical action names to be
                        // present — they only render after the DSS data API
                        // returns.  A bare .noselect.inner container can
                        // exist before data loads, so don't accept it alone.
                        return text.includes("NEXT FTL JUMP") &&
                               text.includes("ORBITAL BLOCKADE") &&
                               text.includes("EAGLE STORM");
                      }
                      if (kind === "homepage") {
                        // Reject the settings page — the show-inactive
                        // toggle navigates there and may not return cleanly
                        // on slow servers.
                        if (window.location.hash.toLowerCase().includes("settings")) {
                          return false;
                        }
                        // The homepage must show campaign data (a planets-list
                        // with children), not just the app shell or an SVG icon.
                        return document.querySelector(".planets-list > *") !== null ||
                               document.querySelector(
                                 "[class*='campaign'], [class*='Campaign']"
                               ) !== null;
                      }
                      if (kind === "planet") {
                        // 2026-08 网站改版：详情路由要求 `_{(index)}` 编号
                        // 后缀，缺后缀会被弹回首页——hash 离开 planets 路由
                        // 即视为未就绪（宁可超时走 stale 也不截首页）。
                        if (!window.location.hash
                          .toLowerCase()
                          .startsWith("#hellpad/planets/")
                        ) {
                          return false;
                        }
                        return text.includes(expected);
                      }
                      return text.includes(expected);
                    }
                    """,
                    arg={
                        "kind": target.kind,
                        # value 可能带 `_(index)` 编号后缀，页面文本只含星球名
                        "expected": target.value.split("_(")[0]
                        .replace("_", " ")
                        .upper(),
                    },
                    timeout=load_timeout,
                )
            except Exception as exc:
                relevant_failures = [
                    item
                    for item in failed_requests
                    if "google-analytics.com" not in item
                ]
                details = "; ".join(relevant_failures[-5:])
                suffix = f"；失败请求：{details}" if details else ""
                body_text = await page.evaluate(
                    "() => (document.body?.innerText || '').trim()"
                )
                body_length = len(body_text)
                # Check if any loading overlay is still visible (element-based,
                # NOT text-based — "LOADING"/"WAIT" appear in normal content).
                # NOTE: pulse_station is permanent on DSS, not a loading spinner.
                splash_info = await page.evaluate(
                    """
                    () => {
                      const overlays = document.querySelectorAll(
                        ".backdrop.pulse-2, [class*='splash'], [class*='Splash']"
                      );
                      for (const el of overlays) {
                        const s = getComputedStyle(el);
                        if (s.display === "none" || s.visibility === "hidden") continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 200 && r.height > 200) return true;
                      }
                      return false;
                    }
                    """
                )
                if splash_info:
                    # Try waiting an extra 10s for the page to finish loading.
                    logger.warning(
                        "[HD2] Companion %s 仍在加载（加载指示器未消失），额外等待 10s",
                        target.kind,
                    )
                    try:
                        await page.wait_for_function(
                            """
                            () => {
                              const overlays = document.querySelectorAll(
                                ".backdrop.pulse-2, [class*='splash'], [class*='Splash']"
                              );
                              for (const el of overlays) {
                                const s = getComputedStyle(el);
                                if (s.display === "none" || s.visibility === "hidden") continue;
                                const r = el.getBoundingClientRect();
                                if (r.width > 200 && r.height > 200) return false;
                              }
                              return true;
                            }
                            """,
                            timeout=10_000,
                        )
                    except Exception:
                        # Still loading after extra wait — stale cache or
                        # raise, depending on kind.
                        if self._cache_is_stale(output):
                            self._served_stale.add(target.kind)
                            logger.info(
                                "[HD2] Companion %s 页面加载超时，返回上次缓存截图（stale）",
                                target.kind,
                            )
                            return output
                        raise CompanionScreenshotError(
                            f"Companion 页面加载超时（加载指示器未消失）{suffix}"
                        ) from exc
                elif target.kind == "news_latest":
                    if body_length < 80:
                        if self._cache_is_stale(output):
                            self._served_stale.add(target.kind)
                            logger.info(
                                "[HD2] Companion 新闻页内容为空，返回上次缓存截图（stale）"
                            )
                            return output
                        raise CompanionScreenshotError(
                            f"Companion 新闻页内容为空（body_length={body_length}）"
                        ) from exc
                    logger.warning(
                        "[HD2] Companion 新闻页未在 %s 秒内完成，继续截取当前页面 body_length=%s%s",
                        load_timeout / 1000,
                        body_length,
                        suffix,
                    )
                else:
                    raise CompanionScreenshotError(
                        f"Companion 页面数据加载超时（body_length={body_length}）{suffix}"
                    ) from exc
            if self.show_inactive and target.kind == "homepage":
                # Toggle "Inactive Campaigns" to Shown so the homepage lists more
                # campaign cards (~+1.5s). Best-effort: never block the screenshot.
                logger.info("[HD2] Companion show-inactive 切换开始")
                try:
                    ok = await asyncio.wait_for(
                        page.evaluate(_COMPANION_SHOW_INACTIVE_SCRIPT),
                        timeout=min(25.0, max(8.0, self.timeout / 3)),
                    )
                    logger.info(
                        "[HD2] Companion show-inactive 切换完成 ok=%s", bool(ok)
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort toggle
                    logger.warning("[HD2] Companion show-inactive 切换失败，跳过：%s", exc)
            await page.add_script_tag(content=_CLEANUP_SCRIPT)
            await page.wait_for_timeout(min(self.wait_ms, 5_000))
            translations_source = await page.evaluate(
                _PREPARE_CAPTURE_SCRIPT,
                {
                    "kind": target.kind,
                    "clip": {
                        "x": x,
                        "y": y,
                        "width": clip_width,
                        # news 分段拼接会展示 644px 以下的正文，文本提取
                        # （viewportBottom 过滤）必须跟上可见范围，否则
                        # 拼接图下半部分永远保持英文。
                        "height": (
                            self._NEWS_STITCH_MAX_HEIGHT
                            if target.kind == "news_latest"
                            else clip_height
                        ),
                    },
                },
            )
            # Store extracted text fragments for news-change fingerprinting.
            # This runs on every real capture (cache miss), regardless of
            # whether translation is enabled.
            if isinstance(translations_source, list):
                self._last_capture_texts[target.kind] = [
                    str(v).strip()
                    for v in translations_source
                    if isinstance(v, str) and v.strip()
                ]
            if self.translate_callback is not None and isinstance(
                translations_source, list
            ):
                source_texts = [
                    str(value).strip()
                    for value in translations_source
                    if isinstance(value, str) and value.strip()
                ]
                # Translation is an enhancement, not a reason to lose the
                # screenshot. A slow cloud-hosted provider must not consume the
                # whole browser capture timeout.
                translation_timeout = min(
                    self.translation_timeout,
                    max(5.0, self.timeout / 2),
                )
                translation_task = asyncio.create_task(
                    self.translate_callback(target, source_texts),
                    name=f"hd2-companion-translate-{target.kind}",
                )
                try:
                    outcome = await asyncio.wait_for(
                        asyncio.shield(translation_task),
                        timeout=translation_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[HD2] Companion 翻译超过 %.1f 秒，保留英文继续截图；翻译将在后台写入缓存",
                        translation_timeout,
                    )
                    self._translation_tasks.add(translation_task)
                    translation_task.add_done_callback(
                        self._finish_translation_task
                    )
                    outcome = None
                except BaseException:
                    translation_task.cancel()
                    await asyncio.gather(translation_task, return_exceptions=True)
                    raise
                if outcome is not None:
                    cacheable = outcome.cacheable
                    if (
                        not outcome.translations
                        and outcome.untranslated_count > 0
                    ):
                        # Stale-while-revalidate: if we have a previous
                        # translated screenshot on disk, serve it instead
                        # of a fresh English/partial page.
                        if self._cache_is_stale(output):
                            self._served_stale.add(target.kind)
                            logger.info(
                                "[HD2] Companion %s 翻译未完成，返回上次缓存截图（stale）",
                                target.kind,
                            )
                            return output
                        # No stale cache: take the screenshot with whatever
                        # (zero) translations we have.  Never raise — the
                        # user always gets a screenshot.
                        logger.warning(
                            "[HD2] Companion %s 未获得中文译文且无缓存，保留原网页截图",
                            target.kind,
                        )
                        cacheable = False
                    if outcome.translations:
                        await page.evaluate(
                            _APPLY_TRANSLATIONS_SCRIPT,
                            outcome.translations,
                        )
                        await page.wait_for_timeout(500)
                    if not outcome.complete:
                        # Incomplete translation: prefer stale cache, but if
                        # none exists, still screenshot what we have (partial
                        # Chinese + remaining English).  Never raise.
                        if self._cache_is_stale(output):
                            self._served_stale.add(target.kind)
                            logger.info(
                                "[HD2] Companion %s 翻译不完整，返回上次缓存截图（stale）",
                                target.kind,
                            )
                            return output
                        logger.warning(
                            "[HD2] Companion %s 翻译不完整（%d/%d），"
                            "部分中文+部分英文继续截图",
                            target.kind,
                            len(outcome.translations),
                            len(outcome.translations) + outcome.untranslated_count,
                        )
                        cacheable = False
                else:
                    # outcome is None: the foreground translation timed out at
                    # the Playwright layer.  Prefer stale cache; otherwise take
                    # the screenshot as-is.  Never raise.
                    cacheable = False
                    if self._cache_is_stale(output):
                        self._served_stale.add(target.kind)
                        logger.info(
                            "[HD2] Companion %s 翻译前台超时，返回上次缓存截图（stale）",
                            target.kind,
                        )
                        return output
                    logger.warning(
                        "[HD2] Companion %s 翻译前台超时且无缓存，保留英文继续截图（不写入主缓存）",
                        target.kind,
                    )

            if target.kind in {"news_latest", "station"}:
                stitched = False
                if target.kind == "news_latest":
                    stitched = await self._stitch_news_capture(page, temporary)
                if not stitched:
                    await page.locator(
                        '[data-hd2-capture-root="true"]'
                    ).screenshot(
                        path=str(temporary),
                        type="png",
                        animations="disabled",
                    )
            else:
                await page.screenshot(
                    path=str(temporary),
                    type="png",
                    animations="disabled",
                    clip={
                        "x": x,
                        "y": y,
                        "width": clip_width,
                        "height": clip_height,
                    },
                )
            self._validate_image(temporary)
            destination = output
            if not cacheable:
                destination = output.with_name(
                    f"playwright_uncached_{uuid.uuid4().hex}_{target.cache_name}"
                )
            temporary.replace(destination)
            return destination
        except CompanionScreenshotError:
            raise
        except Exception as exc:
            raise CompanionScreenshotError(
                f"本地 Playwright 截图失败 ({type(exc).__name__}): {exc}"
            ) from exc
        finally:
            if context is not None:
                # 清理路径绝不抛错：context.close() 失败时保留原始
                # 超时/业务异常，不再让清理异常取而代之。
                try:
                    await context.close()
                except Exception as exc:  # noqa: BLE001 - cleanup path
                    logger.debug("[HD2] browser context close failed: %s", exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    _NEWS_STITCH_MAX_HEIGHT = 3600
    _NEWS_HEADER_CHROME = 82
    _NEWS_FOOTER_CHROME = 64

    async def _stitch_news_capture(self, page: Any, temporary: Path) -> bool:
        """news 页 2026-09 改版后挂在外层 overflow-hidden 壳的内层滚动容器里，
        元素截图超出视口的部分不会被绘制（成片下方大片黑底）。改为滚轮分段
        滚动 + 视口裁剪 + 纵向拼接，只覆盖根元素实际渲染的内容。返回 False
        时调用方回退旧的元素整体截图。"""
        try:
            first = await page.evaluate(
                """() => {
                  const root = document.querySelector(
                    '[data-hd2-capture-root="true"]'
                  );
                  if (!root) return null;
                  const r = root.getBoundingClientRect();
                  return { x: r.x, top: r.top, width: r.width,
                           height: r.height, viewport: innerHeight };
                }"""
            )
            if not first:
                return False
            art_x = max(0, int(first["x"]))
            art_w = int(
                min(first["width"], page.viewport_size["width"] - art_x)
            )
            root_h = int(first["height"])
            viewport_h = int(first["viewport"])
            if art_w < 300 or root_h <= viewport_h * 0.95:
                return False  # 内容本就在视口内，元素截图即可
            await page.mouse.move(art_x + art_w / 2, viewport_h / 2)
            canvas = Image.new(
                "RGB",
                (art_w, min(root_h, self._NEWS_STITCH_MAX_HEIGHT)),
                (12, 16, 24),
            )
            prev_end = 0
            for _ in range(8):
                probe = await page.evaluate(
                    """() => {
                      const root = document.querySelector(
                        '[data-hd2-capture-root="true"]'
                      );
                      return root ? root.getBoundingClientRect().top : null;
                    }"""
                )
                if probe is None:
                    return False
                top_now = float(probe)
                # 根内坐标 = 视口 y - top_now；避开固定头/底栏
                y0 = max(0.0, self._NEWS_HEADER_CHROME - top_now)
                y1 = min(
                    viewport_h - self._NEWS_FOOTER_CHROME - top_now, root_h
                )
                if y1 - y0 < 40:
                    break
                y0i, y1i = int(y0), int(y1)
                if y0i > prev_end:
                    # 平滑滚动过冲会留缝隙：向上滚回对齐
                    await page.mouse.wheel(0, -(y0i - prev_end))
                    await page.wait_for_timeout(400)
                    continue
                # 等根内图片解码，避免灰占位块进拼图
                await page.evaluate(
                    """() => Promise.race([
                      Promise.all(
                        Array.from(
                          document.querySelectorAll(
                            '[data-hd2-capture-root="true"] img'
                          )
                        ).map((img) =>
                          img.complete
                            ? Promise.resolve()
                            : img.decode().catch(() => {})
                        )
                      ),
                      new Promise((r) => setTimeout(r, 2000)),
                    ])"""
                )
                part = await page.screenshot(
                    type="png",
                    animations="disabled",
                    clip={
                        "x": art_x,
                        "y": y0i + top_now,
                        "width": art_w,
                        "height": y1i - y0i,
                    },
                )
                with Image.open(io.BytesIO(part)) as piece:
                    canvas.paste(piece.convert("RGB"), (0, y0i))
                prev_end = y1i
                if y1i >= root_h - 2 or prev_end >= canvas.height:
                    break
                await page.mouse.wheel(0, y1i - y0i)
                await page.wait_for_timeout(500)
            if prev_end <= 0:
                return False
            canvas.save(temporary, format="PNG", optimize=True)
            return True
        except Exception as exc:  # noqa: BLE001 - 拼接失败回退旧路径
            logger.warning(
                "[HD2] Companion news 分段拼接截图失败，回退元素整体截图: %s",
                exc,
            )
            return False

    def _cache_is_fresh(self, path: Path) -> bool:
        try:
            return (
                self.cache_ttl > 0
                and path.is_file()
                and time.time() - path.stat().st_mtime <= self.cache_ttl
            )
        except OSError:
            return False

    def _cache_is_stale(self, path: Path, *, max_age: int = 7_200) -> bool:
        """Whether *path* is an expired but still-usable cached screenshot.

        Used for stale-while-revalidate: when a fresh capture fails because
        the translation API is slow/rate-limited, serving a slightly-stale
        translated screenshot is far better than degrading to a local card
        or an untranslated English page.

        The default window is 2 hours (7200 s): long enough to ride out a
        transient translation-API outage, short enough that news content
        doesn't go stale.  The background warmup loop refreshes the cache
        every few minutes, so under normal operation the stale path is only
        hit when the API is genuinely down.
        """

        try:
            if not path.is_file():
                return False
            age = time.time() - path.stat().st_mtime
            # Beyond cache_ttl (so not "fresh") but within max_age so the
            # content is still recent enough to be useful.
            return self.cache_ttl < age <= max_age
        except OSError:
            return False

    def _validate_image(self, path: Path) -> None:
        try:
            if path.stat().st_size > self.max_bytes:
                raise CompanionScreenshotError("网页截图超过允许的文件大小")
            with Image.open(path) as image:
                if image.width * image.height > self.max_pixels:
                    raise CompanionScreenshotError("网页截图像素数量超过安全限制")
                if image.format != "PNG":
                    raise CompanionScreenshotError("本地浏览器返回了非 PNG 图片")
                image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise CompanionScreenshotError("本地浏览器返回的图片无法解码") from exc

    def _cleanup(self, max_age: int = 86_400) -> None:
        cutoff = time.time() - max(3600, int(max_age))
        try:
            for path in self.output_dir.glob("playwright_*companion_*.png"):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "COMPANION_ORIGIN",
    "CompanionConfigurationError",
    "CompanionPlaywrightClient",
    "CompanionScreenshotClient",
    "CompanionScreenshotError",
    "CompanionTarget",
    "CompanionTargetError",
    "CompanionTranslationResult",
    "TranslationCallback",
    "build_browserless_endpoint",
    "build_browserless_payload",
    "companion_dss_target",
    "companion_homepage_target",
    "companion_latest_news_target",
    "parse_companion_news_target",
    "parse_companion_planet_target",
]
