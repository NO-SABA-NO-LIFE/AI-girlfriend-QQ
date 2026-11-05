"""Poll Bilibili dynamics and deliver matching posts to subscribed sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import aiohttp

from ..core.diagnostics import HEALTH
from ..core.push_policy import MAX_PENDING_ENTRIES, PENDING_TTL_SECONDS, queued_timestamp

GALAXY_NEWS_UID = "3546777720457465"
LEGACY_GALAXY_NEWS_UID = "3493282298464839"


def normalize_galaxy_news_uid(value: Any) -> str:
    uid = str(value or "").strip()
    if not uid or uid == LEGACY_GALAXY_NEWS_UID:
        return GALAXY_NEWS_UID
    return uid


def _normalize_image_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


@dataclass(frozen=True)
class BilibiliPost:
    dynamic_id: str
    author: str
    text: str
    image_urls: tuple[str, ...]

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/opus/{self.dynamic_id}"


class BilibiliRiskControlError(RuntimeError):
    """Bilibili rejected a request that lacks valid browser authentication."""


class BilibiliAuthenticationError(RuntimeError):
    """Bilibili did not accept the configured login cookie."""


MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


def sign_wbi_params(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    *,
    timestamp: int | None = None,
) -> dict[str, Any]:
    raw_key = img_key + sub_key
    if len(raw_key) < 64:
        raise ValueError("invalid WBI key")
    mixin_key = "".join(raw_key[index] for index in MIXIN_KEY_ENC_TAB)[:32]
    signed = dict(params)
    signed["wts"] = int(timestamp if timestamp is not None else time.time())
    filtered = {
        key: "".join(char for char in str(value) if char not in "!'()*")
        for key, value in signed.items()
    }
    query = urlencode(
        sorted(filtered.items()),
        quote_via=quote,
        safe="",
    )
    signed["w_rid"] = hashlib.md5(
        (query + mixin_key).encode("utf-8")
    ).hexdigest()
    return signed


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _append_unique(target: list[str], values: Any) -> None:
    if not isinstance(values, list):
        return
    for item in values:
        if not isinstance(item, dict):
            continue
        url = _normalize_image_url(item.get("src") or item.get("url"))
        if url and url not in target:
            target.append(url)


def _rich_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    direct = str(value.get("text") or value.get("orig_text") or "").strip()
    if direct:
        return direct
    nodes = value.get("rich_text_nodes")
    if not isinstance(nodes, list):
        return ""
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        text = str(node.get("text") or node.get("orig_text") or "")
        if text:
            parts.append(text)
    return "".join(parts).strip()


def parse_dynamic(item: dict[str, Any]) -> BilibiliPost | None:
    dynamic_id = str(item.get("id_str") or item.get("id") or "").strip()
    if not dynamic_id:
        return None

    modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
    author = str(_nested(modules, "module_author", "name") or "银河快报").strip()
    dynamic = _nested(modules, "module_dynamic")
    if not isinstance(dynamic, dict):
        dynamic = {}

    texts: list[str] = []
    desc_text = _rich_text(dynamic.get("desc"))
    if desc_text:
        texts.append(desc_text)

    major = dynamic.get("major") if isinstance(dynamic.get("major"), dict) else {}
    opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
    opus_text = _rich_text(opus.get("summary"))
    if opus_text and opus_text not in texts:
        texts.append(opus_text)
    opus_title = str(opus.get("title") or "").strip()
    if opus_title and opus_title not in texts:
        texts.insert(0, opus_title)

    images: list[str] = []
    _append_unique(images, _nested(major, "draw", "items"))
    _append_unique(images, _nested(major, "opus", "pics"))

    original = _nested(item, "orig", "modules", "module_dynamic")
    if isinstance(original, dict):
        original_text = _rich_text(original.get("desc"))
        if original_text and original_text not in texts:
            texts.append(original_text)
        original_major = (
            original.get("major") if isinstance(original.get("major"), dict) else {}
        )
        original_opus = (
            original_major.get("opus")
            if isinstance(original_major.get("opus"), dict)
            else {}
        )
        original_opus_text = _rich_text(original_opus.get("summary"))
        if original_opus_text and original_opus_text not in texts:
            texts.append(original_opus_text)
        _append_unique(images, _nested(original_major, "draw", "items"))
        _append_unique(images, _nested(original_major, "opus", "pics"))

    return BilibiliPost(
        dynamic_id=dynamic_id,
        author=author,
        text="\n".join(texts).strip(),
        image_urls=tuple(images),
    )


def parse_opus_feed_item(item: dict[str, Any]) -> BilibiliPost | None:
    opus_id = str(item.get("opus_id") or "").strip()
    if not opus_id:
        return None
    text = str(item.get("content") or "").strip()
    cover = item.get("cover") if isinstance(item.get("cover"), dict) else {}
    cover_url = _normalize_image_url(cover.get("url"))
    return BilibiliPost(
        dynamic_id=opus_id,
        author="银河快报",
        text=text,
        image_urls=(cover_url,) if cover_url else (),
    )


class BilibiliMonitor:
    OPUS_API_URL = (
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/feed/space"
    )
    API_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    DESKTOP_API_URL = (
        "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/feed/space"
    )
    NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
    FEATURES = (
        "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,"
        "forwardListHidden,decorationCard,commentsNewVersion,"
        "onlyfansAssetsV2,ugcDelete,onlyfansQaCard"
    )
    IMAGE_HOST_SUFFIXES = (".hdslb.com", ".bilibili.com")

    def __init__(
        self,
        data_dir: Path,
        *,
        uid: str,
        keyword: str,
        interval: int,
        cookie: str,
        proxy_url: str,
        timeout: int,
        max_image_bytes: int,
        send_post: Callable[[BilibiliPost, list[Path], list[str]], Awaitable[int | None]],
        log_info: Callable[[str], None],
        log_warning: Callable[[str], None],
        image_cache: Any | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.image_dir = self.data_dir / "images"
        self.state_path = self.data_dir / "state.json"
        self.uid = normalize_galaxy_news_uid(uid)
        self.keyword = str(keyword).strip()
        self.interval = max(60, int(interval))
        self.cookie = str(cookie).strip()
        self.proxy_url = str(proxy_url).strip()
        self.timeout = aiohttp.ClientTimeout(total=max(5, int(timeout)))
        self.max_image_bytes = max(1024, int(max_image_bytes))
        self.send_post = send_post
        self.log_info = log_info
        self.log_warning = log_warning
        self.image_cache = image_cache
        self.sessions: list[str] = []
        self.seen_ids: list[str] = []
        self.pending: dict[str, dict[str, Any]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._risk_warning_logged = False
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_keys_at = 0.0
        self._load_state()

    @property
    def has_login_cookie(self) -> bool:
        return "SESSDATA=" in self.cookie.upper()

    def _load_state(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self.sessions = [str(x) for x in raw.get("sessions", []) if str(x).strip()]
        if str(raw.get("uid") or "") != self.uid:
            self.seen_ids = []
            return
        self.seen_ids = [str(x) for x in raw.get("seen_ids", []) if str(x).strip()][
            :200
        ]
        pending = raw.get("pending", {})
        if isinstance(pending, dict):
            self.pending = {
                str(key): value
                for key, value in pending.items()
            }
            self.prune_pending()

    @staticmethod
    def _pending_post(dynamic_id: str, entry: Any) -> BilibiliPost:
        if not isinstance(entry, dict) or not isinstance(entry.get("sessions"), list):
            raise ValueError("invalid pending entry/recipients")
        raw = entry.get("post")
        if not isinstance(raw, dict):
            raise ValueError("invalid pending post")
        post = BilibiliPost(**raw)
        if (
            not isinstance(post.dynamic_id, str)
            or not post.dynamic_id.strip()
            or post.dynamic_id != dynamic_id
            or not isinstance(post.author, str)
            or not isinstance(post.text, str)
            or not isinstance(post.image_urls, (list, tuple))
            or any(not isinstance(url, str) for url in post.image_urls)
        ):
            raise ValueError("invalid pending post fields")
        return post

    def prune_pending(self) -> None:
        now = time.time()
        for key, entry in list(self.pending.items()):
            try:
                self._pending_post(key, entry)
            except (TypeError, ValueError) as exc:
                self.log_warning(f"[HD2] dropped invalid pending Bilibili post: {key}: {exc}")
                self.pending.pop(key)
                continue
            entry["queued_at"] = queued_timestamp(entry.get("queued_at"), now)
            entry["sessions"][:] = [s for s in entry["sessions"] if s in self.sessions]
            if not entry["sessions"] or now - entry["queued_at"] >= PENDING_TTL_SECONDS:
                if entry["sessions"]:
                    self.log_warning(f"[HD2] dropped expired pending Bilibili post: {key}")
                self.pending.pop(key)
        oldest = sorted(self.pending, key=lambda key: self.pending[key]["queued_at"])
        for key in oldest[:max(0, len(self.pending) - MAX_PENDING_ENTRIES)]:
            self.log_warning(f"[HD2] dropped oldest pending Bilibili post (retention limit): {key}")
            self.pending.pop(key)
        self._save_state()

    def _save_state(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "uid": self.uid,
                    "sessions": self.sessions,
                    "seen_ids": self.seen_ids[:200],
                    "pending": self.pending,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def subscribe(self, unified_msg_origin: str) -> bool:
        value = str(unified_msg_origin).strip()
        if not value or value in self.sessions:
            return False
        self.sessions.append(value)
        self._save_state()
        return True

    def unsubscribe(self, unified_msg_origin: str) -> bool:
        value = str(unified_msg_origin).strip()
        if value not in self.sessions:
            return False
        self.sessions.remove(value)
        for entry in self.pending.values():
            if value in entry["sessions"]:
                entry["sessions"].remove(value)
        self._save_state()
        return True

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="hd2-bilibili-monitor")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self.image_cache is not None:
            await self.image_cache.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://space.bilibili.com",
                "Referer": f"https://space.bilibili.com/{self.uid}/dynamic",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.0.0 Safari/537.36"
                ),
            }
            if self.cookie:
                headers["Cookie"] = self.cookie
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers=headers)
        return self._session

    async def _run(self) -> None:
        while not self._stopping:
            sleep_seconds = self.interval
            try:
                sent = await self.check_once()
                HEALTH.record_ok(
                    "bilibili",
                    detail=f"轮询正常，推送 {sent} 条" if sent else "轮询正常",
                )
                self._risk_warning_logged = False
            except asyncio.CancelledError:
                raise
            except BilibiliRiskControlError as exc:
                HEALTH.record_fail("bilibili", detail=str(exc))
                if not self._risk_warning_logged:
                    self.log_warning(f"[HD2] {exc}")
                    self._risk_warning_logged = True
                sleep_seconds = max(self.interval, 3600)
            except Exception as exc:
                HEALTH.record_fail(
                    "bilibili", detail=f"{type(exc).__name__}: {exc}"
                )
                self.log_warning(
                    f"[HD2] Bilibili monitor error: {type(exc).__name__}: {exc}"
                )
            await asyncio.sleep(sleep_seconds)

    async def _get_wbi_keys(self) -> tuple[str, str]:
        session = await self._get_session()
        if self._wbi_keys is not None and time.monotonic() - self._wbi_keys_at < 300:
            return self._wbi_keys
        kwargs: dict[str, Any] = {}
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        async with session.get(self.NAV_URL, **kwargs) as response:
            if response.status == 412:
                await response.read()
                raise BilibiliRiskControlError(
                    "B站登录状态接口也被风控 (-412)，说明当前 AstrBot 服务器出口 IP "
                    "或 Cookie 会话被限制。请在服务器配置可用代理，或更换网络出口。"
                )
            if response.status != 200:
                body = (await response.text())[:200].replace("\n", " ")
                raise RuntimeError(f"B站登录状态 HTTP {response.status}: {body}")
            payload = await response.json(content_type=None)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("isLogin"):
            raise BilibiliAuthenticationError(
                "B站 Cookie 未通过登录验证，请重新复制浏览器中的完整 Cookie；"
                "需要同时保留 SESSDATA、DedeUserID、buvid3、buvid4 等字段。"
            )
        wbi_img = data.get("wbi_img")
        if not isinstance(wbi_img, dict):
            raise RuntimeError("B站登录状态响应缺少 WBI 密钥")
        img_key = Path(urlparse(str(wbi_img.get("img_url") or "")).path).stem
        sub_key = Path(urlparse(str(wbi_img.get("sub_url") or "")).path).stem
        if len(img_key) != 32 or len(sub_key) != 32:
            raise RuntimeError("B站登录状态响应中的 WBI 密钥无效")
        self._wbi_keys = (img_key, sub_key)
        self._wbi_keys_at = time.monotonic()
        return self._wbi_keys

    async def fetch_posts(self) -> list[BilibiliPost]:
        session = await self._get_session()
        img_key, sub_key = await self._get_wbi_keys()
        opus_params = sign_wbi_params(
            {
                "host_mid": self.uid,
                "page": 1,
                "type": "dynamic",
                "web_location": "333.1387",
            },
            img_key,
            sub_key,
        )
        opus_kwargs: dict[str, Any] = {"params": opus_params}
        if self.proxy_url:
            opus_kwargs["proxy"] = self.proxy_url
        async with session.get(self.OPUS_API_URL, **opus_kwargs) as response:
            if response.status == 200:
                opus_payload = await response.json(content_type=None)
                opus_items = _nested(opus_payload, "data", "items")
                if isinstance(opus_items, list):
                    opus_posts = [
                        parse_opus_feed_item(item)
                        for item in opus_items
                        if isinstance(item, dict)
                    ]
                    parsed_opus_posts = [
                        post for post in opus_posts if post is not None
                    ]
                    if parsed_opus_posts:
                        return parsed_opus_posts
            else:
                await response.read()

        params = sign_wbi_params(
            {
                "host_mid": self.uid,
                "timezone_offset": -480,
                "platform": "web",
                "features": self.FEATURES,
                "web_location": "333.1387",
                "dm_img_switch": 0,
                "x-bili-device-req-json": json.dumps(
                    {"platform": "web", "device": "pc"}, separators=(",", ":")
                ),
                "x-bili-web-req-json": json.dumps(
                    {"spm_id": "333.1387"}, separators=(",", ":")
                ),
            },
            img_key,
            sub_key,
        )
        blocked_endpoints: list[str] = []
        payload: Any = None
        for endpoint in (self.API_URL, self.DESKTOP_API_URL):
            kwargs: dict[str, Any] = {"params": params}
            if self.proxy_url:
                kwargs["proxy"] = self.proxy_url
            async with session.get(endpoint, **kwargs) as response:
                if response.status == 412:
                    await response.read()
                    blocked_endpoints.append(endpoint)
                    continue
                if response.status != 200:
                    body = (await response.text())[:200].replace("\n", " ")
                    raise RuntimeError(f"HTTP {response.status}: {body}")
                payload = await response.json(content_type=None)
            if isinstance(payload, dict) and payload.get("code") == -412:
                blocked_endpoints.append(endpoint)
                continue
            break
        if len(blocked_endpoints) == 2:
            raise BilibiliRiskControlError(
                "B站账号登录验证成功，但标准与 desktop 动态接口均返回 -412。"
                "这通常是 AstrBot 服务器出口 IP 被限制，请配置 proxy_url 更换出口。"
            )
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise RuntimeError(
                f"API code={payload.get('code')} message={payload.get('message')}"
            )
        items = _nested(payload, "data", "items")
        if not isinstance(items, list):
            raise RuntimeError("API response has no dynamic items")
        posts = [parse_dynamic(item) for item in items if isinstance(item, dict)]
        return [post for post in posts if post is not None]

    def _risk_control_message(self) -> str:
        if self.has_login_cookie:
            return (
                "B站动态请求被风控 (-412)：当前 Cookie 可能已过期，或 AstrBot "
                "服务器 IP 被限制。请更新包含 SESSDATA 的 Cookie 后重载插件。"
            )
        return (
            "B站动态请求被风控 (-412)：请在 bilibili_cookie 中填写浏览器登录后"
            "的完整 B站 Cookie（必须包含 SESSDATA），然后重载插件。后台将在 "
            "1 小时后再试，避免持续触发风控。"
        )

    async def check_once(self) -> int:
        try:
            posts = await self.fetch_posts()
        except Exception:
            # A feed outage must not block retrying already queued posts.
            await self._deliver_pending()
            raise
        current_ids = [post.dynamic_id for post in posts]
        if not self.seen_ids and not self.pending:
            self.seen_ids = current_ids[:200]
            self._save_state()
            self.log_info(
                f"[HD2] Bilibili monitor baseline initialized: {len(current_ids)}"
            )
            return 0

        known = set(self.seen_ids)
        new_posts = [post for post in reversed(posts) if post.dynamic_id not in known]
        for post in new_posts:
            if self.matches(post) and self.sessions and post.dynamic_id not in self.pending:
                self.pending[post.dynamic_id] = {
                    "post": asdict(post),
                    "sessions": list(self.sessions),
                    "queued_at": time.time(),
                }
        # Seen means observed/enqueued; delivery is tracked independently.
        self.seen_ids = list(dict.fromkeys(current_ids + self.seen_ids))[:200]
        self._save_state()
        return await self._deliver_pending()

    async def _deliver_pending(self) -> int:
        self.prune_pending()
        sent = 0
        for dynamic_id, entry in list(self.pending.items()):
            try:
                post = self._pending_post(dynamic_id, entry)
            except (TypeError, ValueError) as exc:
                self.log_warning(
                    f"[HD2] dropped invalid pending Bilibili post: {dynamic_id}: {exc}"
                )
                self.pending.pop(dynamic_id, None)
                self._save_state()
                continue
            remaining = [s for s in entry["sessions"] if s in self.sessions]
            entry["sessions"] = remaining
            if remaining:
                try:
                    paths = await self.download_images(post)
                except Exception as exc:
                    self.log_warning(f"[HD2] Bilibili pending images failed: {exc}")
                    continue
                delivered = False
                for session in list(remaining):
                    if session not in remaining or session not in self.sessions:
                        continue
                    try:
                        count = await self.send_post(post, paths, [session])
                    except Exception as exc:
                        self.log_warning(
                            f"[HD2] Bilibili pending push failed: {session}: {exc}"
                        )
                        continue
                    # Legacy callbacks return None on success; current ones return 1.
                    if count is not None and count <= 0:
                        continue
                    if session in remaining:
                        remaining.remove(session)
                    self._save_state()
                    delivered = True
                sent += int(delivered)
            if not remaining:
                self.pending.pop(dynamic_id, None)
                self._save_state()
        return sent

    def matches(self, post: BilibiliPost) -> bool:
        return self.keyword.casefold() in post.text.casefold()

    def latest_matching(self, posts: list[BilibiliPost]) -> BilibiliPost | None:
        return next((post for post in posts if self.matches(post)), None)

    @classmethod
    def _allowed_image_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(
            host == suffix[1:] or host.endswith(suffix)
            for suffix in cls.IMAGE_HOST_SUFFIXES
        )

    async def download_images(self, post: BilibiliPost) -> list[Path]:
        if self.image_cache is not None:
            paths: list[Path] = []
            for url in post.image_urls:
                if not self._allowed_image_url(url):
                    self.log_warning(
                        f"[HD2] skipped unexpected Bilibili image URL: {url}"
                    )
                    continue
                try:
                    path = await self.image_cache.get(url, raise_errors=True)
                except Exception as exc:
                    self.log_warning(
                        f"[HD2] Bilibili image cache rejected {url}: {exc}"
                    )
                    continue
                if path is not None and path.is_file() and path.stat().st_size > 0:
                    paths.append(path)
                elif path is not None:
                    self.log_warning(
                        f"[HD2] Bilibili image missing or empty on disk: {path}"
                    )
            return paths

        self.image_dir.mkdir(parents=True, exist_ok=True)
        session = await self._get_session()
        paths: list[Path] = []
        for index, url in enumerate(post.image_urls):
            if not self._allowed_image_url(url):
                self.log_warning(f"[HD2] skipped unexpected Bilibili image URL: {url}")
                continue
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            suffix = Path(urlparse(url).path).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                suffix = ".jpg"
            # dynamic_id 来自外部 API 响应，剥离路径分隔符/`..`避免路径穿越
            safe_id = re.sub(r"[^0-9a-zA-Z_-]", "", post.dynamic_id) or "unknown"
            path = self.image_dir / f"{safe_id}_{index}_{digest}{suffix}"
            if path.exists() and path.stat().st_size > 0:
                paths.append(path)
                continue
            kwargs: dict[str, Any] = {}
            if self.proxy_url:
                kwargs["proxy"] = self.proxy_url
            async with session.get(url, allow_redirects=False, **kwargs) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if not content_type.startswith("image/"):
                    raise RuntimeError(f"unexpected image content type: {content_type}")
                content_length = int(response.headers.get("Content-Length", "0") or 0)
                if content_length > self.max_image_bytes:
                    raise RuntimeError(f"Bilibili image too large: {content_length} bytes")
                temporary = path.with_suffix(".tmp")
                downloaded = 0
                try:
                    with temporary.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            downloaded += len(chunk)
                            if downloaded > self.max_image_bytes:
                                raise RuntimeError(
                                    "Bilibili image too large: "
                                    f">{self.max_image_bytes} bytes"
                                )
                            handle.write(chunk)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
            temporary.replace(path)
            paths.append(path)
        return paths
