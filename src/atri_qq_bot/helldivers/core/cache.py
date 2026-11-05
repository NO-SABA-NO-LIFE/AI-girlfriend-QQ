"""内存 + 本地 JSON 缓存，带后台刷新。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .._logging import logger

Fetcher = Callable[[], Awaitable[Any]]


def get_plugin_data_dir(plugin_name: str = "astrbot_plugin_Helldivers") -> Path:
    """获取 Helldivers 数据目录（移植版，不再依赖 AstrBot 路径工具）。

    可通过环境变量 ``HD2_DATA_DIR`` 指定根目录；未设置时回退到包内 ``data/``。
    正常运行时适配层会显式传入亚托莉的数据目录，本函数仅作独立运行时的兜底。
    """
    env_root = os.getenv("HD2_DATA_DIR", "").strip()
    if env_root:
        base = Path(env_root)
    else:
        base = Path(__file__).resolve().parent.parent / "data"

    path = base / plugin_name
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class CacheEntry:
    data: Any
    updated_at: float
    name: str


class CacheService:
    """多命名空间缓存：内存热路径 + 磁盘持久化 + 后台轮询。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        update_interval: int = 180,
        request_delay: float = 4.0,
        enable_background: bool = True,
        soft_ttl: int | None = None,
    ) -> None:
        self.cache_dir = data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # 更保守：默认 3 分钟一轮，避免 429
        self.update_interval = max(60, int(update_interval))
        self.request_delay = max(0.0, float(request_delay))
        self.enable_background = enable_background
        # soft_ttl：条目未过期则后台刷新可跳过（默认=update_interval）
        self.soft_ttl = max(
            30, int(soft_ttl if soft_ttl is not None else self.update_interval)
        )

        self._entries: dict[str, CacheEntry] = {}
        self._fetchers: dict[str, Fetcher] = {}
        self._lock = asyncio.Lock()
        self._disk_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._once_task: asyncio.Task | None = None
        self._started = False
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def register(self, name: str, fetcher: Fetcher) -> None:
        self._fetchers[name] = fetcher

    def _disk_path(self, name: str) -> Path:
        safe = name.replace("/", "_").replace("\\", "_")
        return self.cache_dir / f"{safe}.json"

    def load_disk(self) -> None:
        for name in list(self._fetchers.keys()) or []:
            path = self._disk_path(name)
            if not path.exists():
                # 也尝试加载已有磁盘文件（即使尚未 register 完成）
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._entries[name] = CacheEntry(
                    data=raw.get("data"),
                    updated_at=float(raw.get("updated_at") or 0),
                    name=name,
                )
                logger.debug(f"[HD2] loaded disk cache: {name}")
            except Exception as e:
                logger.warning(f"[HD2] load disk cache {name} failed: {e}")

        # 扫描目录中未在上面加载的
        for path in self.cache_dir.glob("*.json"):
            name = path.stem
            if name in self._entries:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._entries[name] = CacheEntry(
                    data=raw.get("data"),
                    updated_at=float(raw.get("updated_at") or 0),
                    name=name,
                )
            except Exception:
                continue

    def _save_disk(self, name: str, entry: CacheEntry) -> None:
        path = self._disk_path(name)
        tmp = path.with_suffix(".tmp")
        payload = {
            "data": entry.data,
            "updated_at": entry.updated_at,
            "name": name,
        }
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[HD2] save disk cache {name} failed: {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def save_all(self) -> None:
        for name, entry in list(self._entries.items()):
            self._save_disk(name, entry)

    async def set(self, name: str, data: Any) -> None:
        entry = CacheEntry(data=data, updated_at=time.time(), name=name)
        async with self._lock:
            self._entries[name] = entry
        async with self._disk_lock:
            await asyncio.to_thread(self._save_disk, name, entry)

    async def get(self, name: str) -> Any | None:
        async with self._lock:
            entry = self._entries.get(name)
            if not entry:
                return None
            return entry.data

    async def get_entry(self, name: str) -> CacheEntry | None:
        async with self._lock:
            return self._entries.get(name)

    async def is_fresh(self, name: str, ttl: int | None = None) -> bool:
        ttl = self.soft_ttl if ttl is None else max(1, int(ttl))
        async with self._lock:
            entry = self._entries.get(name)
            if not entry:
                return False
            return (time.time() - entry.updated_at) < ttl

    async def refresh_one(self, name: str, *, force: bool = False) -> bool:
        fetcher = self._fetchers.get(name)
        if not fetcher:
            return False
        if not force and await self.is_fresh(name):
            logger.debug(f"[HD2] skip refresh {name}: still fresh")
            return True
        try:
            data = await fetcher()
            # None = 请求失败；[] / {} = 合法空结果，应写入缓存
            if data is None:
                logger.warning(f"[HD2] refresh {name}: request failed (None)")
                return False
            await self.set(name, data)
            if isinstance(data, (list, dict)) and len(data) == 0:
                logger.info(f"[HD2] refreshed cache: {name} (empty)")
            else:
                logger.info(f"[HD2] refreshed cache: {name}")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[HD2] refresh {name} failed: {e}")
            return False

    async def refresh_all(self, *, force: bool = False) -> None:
        names = list(self._fetchers.keys())
        for i, name in enumerate(names):
            ok = await self.refresh_one(name, force=force)
            # 失败时多歇一会，减轻 429 连锁
            pause = self.request_delay if ok else max(self.request_delay * 2, 6.0)
            if i < len(names) - 1 and pause > 0:
                await asyncio.sleep(pause)

    async def get_or_refresh(self, name: str) -> Any | None:
        data = await self.get(name)
        if data is not None:
            return data
        # 单飞：冷缓存/上游失败时，并发的同名请求只触发一次抓取，
        # 其余在锁上等结果，避免对同一 key 的请求风暴。
        lock = self._refresh_locks.setdefault(name, asyncio.Lock())
        async with lock:
            data = await self.get(name)
            if data is not None:
                return data
            await self.refresh_one(name, force=True)
            return await self.get(name)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await asyncio.to_thread(self.load_disk)
        if self.enable_background:
            self._task = asyncio.create_task(self._loop(), name="hd2-cache-refresh")
            logger.info(
                f"[HD2] cache background refresh started, interval={self.update_interval}s"
            )
        else:
            # 至少尝试一次填充
            self._once_task = asyncio.create_task(
                self.refresh_all(), name="hd2-cache-once"
            )

    async def stop(self) -> None:
        self._started = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[HD2] cache task stop error: {e}")
        self._task = None
        if self._once_task and not self._once_task.done():
            self._once_task.cancel()
            await asyncio.gather(self._once_task, return_exceptions=True)
        self._once_task = None
        async with self._disk_lock:
            await asyncio.to_thread(self.save_all)

    async def _loop(self) -> None:
        first = True
        while True:
            try:
                if not first:
                    await asyncio.sleep(self.update_interval)
                first = False
                await self.refresh_all()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "[HD2] cache refresh loop error; will retry next cycle"
                )
                await asyncio.sleep(min(30, self.update_interval))
