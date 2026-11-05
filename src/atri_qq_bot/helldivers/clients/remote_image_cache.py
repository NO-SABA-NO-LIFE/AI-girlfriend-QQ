"""Security-focused asynchronous cache for official Steam CDN images.

The cache accepts only exact allowlisted HTTPS hosts, validates DNS answers and every
redirect, streams into a byte limit, verifies MIME and Pillow decoding, enforces a
pixel limit, and stores a normalized PNG with atomic replacement. Failed downloads
receive a short negative-cache entry so repeated renders do not hammer the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import tempfile
import time
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Final
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..core.plugin_info import USER_AGENT

try:
    import aiohttp
except (
    ImportError
):  # pragma: no cover - callers may inject an aiohttp-compatible session
    aiohttp = None  # type: ignore[assignment]

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = OSError  # type: ignore[assignment,misc]

DEFAULT_STEAM_IMAGE_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "clan.steamstatic.com",
        "clan.akamai.steamstatic.com",
        "shared.akamai.steamstatic.com",
        "store.akamai.steamstatic.com",
        "cdn.akamai.steamstatic.com",
        "shared.steamstatic.com",
    }
)
ALLOWED_IMAGE_MIMES: Final[frozenset[str]] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)
_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})


def _ensure_readable(path: Path) -> None:
    """确保缓存图片对跨容器/跨用户的 QQ 机器人可读。

    NamedTemporaryFile 默认 0600，NapCat 等外部进程无权读取时 QQ 会报
    rich media transfer failed。将权限统一修为 0o644。
    """
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


class RemoteImageError(RuntimeError):
    """Raised when a remote image fails validation or download."""


class _NegativeCacheHit(RemoteImageError):
    """Internal signal that must not refresh the negative-cache timestamp."""


@dataclass(frozen=True, slots=True)
class RemoteImageRecord:
    """Successful cache lookup details."""

    path: Path
    url: str
    from_cache: bool
    width: int
    height: int
    mode: str


class RemoteImageCache:
    """An aiohttp-compatible async image cache with SSRF and image-bomb defenses."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        allowed_hosts: set[str] | frozenset[str] = DEFAULT_STEAM_IMAGE_HOSTS,
        max_bytes: int = 8 * 1024 * 1024,
        max_pixels: int = 16_000_000,
        cache_ttl: int = 30 * 24 * 3600,
        negative_ttl: int = 15 * 60,
        timeout: float = 20.0,
        max_redirects: int = 4,
        max_cache_bytes: int = 150 * 1024 * 1024,
        user_agent: str = USER_AGENT,
        proxy_url: str | None = None,
        session: Any | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_hosts = frozenset(
            str(host).strip().rstrip(".").lower()
            for host in allowed_hosts
            if str(host).strip()
        )
        if not self.allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        self.max_bytes = max(1024, int(max_bytes))
        self.max_pixels = max(1, int(max_pixels))
        self.cache_ttl = max(0, int(cache_ttl))
        self.negative_ttl = max(0, int(negative_ttl))
        self.timeout_seconds = max(1.0, float(timeout))
        self.max_redirects = max(0, int(max_redirects))
        self.max_cache_bytes = max(1024 * 1024, int(max_cache_bytes))
        self.user_agent = str(user_agent or USER_AGENT)
        self.proxy_url = str(proxy_url or "").strip() or None
        self.request_headers = {
            str(key): str(value)
            for key, value in (request_headers or {}).items()
            if str(key).strip() and str(value).strip()
        }
        self._session = session
        self._own_session = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_refs: dict[str, int] = {}

    @property
    def available(self) -> bool:
        return Image is not None and (aiohttp is not None or self._session is not None)

    def bind_session(self, session: Any | None) -> None:
        """Use a caller-owned aiohttp-compatible session."""

        self._session = session
        self._own_session = False

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            closed = bool(getattr(self._session, "closed", False))
            if not closed:
                await self._session.close()
        if self._own_session:
            self._session = None
            self._own_session = False

    async def __aenter__(self) -> "RemoteImageCache":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    def cached_path(self, url: str) -> Path | None:
        """Return a valid positive cache hit without performing network I/O."""

        try:
            canonical = self.validate_url(url)
        except RemoteImageError:
            return None
        key = self._cache_key(canonical)
        image_path, metadata_path, _ = self._paths(key)
        metadata = self._read_json(metadata_path)
        if not image_path.is_file() or not metadata:
            return None
        if (
            self.cache_ttl
            and time.time() - float(metadata.get("fetched_at") or 0) > self.cache_ttl
        ):
            return None
        return image_path

    async def get(
        self,
        url: str,
        *,
        force: bool = False,
        raise_errors: bool = False,
    ) -> Path | None:
        """Return a normalized cached PNG path, downloading when required."""

        record = await self.get_record(url, force=force, raise_errors=raise_errors)
        return record.path if record else None

    async def fetch(
        self,
        url: str,
        *,
        force: bool = False,
        raise_errors: bool = False,
    ) -> Path | None:
        """Alias for :meth:`get`, convenient for cache/client integrations."""

        return await self.get(url, force=force, raise_errors=raise_errors)

    async def get_image(
        self,
        url: str,
        *,
        force: bool = False,
        raise_errors: bool = False,
    ) -> Any | None:
        """Return a detached normalized Pillow image, or ``None`` on failure."""

        path = await self.get(url, force=force, raise_errors=raise_errors)
        if path is None or Image is None:
            return None
        with Image.open(path) as source:
            image = source.copy()
            image.load()
        return image

    async def get_record(
        self,
        url: str,
        *,
        force: bool = False,
        raise_errors: bool = False,
    ) -> RemoteImageRecord | None:
        canonical = ""
        try:
            canonical = self.validate_url(url)
            key = self._cache_key(canonical)
            # 引用计数在等待锁之前累加：持锁者退出时仅当无等待者才回收，
            # 防止后到者拿到新锁对象与旧持锁者并发进入临界区。
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
                self._lock_refs[key] = 0
            self._lock_refs[key] = self._lock_refs.get(key, 0) + 1
            try:
                # single-flight：同 key 并发请求只放一个进临界区，其余等结果
                async with lock:
                    return await self._get_record_locked(canonical, key, force=force)
            finally:
                self._lock_refs[key] -= 1
                if self._lock_refs[key] <= 0:
                    self._lock_refs.pop(key, None)
                    if self._locks.get(key) is lock and not lock.locked():
                        self._locks.pop(key, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = (
                exc if isinstance(exc, RemoteImageError) else RemoteImageError(str(exc))
            )
            if canonical and not isinstance(exc, _NegativeCacheHit):
                key = self._cache_key(canonical)
                _, _, negative_path = self._paths(key)
                self._write_json_atomic(
                    negative_path,
                    {
                        "failed_at": time.time(),
                        "url": canonical,
                        "error": str(error)[:300],
                    },
                )
            if raise_errors:
                raise error from exc
            return None

    def validate_url(self, url: str) -> str:
        """Validate and canonicalize a URL before DNS or HTTP access."""

        raw = str(url or "").strip()
        try:
            parts = urlsplit(raw)
            port = parts.port
        except ValueError as exc:
            raise RemoteImageError("invalid image URL") from exc
        host = (parts.hostname or "").rstrip(".").lower()
        if parts.scheme.lower() != "https":
            raise RemoteImageError("only HTTPS image URLs are allowed")
        if not host or host not in self.allowed_hosts:
            raise RemoteImageError("image host is not allowlisted")
        if parts.username is not None or parts.password is not None:
            raise RemoteImageError("userinfo is not allowed in image URLs")
        if port not in (None, 443):
            raise RemoteImageError("non-standard HTTPS ports are not allowed")
        if not parts.path.startswith("/") or "\x00" in raw:
            raise RemoteImageError("invalid image URL path")
        return urlunsplit(("https", host, parts.path, parts.query, ""))

    async def validate_dns(self, url_or_host: str) -> tuple[str, ...]:
        """Resolve a host and reject non-global/private network destinations."""

        host = (urlsplit(url_or_host).hostname or url_or_host).rstrip(".").lower()
        if host not in self.allowed_hosts:
            raise RemoteImageError("image host is not allowlisted")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            addresses = {literal}
        else:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(
                    host,
                    443,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            except OSError as exc:
                raise RemoteImageError(f"DNS resolution failed for {host}") from exc
            addresses = set()
            for info in infos:
                try:
                    addresses.add(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
                except (ValueError, IndexError):
                    continue
        if not addresses:
            raise RemoteImageError(f"DNS returned no addresses for {host}")
        unsafe = [str(address) for address in addresses if not address.is_global]
        if unsafe:
            raise RemoteImageError(f"DNS resolved to a non-public address: {unsafe[0]}")
        return tuple(sorted(str(address) for address in addresses))

    async def _get_record_locked(
        self,
        canonical: str,
        key: str,
        *,
        force: bool,
    ) -> RemoteImageRecord:
        image_path, metadata_path, negative_path = self._paths(key)
        metadata = self._read_json(metadata_path)
        now = time.time()
        if not force and image_path.is_file() and metadata:
            age = now - float(metadata.get("fetched_at") or 0)
            # TTL=0 统一表示「永不过期」（与 cached_path() 快路径保持一致）
            if self.cache_ttl == 0 or age <= self.cache_ttl:
                # 旧缓存可能是未压缩的 PNG（可达 33MB），QQ 发不出去。
                # 文件超限时强制重新下载并用 JPEG 重新编码。
                cached_bytes = int(metadata.get("bytes") or 0)
                if cached_bytes > self.max_bytes:
                    pass  # 落入下面的重新下载逻辑
                else:
                    _ensure_readable(image_path)
                    return self._record(image_path, metadata, from_cache=True)
        if not force and negative_path.is_file():
            negative = self._read_json(negative_path)
            age = now - float((negative or {}).get("failed_at") or 0)
            if negative and age <= self.negative_ttl:
                raise _NegativeCacheHit(
                    str(negative.get("error") or "negative cache hit")
                )

        response = await self._download(canonical, metadata)
        if response is None:
            if image_path.is_file() and metadata:
                metadata["fetched_at"] = now
                negative_path.unlink(missing_ok=True)
                # 304 Not Modified：服务器内容没变，但如果本地缓存是
                # 旧版大体积 PNG（bytes > max_bytes），需要从磁盘读取
                # 重新编码为 JPEG，否则 QQ 发不出去。
                cached_bytes = int(metadata.get("bytes") or 0)
                if cached_bytes > self.max_bytes:
                    try:
                        body = image_path.read_bytes()
                        normalized, width, height, fmt = (
                            await asyncio.to_thread(
                                self._normalize_image, body
                            )
                        )
                        ext = ".jpg" if fmt == "JPEG" else ".png"
                        new_path = self.cache_dir / f"{key}{ext}"
                        if new_path != image_path and image_path.is_file():
                            image_path.unlink(missing_ok=True)
                        image_path = new_path
                        self._write_bytes_atomic(image_path, normalized)
                        metadata.update(
                            {
                                "format": fmt,
                                "mode": fmt,
                                "bytes": len(normalized),
                                "width": width,
                                "height": height,
                                "fetched_at": now,
                            }
                        )
                        negative_path.unlink(missing_ok=True)
                    except Exception:
                        pass  # 重新编码失败则退回旧文件
                _ensure_readable(image_path)
                self._write_json_atomic(metadata_path, metadata)
                return self._record(image_path, metadata, from_cache=True)
            raise RemoteImageError(
                "server returned not-modified without a cached image"
            )

        body, final_url, mime, etag, last_modified = response
        normalized, width, height, fmt = await asyncio.to_thread(
            self._normalize_image, body
        )
        # 根据实际格式确定文件扩展名
        ext = ".jpg" if fmt == "JPEG" else ".png"
        new_image_path = self.cache_dir / f"{key}{ext}"
        # 如果旧格式文件存在（比如之前是 .png，现在改 .jpg），先删除
        if new_image_path != image_path and image_path.is_file():
            image_path.unlink(missing_ok=True)
        image_path = new_image_path
        self._write_bytes_atomic(image_path, normalized)
        stored = {
            "url": canonical,
            "final_url": final_url,
            "fetched_at": now,
            "mime": mime,
            "format": fmt,
            "etag": etag,
            "last_modified": last_modified,
            "width": width,
            "height": height,
            "mode": fmt,
            "bytes": len(normalized),
        }
        self._write_json_atomic(metadata_path, stored)
        negative_path.unlink(missing_ok=True)
        self._prune_cache(image_path)
        return self._record(image_path, stored, from_cache=False)

    async def _ensure_session(self) -> Any:
        if self._session is not None and not bool(
            getattr(self._session, "closed", False)
        ):
            return self._session
        if aiohttp is None:
            raise RemoteImageError(
                "aiohttp is required unless a compatible session is injected"
            )
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout, trust_env=False)
        self._own_session = True
        return self._session

    async def _download(
        self,
        initial_url: str,
        metadata: dict[str, Any] | None,
    ) -> tuple[bytes, str, str, str, str] | None:
        session = await self._ensure_session()
        current = initial_url
        headers = {
            "Accept": ", ".join(sorted(ALLOWED_IMAGE_MIMES)),
            "User-Agent": self.user_agent,
        }
        headers.update(self.request_headers)
        if metadata and str(metadata.get("url") or "") == initial_url:
            if metadata.get("etag"):
                headers["If-None-Match"] = str(metadata["etag"])
            if metadata.get("last_modified"):
                headers["If-Modified-Since"] = str(metadata["last_modified"])

        for redirect_count in range(self.max_redirects + 1):
            current = self.validate_url(current)
            await self.validate_dns(current)
            request_kwargs: dict[str, Any] = {
                "headers": headers,
                "allow_redirects": False,
            }
            if self.proxy_url:
                request_kwargs["proxy"] = self.proxy_url
            if aiohttp is not None:
                request_kwargs["timeout"] = aiohttp.ClientTimeout(
                    total=self.timeout_seconds
                )
            async with session.get(current, **request_kwargs) as response:
                if not self.proxy_url:
                    self._validate_peer_address(response)
                status = int(response.status)
                if status == 304:
                    return None
                if status in _REDIRECT_STATUSES:
                    if redirect_count >= self.max_redirects:
                        raise RemoteImageError("too many image redirects")
                    location = str(response.headers.get("Location") or "").strip()
                    if not location:
                        raise RemoteImageError("image redirect has no Location header")
                    current = self.validate_url(urljoin(current, location))
                    headers.pop("If-None-Match", None)
                    headers.pop("If-Modified-Since", None)
                    continue
                if status != 200:
                    raise RemoteImageError(f"image request returned HTTP {status}")
                mime = (
                    str(response.headers.get("Content-Type") or "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if mime not in ALLOWED_IMAGE_MIMES:
                    raise RemoteImageError(
                        f"disallowed image MIME type: {mime or 'missing'}"
                    )
                length_header = response.headers.get("Content-Length")
                if length_header:
                    try:
                        if int(length_header) > self.max_bytes:
                            raise RemoteImageError("remote image exceeds byte limit")
                    except ValueError:
                        raise RemoteImageError(
                            "invalid Content-Length header"
                        ) from None
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    received += len(chunk)
                    if received > self.max_bytes:
                        raise RemoteImageError("remote image exceeds byte limit")
                    chunks.append(bytes(chunk))
                if not chunks:
                    raise RemoteImageError("remote image body is empty")
                return (
                    b"".join(chunks),
                    current,
                    mime,
                    str(response.headers.get("ETag") or ""),
                    str(response.headers.get("Last-Modified") or ""),
                )
        raise RemoteImageError("image redirect processing failed")

    @staticmethod
    def _validate_peer_address(response: Any) -> None:
        connection = getattr(response, "connection", None)
        transport = getattr(connection, "transport", None)
        if transport is None:
            return
        peer = transport.get_extra_info("peername")
        if not peer:
            return
        try:
            address = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
        except (ValueError, IndexError, TypeError) as exc:
            raise RemoteImageError("could not validate image peer address") from exc
        if not address.is_global:
            raise RemoteImageError("image connection used a non-public peer address")

    def _normalize_image(self, body: bytes) -> tuple[bytes, int, int, str]:
        if Image is None:
            raise RemoteImageError("Pillow is required for remote image verification")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(body)) as probe:
                    width, height = probe.size
                    if width <= 0 or height <= 0 or width * height > self.max_pixels:
                        raise RemoteImageError("remote image exceeds pixel limit")
                    probe.verify()
                with Image.open(BytesIO(body)) as source:
                    width, height = source.size
                    if width * height > self.max_pixels:
                        raise RemoteImageError("remote image exceeds pixel limit")
                    source.load()
                    has_alpha = (
                        source.mode in {"RGBA", "LA"} or "transparency" in source.info
                    )
                    mode = "RGBA" if has_alpha else "RGB"
                    normalized = source.convert(mode)
                    output: BytesIO | None = None
                    fmt = "PNG"
                    # 有透明通道 → PNG；否则优先 JPEG（照片类图片体积小 10-50 倍）。
                    # PNG 无损无压缩，32M 像素的照片可达 33MB，QQ 发不出去。
                    if not has_alpha:
                        output = BytesIO()
                        normalized.save(output, format="JPEG", quality=90)
                        fmt = "JPEG"
                    # JPEG 仍超 max_bytes → 逐步降质量
                    if output is not None and len(output.getvalue()) > self.max_bytes:
                        for quality in (80, 70, 60, 50):
                            output = BytesIO()
                            normalized.save(output, format="JPEG", quality=quality)
                            if len(output.getvalue()) <= self.max_bytes:
                                break
                    if output is None:
                        output = BytesIO()
                        normalized.save(output, format="PNG", optimize=True)
                    normalized.close()
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ) as exc:
            if isinstance(exc, RemoteImageError):
                raise
            raise RemoteImageError("remote body is not a safe supported image") from exc
        return output.getvalue(), width, height, fmt

    def _prune_cache(self, protected: Path | None = None) -> None:
        """按最久未修改顺序清理远程图片，保持磁盘缓存上限。"""
        try:
            images = sorted(
                list(self.cache_dir.glob("*.png"))
                + list(self.cache_dir.glob("*.jpg")),
                key=lambda path: path.stat().st_mtime,
            )
            total = sum(path.stat().st_size for path in images)
            for path in images:
                if total <= self.max_cache_bytes:
                    break
                if protected is not None and path == protected:
                    continue
                size = path.stat().st_size
                stem = path.stem
                path.unlink(missing_ok=True)
                (self.cache_dir / f"{stem}.json").unlink(missing_ok=True)
                total -= size
        except OSError:
            pass

    @staticmethod
    def _cache_key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _paths(self, key: str) -> tuple[Path, Path, Path]:
        """返回 (图片路径, metadata 路径, negative 路径)。

        图片文件优先匹配已存在的扩展名（.png 或 .jpg），没有则默认 .png。
        """
        metadata_path = self.cache_dir / f"{key}.json"
        # 查找已有的缓存文件（可能是 .png 或 .jpg）
        for ext in (".png", ".jpg"):
            candidate = self.cache_dir / f"{key}{ext}"
            if candidate.is_file():
                return (candidate, metadata_path, self.cache_dir / f"{key}.negative.json")
        return (
            self.cache_dir / f"{key}.png",
            metadata_path,
            self.cache_dir / f"{key}.negative.json",
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _record(
        path: Path, metadata: dict[str, Any], *, from_cache: bool
    ) -> RemoteImageRecord:
        return RemoteImageRecord(
            path=path,
            url=str(metadata.get("final_url") or metadata.get("url") or ""),
            from_cache=from_cache,
            width=max(0, int(metadata.get("width") or 0)),
            height=max(0, int(metadata.get("height") or 0)),
            mode=str(metadata.get("mode") or "RGB"),
        )

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.stem}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            # NamedTemporaryFile 0600 权限会导致跨容器/跨用户的 QQ 机器人
            # (如 NapCat) 无法读取，报 rich media transfer failed。
            # 统一设为 0o644，与截图等其他图片写入方式保持一致。
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    @classmethod
    def _write_json_atomic(cls, path: Path, value: dict[str, Any]) -> None:
        cls._write_bytes_atomic(
            path,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
        )


__all__ = [
    "ALLOWED_IMAGE_MIMES",
    "DEFAULT_STEAM_IMAGE_HOSTS",
    "RemoteImageCache",
    "RemoteImageError",
    "RemoteImageRecord",
]
