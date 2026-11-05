"""第三方翻译 API + 本地翻译缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import aiohttp
from .._logging import logger

from ..core.diagnostics import HEALTH
from ..core.plugin_info import USER_AGENT


class TranslationService:
    """调用外部翻译接口，失败回退原文。"""

    def __init__(
        self,
        *,
        api_url: str,
        timeout: int = 30,
        target_lang: str = "zh",
        user_agent: str = USER_AGENT,
        proxy_url: str = "",
        enabled: bool = True,
        session: aiohttp.ClientSession | None = None,
        mode: str = "builtin",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        stream: bool = False,
        fallback_ttl: int = 900,
        error_cooldown: int = 60,
        max_tokens: int = 0,
        min_interval: float = 0,
    ) -> None:
        self.api_url = (api_url or "").strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.target_lang = target_lang or "zh"
        self.user_agent = user_agent
        self.proxy_url = proxy_url or None
        self.enabled = enabled
        self._session = session
        self._own_session = False
        # Use a Semaphore (not Lock) so multiple batches can run concurrently
        # while still limiting total parallel API calls to avoid rate-limiting.
        # A Lock serializes everything — with 96 fragments in 6 batches, that
        # means 6 sequential ~10s API calls = 60s, far exceeding any budget.
        self._openai_semaphore = asyncio.Semaphore(3)
        requested_mode = (mode or "auto").strip().lower()
        if requested_mode not in {"builtin", "openai", "auto"}:
            logger.warning(
                "[HD2] unknown translation_mode=%s; using auto mode", requested_mode
            )
            requested_mode = "auto"
        self.requested_mode = requested_mode
        self.api_key = (api_key or "").strip()
        self.model = (model or "gpt-4o-mini").strip()
        self.stream = bool(stream)
        self.fallback_ttl = max(60, int(fallback_ttl))
        self.error_cooldown = max(0, int(error_cooldown or 0))
        # Cap the model's output length to stop free-tier LLMs from "running
        # away" — without this, a batch of 8 short phrases can balloon into
        # 1000-4000 tokens of rambling JSON/explanation, taking 30-90s and
        # tripping every timeout in the pipeline.  0 means "do not send".
        self.max_tokens = max(0, int(max_tokens or 0))
        # Client-side pacing for tight per-minute quotas (e.g. 5 req/min):
        # space requests at least this many seconds apart so we never trip
        # the provider's 429.  0 disables pacing.
        self.min_interval = max(0.0, float(min_interval or 0))
        self._next_request_at = 0.0
        self._openai_cooldown_until = 0.0
        # 请求级失败（超时/网络异常）的瞬态窗口：与 429/5xx 冷却不同，
        # 这些失败不代表配额耗尽，但同样不应被上层当成"翻不动"写负缓存。
        self.transient_failure_window = 60.0
        self._transient_failure_until = 0.0
        self._last_cooldown_log = 0.0
        self.mode = self._resolve_mode()

    def in_cooldown(self) -> bool:
        """Whether the openai-compatible backend is currently rate-limiting."""

        return (
            self.mode == "openai"
            and time.monotonic() < self._openai_cooldown_until
        )

    def in_transient_failure(self) -> bool:
        """Whether a request-level failure (timeout/network) happened recently."""

        return time.monotonic() < self._transient_failure_until

    def _resolve_mode(self) -> str:
        if self.requested_mode != "auto":
            return self.requested_mode
            return self.requested_mode
        path = urlsplit(self.api_url).path.lower()
        if (
            path.endswith("/chat/completions")
            or path.endswith("/v1")
            or "/openai" in path
            or "/oneapi" in path
            or path in {"", "/"}
        ):
            return "openai"
        return "builtin"

    def provider_fingerprint(self) -> str:
        parts = (
            # 协议号随提示词的实质性改版递增：指纹变化会使旧翻译缓存全部
            # 失效，强制用新提示词重新翻译。v4 = 机翻腔对照示例 + 语序重组
            # 规则（此前用户看到的始终是缓存里的旧提示词译文）。
            "translation-protocol-v4-hd2-localization",
            self.mode,
            urlsplit(self.api_url).scheme.lower(),
            urlsplit(self.api_url).netloc.lower(),
            urlsplit(self.api_url).path.rstrip("/").lower(),
            self.model if self.mode == "openai" else "",
            self.target_lang,
        )
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def diagnostic(self) -> dict[str, Any]:
        parsed = urlsplit(self.api_url)
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "requested_mode": self.requested_mode,
            "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else "unset",
            "model": self.model,
            "stream": self.stream,
            "max_tokens": self.max_tokens,
            "min_interval": self.min_interval,
            "key_present": bool(self.api_key),
            "target_lang": self.target_lang,
            "error_cooldown": self.error_cooldown,
        }

    def bind_session(self, session: aiohttp.ClientSession | None) -> None:
        self._session = session
        self._own_session = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
        if self._own_session:
            self._session = None
            self._own_session = False

    def _chat_url(self) -> str:
        """openai 模式：允许填 base 或完整 chat 地址。"""
        return self._chat_urls()[0]

    def _chat_urls(self) -> list[str]:
        """Return likely OpenAI-compatible chat endpoints for permissive proxies."""
        url = self.api_url.rstrip("/")
        parsed = urlsplit(url)
        root = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
        path = parsed.path.rstrip("/").lower()
        candidates: list[str] = []
        if url.endswith("/chat/completions"):
            candidates.append(url)
        elif url.endswith("/v1"):
            candidates.append(url + "/chat/completions")
            if root:
                candidates.append(root + "/chat/completions")
        elif path in {"", "/"}:
            candidates.append(url + "/v1/chat/completions")
            candidates.append(url + "/chat/completions")
        else:
            candidates.append(url + "/chat/completions")
            if root:
                candidates.append(root + "/v1/chat/completions")
        deduped: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped or [url]

    @staticmethod
    def _localization_system_prompt(
        lang_name: str,
        *,
        batch: bool,
    ) -> str:
        prompt = (
            "你是《绝地潜兵2》简体中文本地化编辑，为超级地球真理部改写战时新闻与战场简报。"
            f"把英文游戏文本改写成自然流畅的{lang_name}，"
            "像中文母语军官写的播报稿，坚决消除机翻腔。\n"
            "改写规则：\n"
            "1. 输入中已内嵌的中文专有名词（星球名、阵营名、战备名等）是官方译名，"
            "直接沿用；整句必须按中文语序重新组织，不要沿英文语序逐词替换。\n"
            "2. 核心术语：Helldivers→绝地潜兵，Super Earth→超级地球，"
            "Managed Democracy→管理式民主，Ministry of Truth→真理部，"
            "Major Order→最高指令，Democracy Space Station/DSS→民主空间站/DSS，"
            "Terminids→终结族，Automatons→机器人，Illuminate→光能者，"
            "Defense Campaigns→防御战役，Liberation Campaigns→解放战役，"
            "Voteless→无票者，SEAF→超级地球武装部队，Galactic War→银河战争，"
            "Stratagem→战略配备，Hellpod→地狱舱，Eagle→飞鹰，Orbital→轨道，"
            "Reinforce→增援，Extract→撤离。"
            "其余专名简洁音译，罗马数字（II/III）保留。\n"
            "3. 译感：多用主动句和短句，军报节奏；"
            "删掉直译冠词（\"一个\"\"一种\"），"
            "不用\"被\"\"进行\"\"性的\"等翻译腔；UI 标签≤6字。\n"
            "4. 数字、百分比、日期、倒计时、ID、单位和原有换行原样保留，"
            "不添加原文没有的解释。\n"
            "译感对照（✗机翻 → ✓地道）：\n"
            "✗ 超级地球的公民们被提醒要保持警惕\n"
            "✓ 超级地球提醒各位公民保持警惕\n"
            "✗ 一个针对终结族的新的解放战役已经被启动\n"
            "✓ 针对终结族的新一轮解放战役已经打响"
        )
        if batch:
            return (
                prompt
                + "\n输入是 JSON 字符串数组，来自同一页面的可见文本，"
                "相邻条目可能是同一段落的上下文，可据此把握语境；"
                "但每条译文必须独立成立。必须返回 JSON 对象 "
                + '{"translations":["译文1","译文2"]}。'
                + "译文数组长度和顺序必须与输入完全一致，"
                + "不要使用 markdown 代码块，不要输出其他内容。"
                + '示例 输入: ["JUST NOW","Defense of Vernen Wells"]'
                + ' 示例 输出: {"translations":["刚刚","防御佛农井"]}'
            )
        return prompt + "\n只返回译文本身，不要解释、不要引号、不要 markdown。"

    async def translate_text(self, text: str, to_lang: str | None = None) -> str:
        if not text or not text.strip() or len(text.strip()) < 3:
            return text or ""
        if not self.enabled or not self.api_url:
            return text
        if self.mode == "openai":
            return await self._translate_openai(text, to_lang)
        return await self._translate_builtin(text, to_lang)

    async def translate_texts(
        self,
        texts: list[str],
        to_lang: str | None = None,
    ) -> list[str]:
        """Translate several independent fragments with one provider request."""
        sources = [str(text or "") for text in texts]
        if not sources or not self.enabled or not self.api_url:
            return sources
        if len(sources) == 1:
            return [await self.translate_text(sources[0], to_lang)]
        if self.mode == "openai":
            return await self._translate_openai_batch(sources, to_lang)
        return await self._translate_builtin_batch(sources, to_lang)

    async def _translate_builtin_batch(
        self,
        texts: list[str],
        to_lang: str | None,
    ) -> list[str]:
        marked = "\n".join(
            f"[[HD2_{index}]] {text.strip()}" for index, text in enumerate(texts)
        )
        translated = await self._translate_builtin(marked, to_lang)
        if not translated or translated == marked:
            return texts
        results: list[str] = []
        for index in range(len(texts)):
            start = f"[[HD2_{index}]]"
            end = f"[[HD2_{index + 1}]]"
            if start not in translated:
                return texts
            value = translated.split(start, 1)[1]
            if index + 1 < len(texts):
                if end not in value:
                    return texts
                value = value.split(end, 1)[0]
            results.append(value.strip() or texts[index])
        return results

    @staticmethod
    def _extract_builtin_translation(data: dict[str, Any]) -> str:
        """Accept the documented endpoint plus common proxy response envelopes."""
        candidates: list[Any] = [
            data.get("translatedText"),
            data.get("translated_text"),
            data.get("translation"),
            data.get("result"),
        ]
        nested = data.get("data")
        if isinstance(nested, dict):
            candidates.extend(
                nested.get(key)
                for key in ("translatedText", "translated_text", "translation", "result")
            )
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _uapi_target_lang(value: str) -> str:
        language = str(value or "zh").strip()
        if language.casefold() in {"zh-cn", "zh-hans", "zh-sg"}:
            return "zh"
        return language

    def _builtin_request(
        self, text: str, to_lang: str | None
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, str] | None]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        path = urlsplit(self.api_url).path.rstrip("/").lower()
        if path.endswith("/ai/translate"):
            payload = {
                "text": text.strip(),
                "style": "professional",
                "context": "entertainment",
                "preserve_format": True,
            }
            params = {
                "target_lang": self._uapi_target_lang(
                    to_lang or self.target_lang
                )
            }
            return payload, headers, params
        payload = {
            "text": text.strip(),
            "sourceLanguage": "auto",
            "targetLanguage": to_lang or self.target_lang,
        }
        return payload, headers, None

    async def _translate_builtin(self, text: str, to_lang: str | None) -> str:
        payload, headers, params = self._builtin_request(text, to_lang)
        started = time.perf_counter()
        try:
            session = await self._ensure_session()
            async with session.post(
                self.api_url,
                params=params,
                json=payload,
                headers=headers,
                proxy=self.proxy_url,
                timeout=self.timeout,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[HD2] translation API status={resp.status} for text={text[:40]!r}"
                    )
                    HEALTH.record_fail("translate", detail=f"builtin HTTP {resp.status}")
                    return text
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    HEALTH.record_fail("translate", detail="builtin 响应非 JSON 对象")
                    return text
                translated = self._extract_builtin_translation(data)
                if translated and translated != text.strip():
                    HEALTH.record_ok(
                        "translate",
                        detail=f"builtin {len(text)}字符",
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                    )
                    return translated
                nested = data.get("data")
                logger.warning(
                    "[HD2] translation response missing translated text; keys=%s data_keys=%s",
                    sorted(str(key) for key in data.keys())[:12],
                    sorted(str(key) for key in nested.keys())[:12]
                    if isinstance(nested, dict)
                    else [],
                )
                HEALTH.record_fail("translate", detail="builtin 响应缺译文")
                return text
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[HD2] translation failed: {e}")
            HEALTH.record_fail("translate", detail=f"builtin: {e}")
            return text

    async def _translate_openai(self, text: str, to_lang: str | None) -> str:
        lang = to_lang or self.target_lang or "zh"
        lang_name = "简体中文" if lang.startswith("zh") else lang
        system = self._localization_system_prompt(lang_name, batch=False)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text.strip()},
            ],
            "temperature": 0.3,
            # Non-stream for reliability with free/slow models — streaming
            # is more prone to client_gone and chunk-parsing issues.
            "stream": False,
        }
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens
        content = await self._request_openai_content(payload)
        content = content.strip().strip('"').strip("“”").strip()
        return content if content and content != text.strip() else text

    @staticmethod
    def _extract_openai_batch(content: str) -> list[str] | None:
        """Best-effort extraction of a translation array from an LLM response.

        Accepts the documented ``{"translations": [...]}`` envelope plus several
        common wrappers (top-level array, ``{"data": {...}}``, nested ``data``).
        Returns the parsed list (which may be shorter/longer than expected) so
        the caller can fall back to per-item translation when lengths mismatch.
        Returns ``None`` only when no array can be recovered at all.
        """

        raw = str(content or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
        # 模型偶尔在 JSON 前后混入说明文字，截取首个 { 或 [ 到末尾再尝试解析
        first_obj = raw.find("{")
        first_arr = raw.find("[")
        if first_obj != -1 or first_arr != -1:
            start = min(x for x in (first_obj, first_arr) if x != -1)
            if start > 0:
                raw = raw[start:]
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

        def _strings(values: Any) -> list[str]:
            return [str(v).strip() for v in values if isinstance(v, str) and v.strip()]

        if isinstance(payload, list):
            values = _strings(payload)
            return values or None
        if not isinstance(payload, dict):
            return None
        # 在常见层级里找第一个非空字符串数组
        for container in (payload, payload.get("data"), payload.get("result")):
            if not isinstance(container, dict):
                continue
            for key in ("translations", "data", "result", "items", "list"):
                candidate = container.get(key)
                if isinstance(candidate, list):
                    values = _strings(candidate)
                    if values:
                        return values
        return None

    async def _translate_openai_batch(
        self,
        texts: list[str],
        to_lang: str | None,
    ) -> list[str]:
        lang = to_lang or self.target_lang or "zh"
        lang_name = "简体中文" if lang.startswith("zh") else lang
        system = self._localization_system_prompt(lang_name, batch=True)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(texts, ensure_ascii=False),
                },
            ],
            "temperature": 0.2,
            # CRITICAL: batch translation must NOT use streaming.  When the
            # output is JSON and max_tokens truncates mid-string, streaming
            # produces incomplete chunks that json.loads cannot parse — every
            # fragment falls back to原文.  Non-stream returns the full (even
            # if truncated) response in one shot, which the extractor can at
            # least attempt to recover.
            "stream": False,
        }
        # Cap output: 16 short phrases need at most ~1000 tokens of JSON.
        # Without this, free-tier models can "run away" to 1000-4000 tokens.
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens
        content = await self._request_openai_content(payload)
        # 冷却期 / 接口故障：_request_openai_content 返回空串，逐条也救不回
        if not content:
            return texts
        parsed = self._extract_openai_batch(content)
        if parsed is None:
            logger.warning(
                "[HD2] openai 批量翻译返回格式无效，降级逐条翻译（%d 条）", len(texts)
            )
            return await self._fallback_single_translate(texts, to_lang)
        if len(parsed) == len(texts):
            return parsed

        # Length mismatch is common with free models (extra commentary or a
        # split item).  Keep as many ordered items as we can, then only repair
        # the missing tail with single-item calls instead of redoing the whole
        # batch.  Extra trailing items are discarded.
        keep = min(len(parsed), len(texts))
        results = list(parsed[:keep])
        missing = texts[keep:]
        logger.warning(
            "[HD2] openai 批量翻译数量不匹配 expected=%d got=%d，保留前 %d 条并对剩余 %d 条逐条补齐",
            len(texts),
            len(parsed),
            keep,
            len(missing),
        )
        if not missing:
            return results
        repaired = await self._fallback_single_translate(missing, to_lang)
        results.extend(repaired)
        if len(results) < len(texts):
            results.extend(texts[len(results) :])
        return results[: len(texts)]

    async def _fallback_single_translate(
        self, texts: list[str], to_lang: str | None
    ) -> list[str]:
        """Batch failed/ambiguous: translate one-by-one and keep best effort.

        Each request is spaced by a short delay to avoid hammering the API and
        triggering rate limits when a large batch falls back to per-item calls.
        """

        results: list[str] = []
        # Cap per-item fallback to avoid a request burst, but cover at least
        # the batch size so a 16-item batch doesn't lose 11 items to原文.
        cap = max(5, min(len(texts), 10))
        for index, text in enumerate(texts[:cap]):
            if index > 0:
                await asyncio.sleep(1.5)
            translated = await self._translate_openai(text, to_lang)
            # _translate_openai 失败时返回原文，保留原文比丢掉更安全
            results.append(translated if translated else text)
            if self.in_cooldown() or self.in_transient_failure():
                break
        if len(results) < len(texts):
            results.extend(texts[len(results) :])
        return results

    async def _request_openai_content(self, payload: dict[str, Any]) -> str:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "Accept": (
                "text/event-stream"
                if payload.get("stream")
                else "application/json"
            ),
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            # Check cooldown (fast, non-blocking path — no semaphore needed)
            now = time.monotonic()
            if now < self._openai_cooldown_until:
                remaining = self._openai_cooldown_until - now
                if now - self._last_cooldown_log >= 15:
                    self._last_cooldown_log = now
                    logger.warning(
                        "[HD2] openai 翻译冷却中，%.0f 秒后重试",
                        remaining,
                    )
                return ""
            # Limit concurrency (not serialize) so multiple batches can run
            # in parallel while staying under the API's rate limit.
            async with self._openai_semaphore:
                # Re-check cooldown after acquiring the semaphore — another
                # request may have triggered it while we were waiting.
                now = time.monotonic()
                if now < self._openai_cooldown_until:
                    return ""
                # Space requests min_interval apart so tight per-minute
                # quotas (e.g. 5 req/min free tiers) are never exceeded.
                # Claim the slot FIRST and advance the pointer atomically
                # (no await between read and write), then sleep until our
                # own slot — waking-then-claiming would let several tasks
                # read the same target time and all fire together.
                if self.min_interval > 0:
                    slot = max(self._next_request_at, now)
                    self._next_request_at = slot + self.min_interval
                    wait = slot - now
                    if wait > 0:
                        await asyncio.sleep(wait)
                        # Cooldown may have kicked in while we waited.
                        if time.monotonic() < self._openai_cooldown_until:
                            return ""
                session = await self._ensure_session()
                urls = self._chat_urls()
                for url_index, chat_url in enumerate(urls):
                    for attempt in range(2):
                        async with session.post(
                            chat_url,
                            json=payload,
                            headers=headers,
                            proxy=self.proxy_url,
                            timeout=self.timeout,
                        ) as resp:
                            if resp.status == 200:
                                if payload.get("stream"):
                                    content = await self._read_openai_stream_content(resp)
                                    if content:
                                        HEALTH.record_ok("translate", detail="openai stream")
                                    else:
                                        HEALTH.record_fail("translate", detail="openai 流为空")
                                    return content
                                data = await resp.json(content_type=None)
                                choices = (data or {}).get("choices") or []
                                if not choices:
                                    HEALTH.record_fail("translate", detail="openai 无 choices")
                                    return ""
                                content = str(
                                    (choices[0].get("message") or {}).get("content")
                                    or ""
                                ).strip()
                                if content:
                                    HEALTH.record_ok("translate", detail="openai 200")
                                else:
                                    HEALTH.record_fail("translate", detail="openai 空回复")
                                return content
                            body = ""
                            try:
                                body = (await resp.text())[:160]
                            except Exception:
                                pass
                            if resp.status == 404 and url_index + 1 < len(urls):
                                logger.warning(
                                    "[HD2] openai 翻译 endpoint 404: %s，尝试备用 endpoint",
                                    chat_url,
                                )
                                break
                            if resp.status in {429, 567}:
                                self._enter_openai_cooldown(resp, body)
                                logger.warning(
                                    "[HD2] openai 翻译 status=%s body=%s",
                                    resp.status,
                                    body,
                                )
                                HEALTH.record_fail(
                                    "translate", detail=f"openai HTTP {resp.status}"
                                )
                                return ""
                            if resp.status in {500, 502, 503, 504} and attempt == 0:
                                logger.warning(
                                    "[HD2] openai 翻译 status=%s，2 秒后重试一次",
                                    resp.status,
                                )
                                await asyncio.sleep(2)
                                continue
                            logger.warning(
                                "[HD2] openai 翻译 status=%s body=%s",
                                resp.status,
                                body,
                            )
                            HEALTH.record_fail(
                                "translate", detail=f"openai HTTP {resp.status}"
                            )
                            if resp.status in {500, 502, 503, 504}:
                                self._enter_openai_cooldown(resp, body)
                            return ""
                HEALTH.record_fail("translate", detail="openai endpoint 404")
                return ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 超时/网络异常与 429/5xx 一样视作瞬态：慢速模型（2~15 t/s）
            # 的批量输出很容易撞上超时，若被上层当成"翻不动"会投毒负缓存。
            self._transient_failure_until = (
                time.monotonic() + self.transient_failure_window
            )
            logger.warning(f"[HD2] openai 翻译失败: {e}")
            HEALTH.record_fail("translate", detail=f"openai: {e}")
            return ""

    @staticmethod
    def _stream_content_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return ""
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    async def _read_openai_stream_content(
        self,
        response: aiohttp.ClientResponse,
    ) -> str:
        """Collect an OpenAI-compatible SSE response into one final message."""

        parts: list[str] = []
        async for raw_line in response.content:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data_text = line[5:].strip()
            if data_text == "[DONE]":
                break
            try:
                event = json.loads(data_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            choices = event.get("choices") if isinstance(event, dict) else None
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            content = self._stream_content_text(delta.get("content"))
            if not content:
                message = (
                    choice.get("message")
                    if isinstance(choice.get("message"), dict)
                    else {}
                )
                content = self._stream_content_text(message.get("content"))
            if content:
                parts.append(content)
        return "".join(parts).strip()

    def _enter_openai_cooldown(self, response: Any, body: str = "") -> None:
        if self.error_cooldown <= 0:
            return
        delay = float(self.error_cooldown)
        headers = getattr(response, "headers", {}) or {}
        retry_after = None
        try:
            retry_after = headers.get("Retry-After")
        except AttributeError:
            retry_after = None
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        self._openai_cooldown_until = max(
            self._openai_cooldown_until,
            time.monotonic() + delay,
        )


class TranslationCache:
    """翻译结果内存 + 磁盘缓存。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        provider_fingerprint: str = "",
        fallback_ttl: int = 900,
    ) -> None:
        self.path = data_dir / "translations.json"
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self.provider_fingerprint = provider_fingerprint
        self.fallback_ttl = max(60, int(fallback_ttl))
        # 写盘去抖：store 在热路径上每段调用一次，若每次都全量同步落盘，
        # 会随缓存增长变成 O(全量) 的事件循环阻塞。改为标脏 + 1s 合并写。
        self.MAX_ENTRIES = 4096
        self.SAVE_DEBOUNCE_SECONDS = 1.0
        self._dirty = False
        self._save_handle: asyncio.TimerHandle | None = None
        self._flush_task: asyncio.Task | None = None
        self.load()

    @staticmethod
    def _key(content_type: str, item_id: str) -> str:
        return f"{content_type}:{item_id}"

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except Exception as e:
            logger.warning(f"[HD2] load translation cache failed: {e}")
            self._data = {}

    def _mark_dirty(self) -> None:
        self._dirty = True
        if self._save_handle is None:
            loop = asyncio.get_running_loop()
            self._save_handle = loop.call_later(
                self.SAVE_DEBOUNCE_SECONDS, self._spawn_flush, loop
            )

    def _spawn_flush(self, loop: asyncio.AbstractEventLoop) -> None:
        self._save_handle = None
        # 持引用防任务被 GC（asyncio 只存弱引用）
        self._flush_task = loop.create_task(self.flush())

    async def flush(self) -> None:
        """立即写盘（关机路径与需要落盘保证的测试用）。"""
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        # Hold through disk completion so an older snapshot cannot win a race.
        async with self._flush_lock:
            while self._dirty:
                async with self._lock:
                    self._enforce_capacity()
                    payload = json.dumps(
                        self._data, ensure_ascii=False, separators=(",", ":")
                    )
                    self._dirty = False
                writer = asyncio.create_task(asyncio.to_thread(self._write_payload, payload))
                cancelled = False
                try:
                    while not writer.done():
                        try:
                            await asyncio.shield(writer)
                        except asyncio.CancelledError:
                            # Cancelling to_thread does not stop its disk write.
                            cancelled = True
                    writer.result()
                except Exception as exc:
                    self._dirty = True
                    logger.warning(f"[HD2] save translation cache failed: {exc}")
                    if not cancelled:
                        return
                if cancelled:
                    raise asyncio.CancelledError

    def _enforce_capacity(self) -> None:
        overflow = len(self._data) - self.MAX_ENTRIES
        if overflow <= 0:
            return
        oldest = sorted(
            self._data,
            key=lambda k: float(self._data[k].get("updated_at") or 0),
        )[:overflow]
        for key in oldest:
            self._data.pop(key, None)

    def _write_payload(self, payload: str) -> None:
        # Keep a unique staging file until the atomic replacement succeeds.
        tmp = self.path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise

    async def get(self, content_type: str, item_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            entry = self._data.get(self._key(content_type, item_id))
            if not isinstance(entry, dict):
                return None
            now = time.time()
            entry_fingerprint = str(entry.get("provider_fingerprint") or "")
            success = bool(entry.get("success", entry.get("translated_text") != entry.get("original_text")))
            if self.provider_fingerprint and entry_fingerprint != self.provider_fingerprint:
                return None
            if not success and now - float(entry.get("updated_at") or 0) > self.fallback_ttl:
                return None
            return entry

    async def store(
        self,
        content_type: str,
        item_id: str,
        original_text: str,
        translated_text: str,
        metadata: dict[str, Any] | None = None,
        success: bool | None = None,
        fallback_reason: str = "",
    ) -> None:
        is_success = (
            bool(success)
            if success is not None
            else bool(translated_text.strip() and translated_text.strip() != original_text.strip())
        )
        entry = {
            "original_text": original_text,
            "translated_text": translated_text,
            "metadata": metadata or {},
            "content_type": content_type,
            "item_id": item_id,
            "updated_at": time.time(),
            "success": is_success,
            "fallback_reason": fallback_reason if not is_success else "",
            "provider_fingerprint": self.provider_fingerprint,
        }
        async with self._lock:
            self._data[self._key(content_type, item_id)] = entry
            self._mark_dirty()

    async def clear_outdated(self, content_type: str, keep_ids: list[str]) -> None:
        keep = set(keep_ids)
        prefix = f"{content_type}:"
        async with self._lock:
            remove_keys = [
                k
                for k in self._data
                if k.startswith(prefix) and k[len(prefix) :] not in keep
            ]
            for k in remove_keys:
                self._data.pop(k, None)
            if remove_keys:
                self._mark_dirty()

    async def prune_prefixes(
        self, content_type: str, keep_prefixes: list[str]
    ) -> None:
        """按前缀保留清理：键带文本摘要（`{content_type}:{item}:{digest}`），
        精确 id 匹配无法表达"保留当前物品"，用前缀匹配。"""
        keep = tuple(p for p in keep_prefixes if p)
        if not keep:
            return
        prefix = f"{content_type}:"
        async with self._lock:
            remove_keys = [
                k
                for k in self._data
                if k.startswith(prefix)
                and not k[len(prefix) :].startswith(keep)
            ]
            for k in remove_keys:
                self._data.pop(k, None)
            if remove_keys:
                self._mark_dirty()
