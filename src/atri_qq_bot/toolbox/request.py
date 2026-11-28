from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from ..toolbox_parts.analysis_result import ToolAnalysisResult
from ..toolbox_parts.formatting import _shorten
from ..toolbox_parts.request_collection import (
    _classify_category,
    _collect_request,
    _material_filename_hint,
    _SegmentRef,
)
from ..toolbox_parts.visual_origin import ImageOrigin
from .constants import OneBotActionCaller
from .helpers import (
    _dedupe,
    _demote_unverified_visual_identity,
    _exception_summary,
    _is_visual_ref,
    _looks_like_video_url,
    _meaningful_video_title,
    _merge_read_levels,
    _merge_visual_statuses,
    _requests_full_text_transcription,
    _search_result_supports_query,
    _video_url_metadata_fallback,
    _visual_identity_needs_verification,
    _visual_prompt_for_kind,
    _visual_search_query,
)


class ToolboxRequestMixin:
    async def analyze_url_material(self, url: str, share_text: str = '') -> ToolAnalysisResult:
        """Run a durable URL import through the same request pipeline as QQ.

        Only the selected URL is fetched. Platform share captions are evidence
        supplied by the user, not proof of reading the remote video's contents.
        """
        from ..toolbox_parts.request_collection import _plain_share_title
        title = _plain_share_title(share_text, url)
        event = {'message': [{'type': 'share', 'data': {'url': url, 'title': title}}]} if title else {}
        result = await self.analyze(event, url)
        if result is None:
            raise ValueError('资料解析工具已关闭或未返回可读内容')
        if title and (result.read_level == 'metadata_only' or not any(
                m.get('text', '').strip() for m in result.knowledge_materials)):
            text = '用户提供的分享标题（未读取视频画面、音轨或字幕；标题可能被分享平台截断）：\n' + title
            result = replace(result, knowledge_materials=[
                *(m for m in result.knowledge_materials if m.get('text', '').strip()), {
                'title': title, 'locator': url, 'text': text, 'coverage': 'user_share_metadata_only',
                'blocks': [{'anchor': 'user_share_title', 'text': text}],
            }])
        return result

    async def analyze(
        self,
        event: dict[str, Any],
        plain_text: str,
        action_caller: OneBotActionCaller | None = None,
        *,
        allow_vision: bool = True,
        deadline: float | None = None,
        image_refs_max: int | None = None,
        visual_question: str | None = None,
    ) -> ToolAnalysisResult | None:
        if not self.enabled:
            return None

        attribution = bool(getattr(self.config, "image_attribution_enabled", True))
        if image_refs_max is None:
            image_refs_max = getattr(self.config, "toolbox_image_refs_max", 4) if attribution else 3
        request = _collect_request(
            event, plain_text, image_refs_max=image_refs_max, image_attribution=attribution
        )
        if not request.has_material:
            return None

        findings: list[str] = []
        limitations: list[str] = []
        sources: list[str] = []
        read_levels: list[str] = []
        knowledge_materials: list[dict[str, Any]] = []
        visual_data: bytes | None = None
        visual_source = ""
        visual_kind = ""
        visual_statuses: list[str] = []
        visual_ocr_confidence = 0.0
        visual_fallback_available = False
        image_origins: list[ImageOrigin] = []
        image_results: list[ToolAnalysisResult | None] = []
        first_image: ToolAnalysisResult | None = None
        loop = asyncio.get_running_loop()

        def stage_expired() -> bool:
            return deadline is not None and loop.time() >= deadline

        def track_visual_result(result: ToolAnalysisResult) -> None:
            knowledge_materials.extend(result.knowledge_materials)
            if not result.knowledge_materials:
                knowledge_materials.append({"title": "媒体分析记录", "text": "\n".join(result.findings),
                    "locator": ", ".join(result.sources), "coverage": "analysis_summary:" + result.read_level,
                    "raw": result.visual_data, "filename": "visual.bin"})
            nonlocal visual_data
            nonlocal visual_source
            nonlocal visual_kind
            nonlocal visual_ocr_confidence
            nonlocal visual_fallback_available
            if not result.visual_kind:
                return
            visual_kind = result.visual_kind
            visual_statuses.append(result.visual_status or "unavailable")
            visual_ocr_confidence = max(
                visual_ocr_confidence,
                float(result.visual_ocr_confidence or 0.0),
            )
            visual_fallback_available = visual_fallback_available or bool(
                getattr(result, "visual_fallback_available", False)
            )
            if result.visual_data:
                visual_data = result.visual_data
                visual_source = result.visual_source

        for url in request.urls[:4]:
            # 白名单关闭时仍允许普通网页/文档工具，但不读取明显的图片/视频 URL。
            if not allow_vision and (_looks_like_video_url(url) or _is_visual_ref(url)):
                continue
            if _looks_like_video_url(url):
                try:
                    url_deadline = loop.time() + self.video_deadline_seconds
                    if deadline is not None:
                        url_deadline = min(url_deadline, deadline)
                    if stage_expired():
                        raise TimeoutError
                    async with asyncio.timeout_at(url_deadline):
                        result = await self._analyze_url(url)
                except asyncio.TimeoutError:
                    result = _video_url_metadata_fallback(
                        url,
                        "视频链接分析达到 60 秒时限，已降级使用链接标题。",
                    )
                except Exception as exc:
                    result = _video_url_metadata_fallback(
                        url,
                        f"视频链接分析失败，已降级使用链接标题：{_exception_summary(exc)}",
                    )
            else:
                if stage_expired():
                    break
                try:
                    async with asyncio.timeout_at(deadline):
                        result = await self._analyze_url(url)
                except TimeoutError:
                    if deadline is None:
                        raise
                    limitations.append("链接读取到达本轮材料时限，未获得更多证据。")
                    break
            sources.extend(result.sources)
            findings.extend(result.findings)
            limitations.extend(result.limitations)
            read_levels.append(result.read_level)
            track_visual_result(result)

        for path in request.paths[:4]:
            if not allow_vision and _is_visual_ref(path):
                continue
            if stage_expired():
                break
            try:
                async with asyncio.timeout_at(deadline):
                    result = await self._analyze_path(path)
            except TimeoutError:
                if deadline is None:
                    raise
                limitations.append("本地材料读取到达本轮时限。")
                break
            sources.extend(result.sources)
            findings.extend(result.findings)
            limitations.extend(result.limitations)
            read_levels.append(result.read_level)
            track_visual_result(result)

        for file_ref in request.file_refs[:3]:
            if not allow_vision and _is_visual_ref(_material_filename_hint(file_ref)):
                continue
            if stage_expired():
                break
            try:
                async with asyncio.timeout_at(deadline):
                    result = await self._analyze_file_ref(file_ref, action_caller)
            except TimeoutError:
                if deadline is None:
                    raise
                limitations.append("附件读取到达本轮材料时限。")
                break
            sources.extend(result.sources)
            findings.extend(result.findings)
            limitations.extend(result.limitations)
            read_levels.append(result.read_level)
            track_visual_result(result)

        image_question = plain_text if visual_question is None else visual_question
        for image_ref in request.image_refs:
            if not allow_vision:
                continue
            origin = image_ref.image_origin
            if stage_expired():
                if origin is not None:
                    image_origins.append(replace(origin, status="skipped", reason="stage_budget"))
                    image_results.append(None)
                continue
            started_at = loop.time()
            image_deadline = (
                min(deadline, started_at + float(getattr(self.config, "toolbox_vision_image_budget_seconds", 45.0)))
                if deadline is not None else None
            )
            try:
                async with asyncio.timeout_at(image_deadline):
                    if image_deadline is None:
                        result = await self._analyze_image_ref(image_ref, action_caller, user_question=image_question)
                    else:
                        result = await self._analyze_image_ref(
                            image_ref, action_caller, user_question=image_question, deadline=image_deadline
                        )
            except TimeoutError:
                if image_deadline is None:
                    raise
                limitations.append("图片分析超过时限。")
                if origin is not None:
                    image_origins.append(replace(origin, reason="timeout", elapsed_ms=int((loop.time() - started_at) * 1000)))
                    image_results.append(None)
                continue
            result = await self._verify_image_identity(result, image_deadline)
            sources.extend(result.sources)
            limitations.extend(result.limitations)
            read_levels.append(result.read_level)
            findings.extend(result.findings)
            track_visual_result(result)
            if origin is not None:
                evidence_level = {
                    "verified": "verified", "ocr_only": "ocr_only", "metadata_only": "metadata_only",
                }.get(result.visual_status, "none")
                status = {
                    "verified": "analyzed", "ocr_only": "analyzed", "metadata_only": "metadata_only",
                }.get(evidence_level, "unavailable")
                image_origins.append(replace(
                    origin, status=status, evidence_level=evidence_level,
                    reason="unavailable" if evidence_level == "none" else "",
                    elapsed_ms=int((loop.time() - started_at) * 1000),
                ))
                image_results.append(result)
                if first_image is None and result.visual_data and evidence_level in {"verified", "ocr_only"}:
                    first_image = result

        video_deadline = (
            loop.time() + self.video_deadline_seconds
            if request.video_refs
            else None
        )
        if deadline is not None and video_deadline is not None:
            video_deadline = min(deadline, video_deadline)
        for video_ref in request.video_refs[:2]:
            if not allow_vision:
                continue
            result = await self._analyze_video_ref_bounded(
                video_ref,
                action_caller,
                video_deadline,
            )
            sources.extend(result.sources)
            findings.extend(result.findings)
            limitations.extend(result.limitations)
            read_levels.append(result.read_level)
            track_visual_result(result)

        findings.extend(request.share_hints[:6])
        limitations.extend(request.segment_limitations[:4])
        if request.share_hints or request.segment_limitations:
            read_levels.append("metadata_only")

        if not findings and not limitations and not image_origins:
            return None

        category = _classify_category(plain_text, request)
        style = "准确严谨" if category == "生活学术研究" else "抽象有趣"
        visual_status = _merge_visual_statuses(visual_statuses)
        if image_origins:
            levels = {origin.evidence_level for origin in image_origins}
            visual_status = "partial" if len(levels) > 1 else next(iter(levels))
            if visual_status == "none":
                visual_status = "unavailable"
            if first_image is not None:
                visual_data = first_image.visual_data
                visual_source = first_image.visual_source
                visual_kind = first_image.visual_kind
        return ToolAnalysisResult(
            category=category,
            style=style,
            sources=_dedupe(sources),
            findings=_dedupe(findings),
            limitations=_dedupe(limitations),
            read_level=_merge_read_levels(read_levels),
            visual_data=visual_data,
            visual_source=visual_source,
            visual_kind=visual_kind,
            visual_status=visual_status,
            visual_ocr_confidence=visual_ocr_confidence,
            visual_fallback_available=visual_fallback_available,
            knowledge_materials=knowledge_materials,
            full_text_requested=any(result and result.full_text_requested for result in image_results),
            image_origins=image_origins,
            image_results=image_results,
            dropped_images=request.dropped_images if allow_vision else [],
        )

    async def _analyze_video_ref_bounded(
        self,
        video_ref: _SegmentRef,
        action_caller: OneBotActionCaller | None,
        deadline: float | None,
    ) -> ToolAnalysisResult:
        """在单条消息的剩余预算内处理视频，超时后只返回标题/摘要证据。"""
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + self.video_deadline_seconds
        try:
            if loop.time() >= deadline:
                raise TimeoutError
            async with asyncio.timeout_at(deadline):
                return await self._analyze_video_ref(video_ref, action_caller)
        except asyncio.TimeoutError:
            return self._video_metadata_fallback(
                video_ref,
                "视频分析达到 60 秒时限，已降级使用标题/摘要。",
            )
        except Exception as exc:
            return self._video_metadata_fallback(
                video_ref,
                f"视频分析失败，已降级使用标题/摘要：{_exception_summary(exc)}",
            )

    def _video_metadata_fallback(
        self,
        video_ref: _SegmentRef,
        limitation: str,
    ) -> ToolAnalysisResult:
        findings = [f"收到视频：{video_ref.label}。"]
        title = video_ref.summary or video_ref.name or video_ref.label
        if _meaningful_video_title(title):
            findings.append(f"视频标题：{title}")
        if video_ref.url:
            findings.append(f"视频地址：{video_ref.url}")
        return ToolAnalysisResult(
            "日常生活乐趣",
            "抽象有趣",
            [video_ref.url or video_ref.label],
            findings,
            [limitation],
            read_level="metadata_only",
            visual_kind="video",
            visual_status="metadata_only",
            visual_fallback_available=any(
                item.startswith("视频标题：") for item in findings
            ),
        )

    async def analyze_visual_followup(
        self,
        previous: ToolAnalysisResult,
        question: str,
        *,
        allow_vision: bool = True,
        deadline: float | None = None,
    ) -> ToolAnalysisResult | None:
        if not allow_vision or not previous.visual_data:
            return None
        full_text = _requests_full_text_transcription(question)
        prompt = _visual_prompt_for_kind(
            previous.visual_kind or "auto",
            question,
            full_text=full_text,
            reference_image=True,
        )
        kwargs = {"deadline": deadline} if deadline is not None else {}
        async with asyncio.timeout_at(deadline):
            result = await self._analyze_image_bytes(
                previous.visual_data,
                previous.visual_source or "上一张图片",
                visual_kind=previous.visual_kind or "auto",
                vision_prompt=prompt,
                full_text=full_text,
                **kwargs,
            )
        return await self._verify_image_identity(result, deadline)

    async def _verify_image_identity(
        self, result: ToolAnalysisResult, image_deadline: float | None
    ) -> ToolAnalysisResult:
        # Only consume the model's description. Keep independent OCR verbatim.
        for index, finding in enumerate(result.findings):
            label, _, description = finding.partition("：")
            if label in {"图片/表情理解", "表情包情绪理解"}:
                break
        else:
            return result
        if not _visual_identity_needs_verification(description):
            return result

        from ..llm_tools.web_search_tool import DEFAULT_SEARCH_TIMEOUT_SECONDS, search_web

        query = _visual_search_query(description)
        if not query:
            reason = "未获得可用的核验关键词。"
        elif not getattr(self.config, "web_search_enabled", True):
            reason = "联网搜索已关闭。"
        else:
            loop = asyncio.get_running_loop()
            timeout = float(
                getattr(self.config, "web_search_timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS)
                or DEFAULT_SEARCH_TIMEOUT_SECONDS
            )
            search_deadline = loop.time() + timeout
            if image_deadline is not None:
                search_deadline = min(search_deadline, image_deadline)
            try:
                if loop.time() >= search_deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(search_deadline):
                    search_result = await search_web({"query": query, "max_results": 3}, self.config)
                if search_result.startswith(("搜索失败：", "搜索未执行：")):
                    reason = _shorten(search_result, 300)
                elif _search_result_supports_query(search_result, query):
                    return replace(
                        result,
                        sources=[*result.sources, f"网页核验：{query}"],
                        findings=[*result.findings, f"视觉身份联网核验：{_shorten(search_result, 2400)}"],
                    )
                else:
                    reason = "没有找到与视觉关键词直接匹配的网页。"
            except TimeoutError:
                reason = "到达核验时限。"
            except Exception as exc:
                reason = _exception_summary(exc)

        demoted = _demote_unverified_visual_identity(description)
        findings = list(result.findings)
        findings[index] = f"{label}：{demoted}"
        materials = []
        for material in result.knowledge_materials:
            text = material.get("text", "")
            if material.get("coverage") == "image_ocr_and_model_description;unverified" and text.endswith(description):
                material = {
                    **material,
                    "text": text.removesuffix(description) + demoted,
                    "blocks": [
                        {**block, "text": demoted}
                        if block.get("anchor") == "model_description" else block
                        for block in material.get("blocks", [])
                    ],
                }
            materials.append(material)
        return replace(
            result,
            findings=findings,
            limitations=[*result.limitations, f"视觉身份联网核验未成功：{reason}"],
            knowledge_materials=materials,
        )
