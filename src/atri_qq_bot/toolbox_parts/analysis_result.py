from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .visual_origin import ImageOrigin


_IMAGE_NUMBER = re.compile(r"(?:第\s*([0-9一二三四五六七八九十]+)\s*张|图\s*([0-9一二三四五六七八九十]+))")
_CHINESE_IMAGE_NUMBERS = dict(zip("一二三四五六七八九十", range(1, 11)))


def _image_numbers(text: str, image_count: int) -> set[int]:
    numbers = set()
    for ordinal, label in _IMAGE_NUMBER.findall(text):
        value = ordinal or label
        if value.isascii() and value.isdigit() and len(value) <= 4:
            numbers.add(int(value))
        elif value in _CHINESE_IMAGE_NUMBERS:
            numbers.add(_CHINESE_IMAGE_NUMBERS[value])
    if "最后一张" in text and image_count:
        numbers.add(image_count)
    return numbers


@dataclass(frozen=True)
class _VisionModelResponse:
    content: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolAnalysisResult:
    """多模态工具的统一结果对象。

    `findings` 保存可引用证据，`limitations` 保存能力边界；两者分开后，
    OCR 成功但视觉理解失败时仍能准确回复文字而不虚构画面。
    """

    category: str
    style: str
    sources: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    read_level: str = "full_content"
    visual_data: bytes | None = field(default=None, repr=False, compare=False)
    visual_source: str = ""
    visual_kind: str = ""
    visual_status: str = ""
    visual_ocr_confidence: float = 0.0
    # 视频降级链路提供了可引用的 OCR 或标题时，允许交给对话模型继续回答。
    # 图片视觉失败不设置此标记，继续保持原有的防幻觉静默保护。
    visual_fallback_available: bool = False
    # 图片真实发送者昵称：群里追问“刚才那张图”时，若图是别人发的，这里记录
    # 发送者昵称，prompt_context 据此提示归属，避免模型把别人的图当成当前用户发的。
    # 默认空串表示“当前用户本人发的图/无需标注”，42 处现有构造无需改动。
    sender_origin_name: str = ""
    # 用户明确要求全文转写/翻译时置 True：提示对话模型基于完整 OCR 逐字输出，
    # 而不是只概括一两句。默认 False，现有构造无需改动。
    full_text_requested: bool = False
    # Archival evidence is deliberately excluded from prompt_context and repr.
    knowledge_materials: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)
    image_origins: list[ImageOrigin] = field(default_factory=list)
    dropped_images: list[ImageOrigin] = field(default_factory=list)
    image_results: list[ToolAnalysisResult | None] = field(default_factory=list, repr=False, compare=False)

    @property
    def image_evidence(self) -> dict[tuple[str, int], ToolAnalysisResult]:
        return {
            origin.key: result
            for origin, result in zip(self.image_origins, self.image_results)
            if origin.message_id and origin.segment_index >= 0 and result is not None
            and origin.evidence_level != "none"
        }

    # visual_data/visual_source 保存原始证据，status/limitations 描述证据边界；
    # 回复层据此区分“看到了画面”和“只识别到文字”，避免视觉失败后产生幻觉。
    def requires_visual_fail_safe(self, question: str = "") -> bool:
        if self.image_origins:
            images = [*self.image_origins, *self.dropped_images]
            targets = _image_numbers(question, len(images))
            if targets:
                return all(1 <= number <= len(images) for number in targets) and all(
                    images[number - 1].evidence_level == "none" for number in targets
                )
            return not self.visual_fallback_available and all(
                origin.evidence_level == "none" for origin in self.image_origins
            )
        return bool(
            self.visual_kind
            and self.visual_status in {"partial", "ocr_only", "metadata_only", "unavailable"}
            and not self.visual_fallback_available
        )

    def visual_failure_reply(self) -> str:
        """视觉失败只作为状态信号，不能伪造一条聊天回复。"""
        return ""

    def needs_grounding_repair(self, reply_text: str) -> bool:
        text = re.sub(r"\s+", "", str(reply_text or ""))
        if self.image_origins:
            images = [*self.image_origins, *self.dropped_images]
            for sentence in re.split(r"[。！？；\n]", str(reply_text or "")):
                for number in _image_numbers(sentence, len(images)):
                    if not 1 <= number <= len(images):
                        continue
                    evidence = images[number - 1].evidence_level
                    if evidence == "verified" or re.search(r"(?:未|没|无法|不能|看不清|读不到|不确定)", sentence):
                        continue
                    if re.search(r"(?:画面|场景|人物|颜色|动作|表情|图中|图里|清楚|可以看到)", sentence):
                        return True
                    if evidence == "none" and re.search(r"(?:显示|写着|内容是|说的是)", sentence):
                        return True
            # Global media claims cannot determine which of several attributed
            # images they describe. Apply the existing text checks below only to
            # turns with no usable image evidence.
            if any(origin.evidence_level != "none" for origin in self.image_origins):
                return False
        if not text or self.read_level == "full_content":
            return False
        if self.requires_visual_fail_safe():
            allowed_ocr = "".join(
                finding.split("：", 1)[1]
                for finding in self.findings
                if finding.startswith("独立 OCR 文字：") and "：" in finding
            )
            visual_claims = (
                "画面里",
                "图里是",
                "图片里是",
                "人物是",
                "角色是",
                "看起来在",
                "表情是",
                "动作是",
            )
            if any(claim in text for claim in visual_claims):
                return True
            if "图中文字" in text and not allowed_ocr:
                return True
        if self.read_level == "partial_content":
            return any(
                claim in text
                for claim in ("看完整个", "看完视频", "读完全文", "完整内容里")
            )
        unread_content_claims = (
            "点开看了",
            "点进去看",
            "看完视频",
            "视频里说",
            "视频里讲",
            "画面里",
            "镜头里",
            "听完了",
            "听起来",
            "旋律",
            "音色",
            "字幕里",
            "全文里",
            "文档里写",
            "图片里",
            "图里",
            "盯着图",
            "瞄了眼",
            "看着图片",
            "看着表情",
            "看不太清",
            "看不清",
            "像素低",
            "太糊",
        )
        return any(claim in text for claim in unread_content_claims)

    def prompt_context(self) -> str:
        source_text = "；".join(self.sources[:6]) or "用户消息"
        image_findings = {item for result in self.image_results if result for item in result.findings}
        other_findings = [item for item in self.findings if item not in image_findings]
        finding_text = "\n".join(f"- {item}" for item in other_findings[:8]) or "- 暂无可用正文"
        limitation_text = "\n".join(f"- {item}" for item in self.limitations[:5]) or "- 无明显限制"
        visibility_rule = ""
        if self.read_level == "metadata_only":
            visibility_rule = (
                "\n可见性限制：本轮材料只读到标题、分享卡片、文件名、QQ 摘要或少量元数据；"
                "这不等于看完视频、看清图片、读完整文档或核验全部内容。"
                "回复时必须自然说明“我只能基于标题/卡片/摘要判断”，"
                "禁止说“我看完了视频”“视频里说”“我看到画面”“文档写了很多细节”等未读取事实。"
            )
        elif self.read_level == "partial_content":
            visibility_rule = (
                "\n可见性限制：本轮只读取到部分正文、公开简介、公开字幕或局部内容；"
                "可以基于可见部分分析，但必须避免断言完整视频/全文的未读细节。"
            )
        visual_rule = ""
        image_blocks = ""
        image_style_rule = (
            "\n图片/表情是用户此刻情绪和状态的表达，不是要你点评的对象。"
            "要把读到的用户情绪当成他的一句潜台词，直接对“他这个人、他此刻的状态”做出自然反应："
            "他懵逼没看懂就解释或询问，他无语就接梗，他开心就一起高兴，他撒娇就顺着回应。"
            "禁止描述画面长什么样、禁止评价这张图或表情本身好不好、像什么。"
        )
        if self.image_origins:
            evidence_descriptions = {
                "verified": "已读取画面，可按本图证据回答",
                "ocr_only": "只读到文字，可转述 OCR，不描述画面",
                "metadata_only": "只读到 QQ 摘要或资料信息，不描述画面",
                "none": "未读到可靠证据，不描述内容",
            }
            lines = ["\n逐图证据（图片内容和归属必须对应，禁止用其他图或聊天历史补全）："]
            for index, origin in enumerate(self.image_origins):
                evidence = evidence_descriptions[origin.evidence_level]
                lines.append(f"- {origin.label}｜{origin.origin_text()}；{evidence}。")
                if index < len(self.image_results) and (result := self.image_results[index]) is not None:
                    for finding in result.findings[:8]:
                        lines.append(f"  {origin.label}｜{finding}")
                    for limitation in result.limitations[:3]:
                        lines.append(f"  {origin.label}｜限制：{limitation}")
            if self.dropped_images:
                lines.append("未纳入本轮分析的图片（不可描述）：")
                lines.extend(f"- {origin.label}｜{origin.origin_text()}；超过本轮图片上限。" for origin in self.dropped_images)
            image_blocks = "\n".join(lines)
            visual_rule = (
                "\n逐图证据优先于用户画像、兴趣、旧图片和旧助手回复。"
                "用户明确询问某张图片时，只以该图为依据；没有该编号或该图没有证据时，如实说明。"
                "引用图或以前的图只用于回答对该图的提问，不代表当前说话人的情绪。"
            )
            image_style_rule = (
                "\n结合文字理解说话人的情绪时，只考虑他本轮发的图，以及和本条合并处理的本人图片。"
                "普通发表情时自然接话。用户明确问某张图的内容时，直接根据那张图的可见证据回答。"
            )
        elif self.visual_kind or any(
            item.startswith(("图片内容分析", "表情包情绪分析", "图片信息", "表情包信息"))
            for item in self.findings
        ):
            if self.requires_visual_fail_safe():
                visual_rule = (
                    "\n视觉证据状态：本轮没有取得可靠的完整画面理解。"
                    "只能复述明确列出的独立 OCR 或 QQ 摘要；禁止描述人物、动作、场景、"
                    "表情、颜色、身份或图片含义，禁止用聊天记忆补全。"
                    "应明确说本次识别不稳定，不得为了自然聊天而猜图。"
                )
            elif self.visual_fallback_available:
                visual_rule = (
                    "\n视觉结果状态：本轮视频没有取得完整可靠的画面理解，"
                    "但提供了可引用的 OCR 文字或视频标题。"
                    "只能基于这些明确证据回答；不要声称看清未读取的画面、动作或声音，"
                    "也不要用旧上下文补全视频内容。"
                    "如果用户问来源、最新进展或标题涉及具体事实，再按需使用联网搜索核验；"
                    "普通分享不必强行搜索。"
                )
            else:
                visual_rule = (
                    "\n以下视觉结果是本轮当前图片唯一可用的画面证据，优先级高于用户画像、兴趣、"
                    "旧图片、旧助手回复和此前聊天猜测。"
                    "回复时优先围绕当前画面或表情情绪回复。"
                    "人物身份、画中文字和画面细节只能来自本轮视觉结果或本轮联网核验，"
                    "不得用聊天记忆补全或改写。"
                    "\n用户问人物、文字、含义或情绪时必须直接回答，不要反问用户想让你识别还是描述。"
                    "视觉结果要求联网核验时直接使用搜索工具，不要先索要搜索许可。"
                )
        # 图片是别人发的、当前用户只是在追问时，明确归属，避免张冠李戴。
        origin_rule = ""
        if self.sender_origin_name and not self.image_origins:
            origin_rule = (
                "\n图片归属：这张图是群友“"
                f"{self.sender_origin_name}”发的，不是当前说话的人发的；"
                "不要把图里的内容当成当前用户说的或做的，回答时注意区分发送者。"
            )
        full_text_rule = ""
        if self.full_text_requested:
            full_text_rule = (
                "\n全文转写要求：用户明确要完整识别/翻译图中文字，"
                "请基于上方“独立 OCR 文字”逐字完整输出，不要概括、不要只挑一两句；"
                "用户要求翻译就给出对应译文，否则转写原文，并保留原有换行顺序。"
                "OCR 为空或识别不可信时，如实说明看不清，不得编造文字。"
            )
        return (
            "你已获得用户发来的外部材料读取结果。不要说“我调用了工具”，不要输出技术流程；"
            "只把这些信息自然用于回答。"
            f"\n分类：{self.category}。"
            f"\n回复风格：{self.style}。"
            "\n风格要求：日常生活乐趣=轻松、有趣、可以短短吐槽；生活学术研究=准确、严谨、先给结论和依据。"
            f"{image_style_rule}"
            "如果用户明确要求深度分析、总结、数据分析或报告，才按结论、依据、风险/建议组织。"
            "\n禁止把没有读取到的内容编造成事实；不确定就说明只能基于当前可见信息判断。"
            f"{visibility_rule}"
            f"{visual_rule}"
            f"{image_blocks}"
            f"{origin_rule}"
            f"{full_text_rule}"
            f"\n来源：{source_text}"
            f"\n可用信息：\n{finding_text}"
            f"\n限制：\n{limitation_text}"
        )

    def fallback_reply(self) -> str:
        """兼容旧接口；工具失败不再产生规则化用户回复。"""
        return ""


__all__ = ["ToolAnalysisResult"]
