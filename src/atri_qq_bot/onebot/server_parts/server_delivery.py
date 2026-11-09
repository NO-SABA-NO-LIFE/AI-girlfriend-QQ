from __future__ import annotations

import copy
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .server_support import OneBotServerMixin, _sync_server_globals
from ...message_plan import outgoing_to_segments  # type: ignore[import-not-found]

LOGGER = logging.getLogger("atri.onebot")

# 内部约定字段：合并“别人发的群图”时，把真实发送者昵称写进图片段的 data 里。
# 命名以下划线开头，避免与 OneBot 原始字段冲突；toolbox 读取后转成归属提示。
# 注意：toolbox_parts/request_collection.py 用同名字符串读取，修改需两处同步。
SENDER_NAME_META_KEY = "_atri_sender_name"


def _history_event_time(event: dict[str, Any]) -> datetime | None:
    """读取 OneBot 历史消息的 epoch 秒，异常记录不参与时间范围判断。"""

    try:
        return datetime.fromtimestamp(float(event.get("time")), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _outgoing_delay_text(
    messages: list[Any],
    fallback_text: str | None = None,
) -> str:
    """估算打字耗时要用的文本：文本取正文，语音等取将要发出的文本。"""
    for message in messages:
        if getattr(message, "kind", "") == "text":
            content = str(getattr(message, "content", "") or "").strip()
            if content:
                return content
    if fallback_text:
        return str(fallback_text)
    return "".join(str(getattr(message, "content", "") or "") for message in messages)


class OneBotDeliveryMixin(OneBotServerMixin):
    async def send_group_summary(self, group_id: str, image_path: str) -> None:
        """将总结 PNG 作为 QQ 图片消息发送到对应群聊。"""
        websocket = self._first_active_websocket()
        if websocket is None:
            raise RuntimeError("NapCat 当前未连接，无法发送群聊总结")
        path = Path(str(image_path)).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"群聊总结图片不存在：{path}")
        result = await self.call_action_and_wait(
            websocket,
            "send_group_msg",
            {
                "group_id": str(group_id),
                "message": [{"type": "image", "data": {"file": str(path), "cache": 0}}],
            },
            15.0,
        )
        self._require_message_delivery(result)

    def _singing_outgoing(
        self,
        event: dict[str, Any],
        user_text: str,
        decision: SingingDecision,
    ) -> tuple[list[OutgoingMessage], str]:
        """将点歌决策转换为唯一一种 OneBot 输出，并记录可诊断事件。"""

        voice_request = VoiceRequest(
            text=str(user_text or "")[: max(20, int(self.config.voice_max_chars))],
            reason="explicit_request",
            mode="singing",
        )
        if decision.song is not None:
            fallback_text = f"《{decision.song.title}》完整歌曲"
            message = OutgoingMessage("record", str(decision.song.path))
            self._log_voice_event(
                event,
                "complete_song_selected",
                request=voice_request,
                message=message,
                error=decision.reason,
            )
            return [message], fallback_text

        if not decision.handled:
            return [], ""
        reply_text = decision.reply_text
        self._log_voice_event(
            event,
            "song_unavailable" if decision.reason not in {"catalog", "stopped"} else "singing_guidance",
            request=voice_request,
            error=decision.reason,
        )
        return [OutgoingMessage("text", reply_text)], ""

    async def _resolve_quoted_message(
        self,
        websocket: Any,
        event: dict[str, Any],
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """解析用户引用的消息，供本轮模型和多模态工具共同使用。

        QQ/OneBot 的 reply 消息段通常只携带目标消息 ID；若事件没有直接
        附带原文，就按需调用 ``get_msg``。读取失败时保留“存在引用”的
        明确信息，但绝不根据 ID 猜测原文。
        """

        message_id = extract_reply_message_id(event.get("message"))
        if not message_id:
            reply_payload = event.get("reply")
            if isinstance(reply_payload, dict):
                value = reply_payload.get("id") or reply_payload.get("message_id")
                if value not in (None, ""):
                    message_id = str(value)
        if not message_id:
            return "", "", []

        inline_message = extract_reply_inline_message(event)
        if inline_message is not None:
            return (
                message_id,
                extract_plain_text(inline_message),
                _message_to_segments(inline_message),
            )

        request_id: int | str = int(message_id) if message_id.isdigit() else message_id
        try:
            result = await self.call_action_and_wait(
                websocket,
                "get_msg",
                {"message_id": request_id},
                3.0,
            )
            data = result.get("data") if isinstance(result, dict) else None
            quoted_message = data.get("message") if isinstance(data, dict) else None
            if quoted_message is not None:
                return (
                    message_id,
                    extract_plain_text(quoted_message),
                    _message_to_segments(quoted_message),
                )
        except Exception as exc:
            LOGGER.warning("引用消息读取失败：message_id=%s error=%s", message_id, exc)

        return message_id, "[引用消息原文暂时无法读取]", []

    def _event_with_quoted_message(
        self,
        event: dict[str, Any],
        quoted_segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """把引用消息的媒体段合并到本轮分析事件，不改变原始事件。"""

        if not quoted_segments:
            return event
        merged = dict(event)
        current_segments = _message_to_segments(event.get("message"))
        merged["message"] = (
            [{"type": "text", "data": {"text": "[引用消息]"}}]
            + quoted_segments
            + [{"type": "text", "data": {"text": "\n[当前消息]"}}]
            + current_segments
        )
        return merged

    def _allow_poke_back(self, event: dict[str, Any]) -> bool:
        """回戳动作的会话级短冷却：正常连续戳都能回戳，只防秒级狂戳刷屏。"""

        return self._poke_cooldown_passed(
            event, "_last_poke_back_at", POKE_BACK_COOLDOWN_SECONDS
        )

    def _allow_poke_text(self, event: dict[str, Any]) -> bool:
        """文字回复的会话级长冷却：避免连续戳产生大段文字刷屏。"""

        return self._poke_cooldown_passed(
            event, "_last_poke_text_at", POKE_TEXT_COOLDOWN_SECONDS
        )

    def _poke_cooldown_passed(
        self, event: dict[str, Any], store_name: str, cooldown_seconds: float
    ) -> bool:
        key = _message_queue_id(event)
        now = time.time()
        store = getattr(self, store_name, None)
        if store is None:
            store = {}
            setattr(self, store_name, store)
        if now - store.get(key, 0.0) < cooldown_seconds:
            return False
        store[key] = now
        return True

    def _remember_pending_group_visual(self, event: dict[str, Any]) -> None:
        key = _pending_visual_key(event)
        if not key or not _event_has_visual_material(event):
            return
        now = time.time()
        self._pending_group_visual_events[key] = (now, event)
        if len(self._pending_group_visual_events) > 256:
            oldest_key = min(
                self._pending_group_visual_events,
                key=lambda item: self._pending_group_visual_events[item][0],
            )
            self._pending_group_visual_events.pop(oldest_key, None)
        # 同时记入群维度的近期图片队列，供被 @ 回复时顺带理解。
        self._remember_recent_group_visual(event, now)

    def _remember_recent_group_visual(
        self, event: dict[str, Any], now: float
    ) -> None:
        group_id = event.get("group_id")
        message = event.get("message")
        if group_id is None or not isinstance(message, list):
            return
        ttl = max(30, int(getattr(self.config, "group_recent_visual_ttl_seconds", 300)))
        max_count = max(1, int(getattr(self.config, "group_recent_visual_max", 3)))
        bucket = self._recent_group_visuals.setdefault(str(group_id), [])
        # 记录图片真实发送者：昵称拿不到时退回 QQ 号，保证归属始终有值。
        sender_id = str(event.get("user_id") or "")
        sender_name = str(_nickname(event) or sender_id or "群友")
        # 收集本条消息里的图片/动画表情段（深拷贝，避免后续 event 被修改影响存档）。
        for segment in message:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type") or "").lower() in {
                "image", "mface", "marketface"
            }:
                bucket.append(
                    (now, copy.deepcopy(segment), sender_id, sender_name)
                )
        # 先按 TTL 淘汰过期项，再限制张数，始终保留时间最新的若干张。
        fresh = [item for item in bucket if now - item[0] <= ttl]
        if len(fresh) > max_count:
            fresh = fresh[-max_count:]
        self._recent_group_visuals[str(group_id)] = fresh

    def _event_with_recent_group_visuals(
        self, event: dict[str, Any]
    ) -> dict[str, Any]:
        """被 @ 回复时，把群里近期图片段合并到本次 event，供本次分析顺带读懂。"""
        if not getattr(self.config, "group_recent_visual_enabled", True):
            return event
        if event.get("message_type") != "group":
            return event
        group_id = str(event.get("group_id") or "")
        now = time.time()
        ttl = max(30, int(getattr(self.config, "group_recent_visual_ttl_seconds", 300)))
        max_count = max(1, int(getattr(self.config, "group_recent_visual_max", 3)))
        bucket = self._recent_group_visuals.get(group_id, [])
        # 兼容旧的 2 元组存档：缺失发送者时按“未知群友”处理，不猜测成当前人。
        recent_entries = [
            item for item in bucket if now - item[0] <= ttl
        ][-max_count:]
        if not recent_entries:
            return event
        current_id = str(event.get("user_id") or "")
        current_segments = (
            event.get("message")
            if isinstance(event.get("message"), list)
            else []
        )
        # 去重：当前消息里已经带的图片不重复合并。
        existing_keys = {
            str((seg.get("data") or {}).get("file") or (seg.get("data") or {}).get("url"))
            for seg in current_segments
            if isinstance(seg, dict)
        }
        extra: list[dict[str, Any]] = []
        for entry in recent_entries:
            # 兼容旧的 2 元组存档：缺失发送者时留空，按“非本人”处理而不猜测成当前人。
            stored_at, segment = entry[0], entry[1]
            sender_id = entry[2] if len(entry) > 2 else ""
            sender_name = entry[3] if len(entry) > 3 else ""
            seg_key = str(
                (segment.get("data") or {}).get("file")
                or (segment.get("data") or {}).get("url")
            )
            if seg_key in existing_keys:
                continue
            copied = copy.deepcopy(segment)
            # 别人发的图：在内部字段标注真实发送者，下游把它转成明确的归属提示，
            # 模型就不会把这张图当成当前触发者发的。本人发的图无需标注。
            if sender_id and sender_id != current_id:
                copied.setdefault("data", {})[SENDER_NAME_META_KEY] = (
                    sender_name or sender_id
                )
            extra.append(copied)
        if not extra:
            return event
        merged = dict(event)
        merged["message"] = extra + [dict(seg) for seg in current_segments]
        return merged

    def _event_with_pending_group_visual(
        self,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        key = _pending_visual_key(event)
        if not key:
            return event
        if _event_has_visual_material(event):
            self._pending_group_visual_events.pop(key, None)
            return event
        pending = self._pending_group_visual_events.pop(key, None)
        if (
            not pending
            or time.time() - pending[0] > PENDING_GROUP_VISUAL_TTL_SECONDS
        ):
            return event
        return _merge_pending_visual_event(pending[1], event)

    async def _send_streamed_speech_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        conversation_id: str,
        request: Any,
    ) -> str:
        segments = split_spoken_text(
            request.text,
            maximum_units=int(
                getattr(self.config, "voice_segment_max_chars", 34) or 34
            ),
        )
        if not segments:
            return ""

        delivered_segments: list[str] = []
        for index, text in enumerate(segments):
            segment_started_at = time.monotonic()
            segment_request = replace(request, text=text)
            try:
                synthesis = await self._synthesize_voice_segment(
                    conversation_id,
                    segment_request,
                    enforce_cooldown=index == 0,
                )
                self._log_voice_event(
                    event,
                    "synthesized",
                    request=segment_request,
                    result=synthesis,
                    elapsed_ms=(time.monotonic() - segment_started_at) * 1000,
                )
                delivered = await self.send_reply(
                    websocket,
                    event,
                    [OutgoingMessage("record", str(synthesis.audio_path))],
                    fallback_text=text,
                    model_type="voice",
                    model_name=self._delivery_model_name("voice"),
                    first_delay=index == 0,
                )
                if not delivered:
                    # 音频文件已经生成，但 NapCat 可能拒绝了 record 上传。
                    # 不能因为语音通道失败而吞掉已经生成好的回复文本。
                    remaining_text = _voice_remaining_text(
                        request.text,
                        segments,
                        delivered_segments,
                        index,
                    )
                    text_delivered = await self.send_reply(
                        websocket,
                        event,
                        [OutgoingMessage("text", remaining_text)],
                        model_type="voice",
                        model_name=self._delivery_model_name("voice"),
                    )
                    if text_delivered:
                        return "".join(delivered_segments) + text_delivered
                    return ""
                delivered_segments.append(text)
            except Exception as exc:
                LOGGER.error(
                    "语音分段合成失败：conversation=%s segment=%s error=%s",
                    conversation_id,
                    index + 1,
                    exc,
                )
                self._log_voice_event(
                    event,
                    "synthesis_failed",
                    request=segment_request,
                    error=exc,
                    elapsed_ms=(time.monotonic() - segment_started_at) * 1000,
                )
                # TTS 是可选的呈现方式，不应成为回复链路的单点故障。
                # 已成功发送的语音不再重复；只把尚未发送的部分转成文本。
                remaining_text = _voice_remaining_text(
                    request.text,
                    segments,
                    delivered_segments,
                    index,
                )
                text_delivered = await self.send_reply(
                    websocket,
                    event,
                    [OutgoingMessage("text", remaining_text)],
                    model_type="voice",
                    model_name=self._delivery_model_name("voice"),
                )
                if text_delivered:
                    return "".join(delivered_segments) + text_delivered
                return ""
            if index < len(segments) - 1:
                await asyncio.sleep(_send_delay(self.config))
        return str(request.text or "").strip()

    async def _synthesize_voice_segment(
        self,
        conversation_id: str,
        request: VoiceRequest,
        *,
        enforce_cooldown: bool,
    ) -> Any:
        """One guarded seam for all conversational TTS segments."""

        if enforce_cooldown:
            return await self.voice.synthesize(conversation_id, request)
        return await self.voice.synthesize(
                conversation_id,
                request,
                enforce_cooldown=False,
            )

    def _delivery_model_name(self, model_type: str) -> str:
        if model_type == "vision":
            return str(
                getattr(self.config, "toolbox_vision_model", "")
                or getattr(self.config, "toolbox_vision_fallback_model", "")
                or ""
            )[:160]
        if model_type == "voice":
            return str(
                getattr(self.config, "voice_zh_profile", "")
                or getattr(self.config, "voice_profile", "")
                or ""
            )[:160]
        return str(getattr(self.config, "openai_model", "") or "")[:160]

    async def send_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        messages: str | list[OutgoingMessage],
        fallback_text: str | None = None,
        model_type: str = "",
        model_name: str = "",
        first_delay: bool = True,
        group_quote: bool = True,
    ) -> str:
        outgoing = [OutgoingMessage("text", messages)] if isinstance(messages, str) else messages
        normalized_model_type = str(model_type or "").strip().lower()
        if normalized_model_type not in {"chat", "vision", "voice"}:
            normalized_model_type = "voice" if any(item.kind == "record" for item in outgoing) else "chat"
        delivered_parts: list[str] = []
        if outgoing and first_delay:
            # 真人不会在消息到达的同一瞬间发出回复：先停顿"看到消息 + 起手打字"。
            # 语音分段逐段调用本方法，由调用方用 first_delay=False 关掉重复停顿。
            await asyncio.sleep(
                _first_reply_delay(
                    self.config, _outgoing_delay_text(outgoing, fallback_text)
                )
            )
        # 指令返图合并：开关开启且这一组里含图片时，把文字和图片拼成一条消息
        # 整体发送。普通对话（无图片或只有一条）不进入此分支，保持逐条流式。
        if (
            getattr(self.config, "hd2_merge_messages", False)
            and len(outgoing) > 1
            and any(item.kind == "image" for item in outgoing)
        ):
            return await self._send_merged_reply(
                websocket, event, outgoing, group_quote=group_quote
            )
        for index, message in enumerate(outgoing):
            try:
                # 引用+@只加在第一个气泡，后续气泡不带前缀，避免重复刷屏。
                await self._send_one_reply(
                    websocket, event, message, group_quote=group_quote and index == 0
                )
                self._record_delivery_event(event, message=message, model_type=normalized_model_type, model_name=model_name, status="success")
            except Exception as exc:
                self._record_delivery_event(event, message=message, model_type=normalized_model_type, model_name=model_name, status="failed", error=str(exc))
                LOGGER.error(
                    "OneBot 发送失败：message_type=%s user=%s group=%s kind=%s error=%s",
                    event.get("message_type"),
                    event.get("user_id"),
                    event.get("group_id"),
                    message.kind,
                    exc,
                )
                if message.kind == "record" and fallback_text:
                    # 语音是可选的传输形式。record 上传失败时，直接在同一
                    # 个发送边界改发文本，避免上层把一次语音失败误判成整条
                    # 回复失败。这里不递归调用 send_reply，防止二次异常扩大。
                    text_fallback = OutgoingMessage("text", str(fallback_text).strip())
                    try:
                        await self._send_one_reply(
                            websocket, event, text_fallback, group_quote=group_quote
                        )
                        self._record_delivery_event(event, message=text_fallback, model_type=normalized_model_type, model_name=model_name, status="success")
                    except Exception as fallback_exc:
                        self._record_delivery_event(event, message=text_fallback, model_type=normalized_model_type, model_name=model_name, status="failed", error=str(fallback_exc))
                        LOGGER.error(
                            "语音失败后的文本兜底也发送失败：user=%s group=%s error=%s",
                            event.get("user_id"),
                            event.get("group_id"),
                            fallback_exc,
                        )
                        await self._mark_napcat_manual_login_required()
                        return ""
                    return str(fallback_text).strip()
                await self._mark_napcat_manual_login_required()
                return ""

            if message.kind == "text":
                delivered_parts.append(message.content)
            elif message.kind == "record" and fallback_text:
                delivered_parts.append(fallback_text)

            if index < len(outgoing) - 1:
                # 下一条要等"把这一条打完"的时间，长消息等得久、短句更干脆。
                await asyncio.sleep(
                    _typing_delay(self.config, _outgoing_delay_text([message], fallback_text))
                )
        return "\n".join(part for part in delivered_parts if part).strip()

    def _record_delivery_event(
        self,
        event: dict[str, Any],
        *,
        message: OutgoingMessage,
        model_type: str,
        model_name: str = "",
        status: str,
        error: str = "",
    ) -> None:
        if self.insights is None:
            return
        recorder = getattr(self.insights, "record_delivery", None)
        if not callable(recorder):
            return
        message_id = str(event.get("message_id") or uuid.uuid4().hex)
        recorder(
            event_id=f"{message_id}:{uuid.uuid4().hex[:8]}",
            conversation_id=_conversation_id(event),
            group_id=str(event.get("group_id") or ""),
            message_type=str(event.get("message_type") or ""),
            message_kind=str(message.kind or "text"),
            model_type=model_type,
            model_name=model_name,
            status=status,
            error=str(error or "")[:500],
        )

    def _smart_group_reply_allowed(
        self,
        event: dict[str, Any],
        conversation_id: str,
        plain_text: str,
    ) -> bool:
        if event.get("message_type") != "group":
            return True
        if self.config.reply_mode != "smart":
            return True
        if is_bot_mentioned(event, self.config.bot_qq, plain_text):
            return True

        now = datetime.now().timestamp()
        last_at = float(self._last_smart_group_reply_at.get(conversation_id, 0.0))
        return now - last_at >= SMART_GROUP_REPLY_COOLDOWN_SECONDS

    def _mark_smart_group_reply(
        self,
        event: dict[str, Any],
        conversation_id: str,
        plain_text: str,
    ) -> None:
        if event.get("message_type") != "group":
            return
        if self.config.reply_mode != "smart":
            return
        if is_bot_mentioned(event, self.config.bot_qq, plain_text):
            return
        self._last_smart_group_reply_at[conversation_id] = datetime.now().timestamp()

    def _group_reply_prefix_segments(
        self, event: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """构造群聊回复开头的引用(reply) + @对方(at) 段。"""
        prefix: list[dict[str, Any]] = []
        message_id = str(event.get("message_id") or "").strip()
        user_id = str(event.get("user_id") or "").strip()
        if message_id:
            prefix.append({"type": "reply", "data": {"id": message_id}})
        if user_id:
            prefix.append({"type": "at", "data": {"qq": user_id}})
            # @ 昵称后补一个空格，避免昵称和正文挤在一起。
            prefix.append({"type": "text", "data": {"text": " "}})
        return prefix

    async def _send_group_poke(
        self,
        websocket: Any,
        group_id: Any,
        target_id: Any,
    ) -> bool:
        """在群里主动戳一戳指定成员。失败只记录不抛出，避免拖垮主回复。"""
        if not getattr(self.config, "group_poke_enabled", True):
            return False
        if websocket is None or group_id in (None, "") or target_id in (None, ""):
            return False
        try:
            result = await self.call_action_and_wait(
                websocket,
                "group_poke",
                {"group_id": int(group_id), "user_id": int(target_id)},
                8.0,
            )
            return result is not None
        except Exception as exc:  # pragma: no cover - 网络/协议异常兜底
            LOGGER.warning("主动戳一戳失败 group=%s target=%s: %s", group_id, target_id, exc)
            return False

    def _with_group_reply_prefix(
        self,
        event: dict[str, Any],
        onebot_message: Any,
        *,
        group_quote: bool = True,
    ) -> Any:
        """群聊回复在消息开头加引用+@；私聊、开关关闭或本次禁用时原样返回。"""
        if (
            not group_quote
            or event.get("message_type") != "group"
            or not getattr(self.config, "group_reply_quote_enabled", True)
        ):
            return onebot_message
        # 统一成段数组，再把前缀拼到最前面。
        if isinstance(onebot_message, str):
            segments = (
                [{"type": "text", "data": {"text": onebot_message}}]
                if onebot_message
                else []
            )
        else:
            segments = onebot_message
        return self._group_reply_prefix_segments(event) + segments

    async def _send_one_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        message: OutgoingMessage,
        *,
        group_quote: bool = True,
    ) -> None:
        message_type = event.get("message_type")
        onebot_message = outgoing_to_onebot_message(message)
        if message.kind == "record":
            if message_type == "private":
                result = await self.call_action_and_wait(
                    websocket,
                    "send_private_msg",
                    {"user_id": event["user_id"], "message": onebot_message},
                    12.0,
                )
                try:
                    self._require_message_delivery(result)
                except RuntimeError as exc:
                    self._log_voice_event(event, "delivery_failed", error=str(exc))
                    raise
                self._log_voice_event(event, "delivered", message=message, result=result)
                return
            if message_type == "group":
                group_message = self._with_group_reply_prefix(
                    event, onebot_message, group_quote=group_quote
                )
                result = await self.call_action_and_wait(
                    websocket,
                    "send_group_msg",
                    {"group_id": event["group_id"], "message": group_message},
                    12.0,
                )
                try:
                    self._require_message_delivery(result)
                except RuntimeError as exc:
                    self._log_voice_event(event, "delivery_failed", error=str(exc))
                    raise
                self._log_voice_event(event, "delivered", message=message, result=result)
                return
        if message_type == "private":
            result = await self.call_action_and_wait(
                websocket,
                "send_private_msg",
                {"user_id": event["user_id"], "message": onebot_message},
                12.0,
            )
            self._require_message_delivery(result)
            return

        if message_type == "group":
            group_message = self._with_group_reply_prefix(
                event, onebot_message, group_quote=group_quote
            )
            result = await self.call_action_and_wait(
                websocket,
                "send_group_msg",
                {"group_id": event["group_id"], "message": group_message},
                12.0,
            )
            self._require_message_delivery(result)

    async def _send_merged_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        outgoing: list[OutgoingMessage],
        *,
        group_quote: bool = True,
    ) -> str:
        """把多条 OutgoingMessage 的消息段 flatten 成一条 QQ 消息整体发送。"""
        segments: list[dict[str, Any]] = []
        for message in outgoing:
            segments.extend(outgoing_to_segments(message))
        if not segments:
            return ""
        if event.get("message_type") == "group":
            merged_segments = self._with_group_reply_prefix(
                event, segments, group_quote=group_quote
            )
            result = await self.call_action_and_wait(
                websocket,
                "send_group_msg",
                {"group_id": event["group_id"], "message": merged_segments},
                12.0,
            )
        else:
            result = await self.call_action_and_wait(
                websocket,
                "send_private_msg",
                {"user_id": event["user_id"], "message": segments},
                12.0,
            )
        self._require_message_delivery(result)
        # 返回值与逐条路径保持一致：拼接已送达的文本，供上层记录/归档。
        return "\n".join(
            item.content for item in outgoing if item.kind == "text" and item.content
        ).strip()

    def _require_message_delivery(self, result: dict[str, Any] | None) -> None:
        try:
            _require_onebot_action_success(result)
        except RuntimeError:
            detail = _onebot_action_error_detail(result, default="QQ 消息发送失败")
            publish_onebot_probe_result(False, detail=detail, self_id=self.config.bot_qq)
            self._onebot_probe_failures += 1
            raise
        self._onebot_probe_failures = 0
        publish_onebot_probe_result(
            True,
            detail="QQ 消息发送成功",
            self_id=self.config.bot_qq,
        )

    async def call_action(
        self, websocket: Any, action: str, params: dict[str, Any]
    ) -> None:
        payload = {
            "action": action,
            "params": params,
            "echo": f"atri-{uuid.uuid4().hex}",
        }
        LOGGER.debug("调用 OneBot action=%s", action)
        await websocket.send(json.dumps(payload, ensure_ascii=False))

    async def fetch_group_list(self) -> list[dict[str, Any]]:
        """从当前 NapCat 连接读取群列表，供 WebUI 显示群名；失败时返回空列表。"""
        for websocket in tuple(self._active_websockets):
            try:
                result = await self.call_action_and_wait(websocket, "get_group_list", {}, 4.0)
            except Exception as exc:
                LOGGER.debug("读取 QQ 群列表失败：%s", exc)
                continue
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                continue
            groups: list[dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict) or not item.get("group_id"):
                    continue
                groups.append(
                    {
                        "group_id": str(item.get("group_id")),
                        "group_name": str(item.get("group_name") or "").strip()[:200],
                    }
                )
            return groups
        return []

    async def fetch_group_history(
        self,
        group_id: str,
        start: datetime,
        end: datetime,
        limit: int,
        progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]]:
        """按需从当前登录 QQ 客户端读取一个时间范围内的群历史。

        这是 NapCat 暴露给已登录 QQ 内核的 ``get_group_msg_history``，不是
        ATRI 的实时事件归档。分页直到覆盖 ``start``，因此 ATRI 昨天未运行
        不会让今天的手动日报少统计那段时间的客户端历史。
        """

        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            raise ValueError("群号不能为空")
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("历史记录读取时间范围无效")

        maximum = max(100, min(20000, int(limit)))
        # ``get_group_msg_history`` 在部分 QQ NT 内核上一次转换大量消息会阻塞
        # 回执，继而使反向 WS 心跳超时。使用 NapCat 的默认页大小，换取稳定的
        # 小批次读取；仅在用户手动生成报告时运行，不影响日常消息链路。
        # 20 条一页会让高活跃群在 24 小时窗口内产生数百次 WS 往返；50 条仍
        # 明显低于曾引发 QQ 内核阻塞的 100 条大页，同时可大幅缩短手动日报等待。
        page_size = min(50, maximum)
        cursor = ""
        seen_cursors: set[str] = set()
        seen_messages: set[str] = set()
        matched: list[dict[str, Any]] = []
        reached_start = False

        # 首次调用取最新一页；之后以本页最早的消息为游标，并显式要求向更早
        # 的方向翻页。此前 ``reverse_order=False`` 会反复落到较新的记录，最终
        # 在去重时静默结束，造成“24 小时”只采到一页的假象。
        for page_index in range((maximum + page_size - 1) // page_size + 2):
            params: dict[str, Any] = {
                "group_id": normalized_group_id,
                "count": page_size,
                "reverse_order": bool(cursor),
                # 日报只需文本和媒体类型，不需要为每张历史图片额外解析 URL，
                # 也无需展开合并转发；这两项是大群历史读取的主要耗时来源。
                "disable_get_url": True,
                "parse_mult_msg": False,
            }
            if cursor:
                params["message_seq"] = cursor
            result: dict[str, Any] | None = None
            last_error: Exception | None = None
            # NapCat 重连会替换 websocket；每次重试重新取活动连接，避免把已
            # 关闭的 socket 固定到整次历史导入中。
            for attempt in range(3):
                websocket = self._first_active_websocket()
                if websocket is None:
                    last_error = RuntimeError("NapCat 当前未连接")
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                try:
                    result = await self.call_action_and_wait(
                        websocket, "get_group_msg_history", params, 30.0
                    )
                    break
                except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                    last_error = exc
                    LOGGER.warning(
                        "读取群历史超时或连接重置，准备重试：group=%s page=%s attempt=%s error=%r",
                        normalized_group_id, len(matched), attempt + 1, exc,
                    )
                    await asyncio.sleep(0.8 * (attempt + 1))
            if result is None:
                raise RuntimeError(
                    "QQ 客户端未响应群历史读取请求；NapCat 已自动重试 3 次"
                    + (f"（{last_error!r}）" if last_error is not None else "")
                )
            if not isinstance(result, dict) or str(result.get("status") or "ok").lower() != "ok":
                detail = ""
                if isinstance(result, dict):
                    detail = str(result.get("wording") or result.get("message") or result.get("msg") or "")
                raise RuntimeError(f"QQ 客户端历史读取失败{f'：{detail}' if detail else ''}")
            data = result.get("data") if isinstance(result, dict) else None
            batch = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(batch, list) or not batch:
                if reached_start:
                    break
                raise RuntimeError("QQ 客户端历史分页在到达周期起点前提前结束，未使用不完整消息生成报告")

            oldest: datetime | None = None
            # 游标必须对应时间上最早的一条消息，不能依赖短 ID 的数值大小。
            # QQ 短 ID 并不承诺按时间单调递增。
            oldest_cursor: tuple[datetime, str] | None = None
            new_in_page = 0
            for item in batch:
                if not isinstance(item, dict):
                    continue
                item_group_id = str(item.get("group_id") or normalized_group_id)
                if item_group_id != normalized_group_id:
                    continue
                message_key = str(item.get("message_id") or item.get("message_seq") or item.get("id") or "")
                if not message_key:
                    message_key = f"{item.get('user_id')}:{item.get('time')}:{item.get('raw_message')}"
                if message_key in seen_messages:
                    continue
                seen_messages.add(message_key)
                new_in_page += 1
                occurred_at = _history_event_time(item)
                if occurred_at is not None:
                    oldest = occurred_at if oldest is None or occurred_at < oldest else oldest
                    if occurred_at < start:
                        reached_start = True
                    if start <= occurred_at < end:
                        matched.append(item)
                # NapCat 的历史消息在不同版本中可能只带 ``message_id``。
                # 该值就是 get_group_msg_history 接受的 short id；若只认
                # message_seq 会在第一页提前停止，导致“过去 24h”被截断。
                sequence = str(item.get("message_seq") or "").strip()
                # 该字段常被 NapCat 填为字符串 "0"，语义仍是“未提供游标”，
                # 必须退回到可分页的 message_id，不能把 "0" 再发回去。
                if not sequence or sequence == "0":
                    sequence = str(item.get("message_id") or "").strip()
                if occurred_at is not None and sequence:
                    candidate = (occurred_at, sequence)
                    if oldest_cursor is None or candidate < oldest_cursor:
                        oldest_cursor = candidate

            if progress is not None and (page_index == 0 or (page_index + 1) % 5 == 0 or reached_start):
                await progress(page_index + 1, len(seen_messages))

            if reached_start:
                break
            if len(seen_messages) >= maximum:
                raise RuntimeError(
                    f"QQ 客户端历史在指定周期内超过 {maximum} 条；请提高 GROUP_SUMMARY_MAX_MESSAGES 后重试"
                )
            if oldest_cursor is None:
                raise RuntimeError("QQ 客户端历史缺少可用于分页的消息游标，未使用不完整消息生成报告")
            next_cursor = oldest_cursor[1]
            if not next_cursor or next_cursor in seen_cursors or new_in_page == 0:
                raise RuntimeError("QQ 客户端历史分页游标未向更早消息推进，未使用不完整消息生成报告")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        if not reached_start and len(matched) >= maximum:
            raise RuntimeError(
                f"QQ 客户端历史未读取到周期起点，已达到 {maximum} 条上限；请提高 GROUP_SUMMARY_MAX_MESSAGES 后重试"
            )
        return sorted(matched, key=lambda item: (_history_event_time(item) or end, str(item.get("message_seq") or "")))

    async def call_action_and_wait(
        self,
        websocket: Any,
        action: str,
        params: dict[str, Any],
        timeout_seconds: float = 8.0,
    ) -> dict[str, Any] | None:
        echo = f"atri-tool-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_actions[echo] = future
        payload = {
            "action": action,
            "params": params,
            "echo": echo,
        }
        try:
            LOGGER.debug("调用并等待 OneBot action=%s timeout=%.1fs", action, timeout_seconds)
            await websocket.send(json.dumps(payload, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=max(1.0, float(timeout_seconds)))
        finally:
            self._pending_actions.pop(echo, None)

    def _resolve_pending_action(self, event: dict[str, Any]) -> None:
        echo = str(event.get("echo") or "")
        future = self._pending_actions.pop(echo, None)
        if future is not None and not future.done():
            future.set_result(event)

    def _log_task_exception(self, task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                LOGGER.error(
                    "消息任务异常：%s",
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
