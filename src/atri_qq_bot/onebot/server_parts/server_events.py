from __future__ import annotations

import logging
import time

from .server_support import OneBotServerMixin, _sync_server_globals

# 显式导入而不是依赖 _sync_server_globals 的全局注入：注入只在**实例**属性访问时
# 才把 server.py 的名字塞进本模块，直接以类方法调用（测试、脚本）时会 NameError。
from .helpers import _conversation_id
from ..message_parser import (  # type: ignore[import-not-found]
    extract_reply_message_id,
    group_increase_text,
)
from ..reply_policy import (  # type: ignore[import-not-found]
    _at_segment_targets,
    _has_reply_segment,
    poke_command_target,
)
from ...group_reply_focus import _strip_placeholder_text  # type: ignore[import-not-found]
from ...runtime.reply_strategy import (  # type: ignore[import-not-found]
    forced_event_block_reason,
    object_requires_mention,
)

# 运行时会由 _sync_server_globals 注入 server.py 的同名 logger；这里显式声明一份，
# 便于模块被单独导入（单元测试）时也能正常记录。
LOGGER = logging.getLogger("atri.onebot")


class OneBotEventMixin(OneBotServerMixin):
    async def _resolve_group_increase_name(
        self, websocket: Any, event: dict[str, Any]
    ) -> dict[str, Any]:
        """入群通知不带昵称，额外问一次 QQ 内核；失败就退回 QQ 号。

        纯增益步骤：拿不到昵称也必须照常欢迎，所以这里把所有异常都吞掉。
        群名片优先于昵称——群里显示的就是群名片。
        """
        info = dict(event.get("_atri_group_increase_member") or {})
        group_id = info.get("group_id") or event.get("group_id")
        user_id = info.get("user_id") or event.get("user_id")
        name = str(info.get("nickname") or "").strip()

        if not name and group_id is not None and user_id is not None:
            try:
                result = await self.call_action_and_wait(
                    websocket,
                    "get_group_member_info",
                    {
                        "group_id": int(group_id),
                        "user_id": int(user_id),
                        "no_cache": False,
                    },
                    5.0,
                )
                data = result.get("data") if isinstance(result, dict) else None
                if isinstance(data, dict):
                    name = str(data.get("card") or data.get("nickname") or "").strip()
            except Exception as exc:
                LOGGER.debug("读取新成员昵称失败，改用 QQ 号：%s", exc)

        # 无论名字来自通知还是刚查到的，都用**同一个格式函数**重写文本。
        # 不这样做的话，"通知自带昵称"这条路径会依赖归一化阶段生成的文本，
        # 形成一处看不见的耦合（测试抓到过）。
        text = group_increase_text(name, user_id)
        enriched = dict(event)
        enriched["message"] = [{"type": "text", "data": {"text": text}}]
        enriched["raw_message"] = text
        enriched["_atri_group_increase_member"] = {**info, "nickname": name}
        return enriched

    async def _handle_event(self, websocket: Any, event: dict[str, Any]) -> None:
        """执行一次完整的入站消息决策和回复流程。"""
        # 补昵称要在抽取纯文本之前——否则回复链路拿到的还是"QQ 号"版本。
        if event.get("_atri_group_increase_event"):
            event = await self._resolve_group_increase_name(websocket, event)

        plain_text = extract_plain_text(event.get("message"))

        # Helldivers 指令守卫：命中以 / 开头的情报命令时，直接取情报并发送后
        # return，绕开回复策略与 LLM。因此群里无需 @ 机器人也能触发情报查询。
        helldivers = getattr(self, "helldivers", None)
        if helldivers is not None:
            # 传入会话标识，订阅/退订命令据此记录要推送到哪个群/私聊。
            hd2_session_key = _conversation_id(event)
            hd2_messages = await helldivers.handle(
                plain_text, hd2_session_key
            )
            if hd2_messages is not None:
                # 确定性查询命令：用 first_delay=False 跳过拟人打字停顿，响应更快；
                # group_quote=False 让情报返图不引用、不 @ 发令者。
                await self.send_reply(
                    websocket,
                    event,
                    hd2_messages,
                    first_delay=False,
                    group_quote=False,
                )
                return

        # 指令戳人：“亚托莉戳一下 @某人”，先执行戳人动作，再让正常流程配一句话。
        poke_target = poke_command_target(event, self.config.bot_qq)
        poke_command_hit = poke_target is not None
        poke_action_done = False
        if poke_command_hit:
            poke_action_done = await self._send_group_poke(
                websocket, event.get("group_id"), poke_target
            )

        poke_event = bool(event.get("_atri_poke_event"))
        # 入群通知归一化后也是普通群消息，但和戳一戳一样**强制回复**——
        # 否则群聊默认策略不会理一条没有 @ 的消息，欢迎就发不出去。
        join_event = bool(event.get("_atri_group_increase_event"))
        push_event = poke_event or join_event
        is_message = (
            event.get("post_type") == "message"
            and _as_int(event.get("user_id")) != self.config.bot_qq
            and _as_int(event.get("self_id")) in {None, self.config.bot_qq}
        )

        # 被戳时先回戳对方（再走下面的文字回复），像真人互相戳着玩。
        # 回戳用独立短冷却：连续戳都能回戳，只防秒级狂戳。
        poke_back_done = False
        if poke_event and self._allow_poke_back(event):
            poke_back_done = await self._send_group_poke(
                websocket, event.get("group_id"), event.get("user_id")
            )

        # 文字回复用独立长冷却：冷却期内只回戳、不生成大段文字，避免文字刷屏。
        if poke_event and not self._allow_poke_text(event):
            LOGGER.info(
                "戳一戳文字回复进入冷却：conversation=%s user=%s",
                _conversation_id(event),
                event.get("user_id"),
            )
            return

        # 触发判断前先确定被引用者：能补齐 event.reply 时，后续"引用了谁"的
        # 判断就是确定值，而不是在信息缺失时盲目抢答。
        if is_message and _has_reply_segment(event):
            await self._ensure_quoted_sender_id(websocket, event)

        preliminary_reply = push_event or poke_command_hit or should_reply(
            event,
            self.config.bot_qq,
            self.config.reply_mode,
            self.config.owner_qqs,
        )
        active_group_continuation = False
        explicit_group_mention = bool(
            is_message
            and event.get("message_type") == "group"
            and not push_event
            and is_bot_mentioned(event, self.config.bot_qq, plain_text)
        )
        if explicit_group_mention:
            # 「仅 @ 回复」的群不存在"续聊"：会话一旦打开，该成员之后的表情和插话
            # 都会被当成续聊（is_continuation）绕过 @ 检查，而且每次命中还会滑动续期。
            # 这里直接不打开会话，让"每轮都要 @ "的语义从源头成立。
            if not object_requires_mention(
                scope="group",
                user_id=str(event.get("user_id") or ""),
                group_id=str(event.get("group_id") or ""),
            ):
                self._group_reply_focus.open_session(
                    event,
                    nickname=_nickname(event) or "",
                )
        elif is_message and event.get("message_type") == "group" and not push_event:
            active_group_continuation = self._group_reply_focus.is_active_continuation(
                event,
                plain_text,
            )
            preliminary_reply = preliminary_reply or active_group_continuation
            # 没有任何明确触发时，普通群聊由模型按内容判断是否搭话（带冷却）。
            if not preliminary_reply:
                preliminary_reply = await self._maybe_ambient_group_reply(event, plain_text)
        voice_transcript = None
        voice_error: Exception | None = None
        if is_message and (event.get("message_type") == "private" or preliminary_reply):
            try:
                voice_transcript = await self.voice.transcribe_event(
                    event,
                    lambda action, params: self.call_action_and_wait(
                        websocket,
                        action,
                        params,
                        self.config.voice_service_timeout_seconds,
                    ),
                )
            except Exception as exc:
                voice_error = exc
                print(f"[onebot] Voice transcription skipped: {exc}")
        if voice_transcript is not None:
            plain_text = voice_transcript.text

        if is_message:
            with contextlib.suppress(Exception):
                await self.stickers.capture_from_event(
                    event,
                    plain_text,
                    self.config.sticker_capture_enabled,
                    self.config.sticker_capture_max_bytes,
                )

        should_send_reply = preliminary_reply
        conversation_id = _conversation_id(event)
        if is_message:
            self._remember_pending_group_visual(event)
        # 入群事件**刻意绕开下面三道限流**（用户明确要求"入群必回"）：
        # 群内 75 秒冷却、blocked 档案、回复策略的**频率**门控。语义上它是一次"事件"
        # 而不是一次群聊插话——同一批人陆续进群时不该因为刚回过话就漏掉欢迎。
        # 唯一的例外是对象级模式（禁止回复 / 只记录 / 仅 @ 回复）：那表达的是"这个对象
        # 不该被主动搭话"，优先级高于"入群必回"，在下面单独判定。
        if (
            should_send_reply
            and not active_group_continuation
            and not join_event
            and not self._smart_group_reply_allowed(
                event,
                conversation_id,
                plain_text,
            )
        ):
            should_send_reply = False
        profile_id = _profile_id(event)
        nickname = _nickname(event)
        is_owner = _as_int(event.get("user_id")) in set(self.config.owner_qqs)
        trust_tier = getattr(self.reply_engine.memory, "trust_tier", None)
        if (
            is_message
            and not join_event
            and not is_owner
            and callable(trust_tier)
            and trust_tier(profile_id) == "blocked"
        ):
            should_send_reply = False
        if should_send_reply and join_event:
            # 入群欢迎没有 @、也不构成续聊，因此不会被下面的策略门控拦住。对象级模式
            # 必须在这里单独生效，否则"只记录"和"仅 @ 回复"的群会继续收到入群欢迎
            # ——实测 19:16 的那条欢迎就是在 quiet（只记录）模式下发出去的。
            # （戳一戳不需要在这里判：它没被排除在下面的门控之外，已经受对象策略约束。）
            forced_block = forced_event_block_reason(
                scope="group" if event.get("message_type") == "group" else "private",
                user_id=str(event.get("user_id") or ""),
                group_id=str(event.get("group_id") or ""),
                is_owner=is_owner,
            )
            if forced_block:
                LOGGER.info(
                    "强制事件被对象策略拦截：conversation=%s user=%s group=%s reason=%s",
                    conversation_id,
                    event.get("user_id"),
                    event.get("group_id"),
                    forced_block,
                )
                should_send_reply = False
        # 回复策略（按对象/好感度/活跃度的门控与频率上限）在策略启用时才生效；
        # strategy_limits 同时承载本轮的长度策略，None 表示策略未启用。
        strategy_limits: dict[str, Any] | None = None
        if is_message and should_send_reply and not join_event:
            decision = self._evaluate_reply_strategy(
                event,
                profile_id=profile_id,
                plain_text=plain_text,
                is_owner=is_owner,
                is_continuation=active_group_continuation,
            )
            if not decision.allowed:
                LOGGER.info(
                    "回复策略拦截：conversation=%s user=%s group=%s reason=%s",
                    conversation_id,
                    event.get("user_id"),
                    event.get("group_id"),
                    decision.reason,
                )
                should_send_reply = False
            elif decision.strategy:
                strategy_limits = decision.strategy
        addressed_to_bot = (
            event.get("message_type") == "private"
            or push_event
            or (
                event.get("message_type") == "group"
                and (should_send_reply or is_bot_mentioned(event, self.config.bot_qq, plain_text))
            )
        )
        group_reply_focus = None
        if is_message and event.get("message_type") == "group" and not push_event:
            group_reply_focus = self._group_reply_focus.resolve(
                event,
                plain_text,
                nickname=nickname or "",
                addressed_to_bot=addressed_to_bot,
            )
            if active_group_continuation and group_reply_focus is not None:
                group_reply_focus = replace(group_reply_focus, source="active_session")
            if not addressed_to_bot:
                self._group_reply_focus.remember(
                    event,
                    plain_text,
                    nickname=nickname or "",
                    addressed_to_bot=False,
                )

        if is_message:
            self._log_message_decision(event, plain_text, should_send_reply)
            LOGGER.info(
                "消息决策：conversation=%s reply=%s mode=%s",
                conversation_id,
                should_send_reply,
                self.config.reply_mode,
            )

        if is_message:
            self.reply_engine.remember_target(conversation_id, event)
            if profile_id != conversation_id:
                self.reply_engine.remember_target(profile_id, event)

            if event.get("message_type") == "group":
                self.reply_engine.observe_group_incoming(
                    event.get("group_id"),
                    event.get("user_id"),
                    plain_text,
                    nickname,
                    runtime_context=(
                        self.config.group_context_enabled and not should_send_reply
                    ),
                    addressed_to_bot=addressed_to_bot,
                    is_owner=is_owner,
                )
            else:
                self.reply_engine.observe_incoming(
                    conversation_id,
                    plain_text,
                    nickname,
                    actor_id=event.get("user_id"),
                    runtime_context=False,
                    profile_id=profile_id,
                    event_id=str(event.get("message_id") or "") or None,
                )

        if not should_send_reply:
            return

        # 常见点歌表达在聊天模型之前处理。它们只读取 data/music 直属的成品，
        # 不受模型工具调用、TTS 开关或群聊提示词影响，也不会用文字假装唱歌。
        turn_id = str(event.get("message_id") or uuid.uuid4().hex)
        singing_decision = self.singing.resolve(profile_id, plain_text)
        if singing_decision.handled and not is_event_modality_allowed(event, "voice"):
            # 点歌属于语音模型能力；未授权时回到普通文本回复，不读取或发送音频。
            LOGGER.info("语音白名单拒绝点歌：user=%s group=%s", event.get("user_id"), event.get("group_id"))
            singing_decision = replace(singing_decision, handled=False)
        if singing_decision.handled:
            if self.cognition_shadow is not None:
                self.cognition_shadow.submit(
                    ConversationTurnRequest(
                        turn_id=turn_id,
                        conversation_id=conversation_id,
                        profile_id=profile_id,
                        kind="group" if event.get("message_type") == "group" else "private",
                        user_text=plain_text,
                        nickname=nickname,
                        actor_id=str(event.get("user_id") or "") or None,
                        addressed_to_bot=addressed_to_bot,
                    ),
                    profile=self.reply_engine.profile_for(profile_id),
                )
            outgoing, fallback_text = self._singing_outgoing(
                event,
                plain_text,
                singing_decision,
            )
            delivered_text = await self.send_reply(
                websocket,
                event,
                outgoing,
                fallback_text=fallback_text or None,
                first_delay=False,
                model_type="voice",
                model_name=self._delivery_model_name("voice"),
            )
            if outgoing and not delivered_text:
                self._record_reply_failure(
                    event,
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    user_text=plain_text,
                    model_reply=singing_decision.reply_text,
                    reason="歌曲语音或文本发送失败",
                    model_type="voice",
                    model_name=self._delivery_model_name("voice"),
                )
                return
            recorded_reply = delivered_text
            if self.cognition_shadow is not None:
                self.cognition_shadow.record_live_outcome(
                    turn_id,
                    reply_text=singing_decision.reply_text,
                    delivered_text=recorded_reply,
                    voice_mode="singing" if singing_decision.song else "",
                )
            self._mark_smart_group_reply(event, conversation_id, plain_text)
            self.reply_engine.record_bot_reply(
                conversation_id,
                recorded_reply,
                False,
                profile_id=profile_id,
            )
            self._log_reply_event(
                event,
                conversation_id=conversation_id,
                user_text=plain_text,
                model_reply="",
                delivered_reply=recorded_reply,
                voice_mode="singing" if singing_decision.song else "",
                reply_focus_source=str(getattr(group_reply_focus, "source", "") or ""),
                reply_focus_text=str(getattr(group_reply_focus, "focus_text", "") or ""),
            )
            return

        if group_reply_focus is not None and group_reply_focus.source == "previous_same_sender":
            analysis_event = group_reply_focus.analysis_event
            self._pending_group_visual_events.pop(_pending_visual_key(event), None)
        else:
            analysis_event = self._event_with_pending_group_visual(event)
        # 被 @ 回复时，顺带把群里近期图片/表情合并进本次分析（平时不主动分析）。
        analysis_event = self._event_with_recent_group_visuals(analysis_event)

        quoted_message_id, quoted_text, quoted_segments = await self._resolve_quoted_message(
            websocket,
            event,
        )
        if quoted_segments:
            analysis_event = self._event_with_quoted_message(
                analysis_event,
                quoted_segments,
            )
        model_input_text = _compose_model_input(plain_text, quoted_text)
        if poke_action_done:
            # 明确告诉模型戳人动作已经完成，避免它回复“我不能操作”。
            model_input_text = (
                f"{model_input_text}\n\n"
                "【系统动作反馈】你已经成功戳了一下对方，这个戳一戳动作已经由系统完成，"
                "对方能看到。请自然地确认这件事，简短回应一句即可，不要说自己做不到、"
                "不能操作、只能喊一声之类的话。"
            )
        elif poke_back_done:
            # 被戳后已经回戳；回戳只是个小动作，引导模型自由接话，不要每次都确认它。
            model_input_text = (
                f"{model_input_text}\n\n"
                "【系统动作反馈】你刚刚已经回戳了对方一下，这个小动作对方已经看到，"
                "不必再描述或确认它。被戳之后想说什么都行，像真人朋友打闹那样："
                "可以傲娇嫌弃、吐槽、反问、撒娇，也可以不接这个梗、直接聊点别的。"
                "回复短一点、口语化，别每次都围着“回戳/扯平”说，也不要说自己做不到、"
                "不能操作。下面只是方向，学感觉、每次换着来，不要照抄："
                "“戳我干嘛啦，无聊。”“你是不是闲的？”“哼，我大人有大量，不跟你计较。”"
                "“就这？没吃饭吗。”“别戳别戳，再戳要收费了啊。”“怎么，想我了？”"
            )
        voice_only_text = plain_text.replace("@群友", "").strip()
        if (
            voice_error is not None
            and find_record_segments(event.get("message"))
            and voice_only_text == "[语音]"
        ):
            self._record_reply_failure(
                event,
                turn_id=turn_id,
                conversation_id=conversation_id,
                user_text=model_input_text,
                model_reply="",
                reason=f"语音识别失败：{voice_error}",
                model_type="voice",
                model_name=self._delivery_model_name("voice"),
            )
            return

        has_current_visual = _event_has_visual_material(analysis_event)
        if has_current_visual:
            self._recent_visual_contexts.pop(conversation_id, None)

        tool_context = None
        vision_allowed = is_event_modality_allowed(event, "vision")
        try:
            tool_context = await self.tools.analyze(
                analysis_event,
                model_input_text,
                lambda action, params: self.call_action_and_wait(
                    websocket,
                    action,
                    params,
                    self.config.toolbox_timeout_seconds,
                ),
                allow_vision=vision_allowed,
            )
        except Exception as exc:
            LOGGER.warning("多模态/工具分析跳过：conversation=%s error=%s", conversation_id, exc)
        if tool_context is not None and conversation_id.startswith("private:"):
            try:
                from ...knowledge.ingestion import archive_analysis

                knowledge = getattr(self.reply_engine.memory, "knowledge", None)
                if knowledge is not None:
                    archive_analysis(knowledge, conversation_id, tool_context,
                                     event_id=str(event.get("message_id") or ""), occurred_at=event.get("time"))
            except Exception as exc:
                LOGGER.warning("资料存档失败：conversation=%s error=%s", conversation_id, exc)
        if vision_allowed and tool_context is None and _should_reuse_cached_visual(
            analysis_event,
            model_input_text,
        ):
            cached = self._recent_visual_contexts.get(conversation_id)
            if cached and time.time() - cached[0] <= VISUAL_CONTEXT_TTL_SECONDS:
                try:
                    if getattr(cached[1], "visual_data", None):
                        tool_context = await self.tools.analyze_visual_followup(
                            cached[1],
                            model_input_text,
                            allow_vision=vision_allowed,
                        )
                except Exception as exc:
                    LOGGER.warning("图片追问分析失败：conversation=%s error=%s", conversation_id, exc)
                if tool_context is None:
                    tool_context = cached[1]
            elif cached:
                self._recent_visual_contexts.pop(conversation_id, None)
        if tool_context is not None and (
            has_current_visual or getattr(tool_context, "visual_data", None)
        ):
            self._recent_visual_contexts[conversation_id] = (time.time(), tool_context)
            if len(self._recent_visual_contexts) > 256:
                oldest = min(
                    self._recent_visual_contexts,
                    key=lambda key: self._recent_visual_contexts[key][0],
                )
                self._recent_visual_contexts.pop(oldest, None)
        if voice_transcript is not None:
            tool_context = VoicePromptContext(
                voice_transcript,
                material_context=tool_context,
                prefer_voice_reply=bool(self.config.voice_reply_to_voice),
            )

        if self.cognition_shadow is not None:
            request = ConversationTurnRequest(
                turn_id=turn_id,
                conversation_id=conversation_id,
                profile_id=profile_id,
                kind="group" if event.get("message_type") == "group" else "private",
                user_text=model_input_text,
                nickname=nickname,
                actor_id=str(event.get("user_id") or "") or None,
                quoted_text=quoted_text,
                reply_focus_source=str(getattr(group_reply_focus, "source", "") or ""),
                reply_focus_text=str(getattr(group_reply_focus, "focus_text", "") or ""),
                addressed_to_bot=addressed_to_bot,
            )
            profile_snapshot = self.reply_engine.profile_for(profile_id)
            self.cognition_shadow.submit(
                request,
                evidence_from_tool_context(tool_context),
                profile=profile_snapshot,
            )

        try:
            reply_text = await self.reply_engine.reply(
                conversation_id,
                model_input_text,
                nickname,
                profile_id=profile_id,
                observed=True,
                tool_context=tool_context,
                reply_focus=group_reply_focus,
                extra_system=self._reply_strategy_system_prompt(strategy_limits),
            )
        except Exception as exc:
            LOGGER.exception("回复引擎异常：conversation=%s", conversation_id)
            reply_text = ""
            failure_reason = f"{type(exc).__name__}: {exc}"
        else:
            consume_failure = getattr(self.reply_engine, "consume_reply_failure", None)
            failure_reason = (
                str(consume_failure(conversation_id) or "").strip()
                if callable(consume_failure)
                else ""
            )
        if not str(reply_text or "").strip():
            reason = failure_reason or "回复引擎返回空结果"
            self._record_reply_failure(
                event,
                turn_id=turn_id,
                conversation_id=conversation_id,
                user_text=model_input_text,
                model_reply="",
                reason=reason,
                tool_context=tool_context,
                reply_focus=group_reply_focus,
                quoted_message_id=quoted_message_id,
                model_type=("vision" if has_current_visual or getattr(tool_context, "visual_data", None) else "chat"),
                model_name=self._delivery_model_name("vision" if has_current_visual or getattr(tool_context, "visual_data", None) else "chat"),
            )
            # 2026-11-07 改动：回复失败不再完全静默。
            # 旧逻辑记录后直接 return，用户侧收不到任何东西（表现为“AI 卡住”，
            # 日志只有 ReadTimeout）。现在发一句降级话术，让用户知道这轮没接住、
            # 可以再说一次，而不是干等。只发文字，不带引用、不延迟，避免二次阻塞。
            try:
                await self.send_reply(
                    websocket,
                    event,
                    "唔……这轮我想太久，脑袋卡了一下。你再说一遍？",
                    model_type="chat",
                    first_delay=False,
                    group_quote=False,
                )
            except Exception as send_exc:  # noqa: BLE001
                LOGGER.warning("失败兜底消息发送失败：%s", send_exc)
            if self.cognition_shadow is not None:
                self.cognition_shadow.record_live_outcome(
                    turn_id,
                    reply_text="",
                    delivered_text="",
                    voice_mode="",
                )
            return
        voice_request = self.reply_engine.consume_voice_request(conversation_id)
        consume_reply_voice_choice = getattr(
            self.reply_engine,
            "consume_reply_voice_choice",
            None,
        )
        autonomous_voice_selected = bool(
            consume_reply_voice_choice(conversation_id)
            if callable(consume_reply_voice_choice)
            else False
        )
        if voice_request is not None:
            voice_request = stabilize_explicit_voice_request(
                plain_text,
                voice_request,
                max_chars=int(self.config.voice_max_chars),
            )
            voice_request = sanitize_voice_request(
                voice_request,
                max_chars=int(self.config.voice_max_chars),
            )
            if voice_request.mode == "speech" and not voice_request.text:
                # 只有动作/思考、没有可朗读正文时，宁可回普通文本也不把空内容送进 TTS。
                voice_request = None
        if voice_request is None and bool(self.config.voice_tts_enabled):
            policy = load_voice_behavior()
            profile = self.reply_engine.profile_for(profile_id)
            guarded_request = build_explicit_voice_fallback(
                plain_text,
                reply_text,
                max_chars=int(self.config.voice_max_chars),
            )
            if (
                guarded_request is not None
                and bool(policy.get("explicit_delivery_guard_enabled", True))
                and (
                    event.get("message_type") != "group"
                    or bool(self.config.voice_group_enabled)
                )
                and evaluate_voice_request(
                    conversation_id,
                    profile,
                    guarded_request.reason,
                    policy,
                ).allowed
            ):
                voice_request = guarded_request
            if voice_request is None and autonomous_voice_selected:
                autonomous_request = build_autonomous_voice_fallback(
                    plain_text,
                    reply_text,
                    max_chars=int(self.config.voice_max_chars),
                    reason="voice_reply" if voice_transcript is not None else "autonomous",
                )
                if (
                    autonomous_request is not None
                    and (
                        event.get("message_type") != "group"
                        or bool(self.config.voice_group_enabled)
                    )
                    and evaluate_voice_request(
                        conversation_id,
                        profile,
                        autonomous_request.reason,
                        policy,
                    ).allowed
                ):
                    voice_request = autonomous_request
        # 点歌只允许上面的固定会话入口；模型工具和旧请求不能绕过歌单确认。
        if voice_request is not None and voice_request.mode != "speech":
            voice_request = None
        if voice_request is not None and not is_event_modality_allowed(event, "voice"):
            # 保留本轮文字回复，但不进入 TTS，避免未授权用户消耗语音服务资源。
            LOGGER.info("语音白名单拒绝 TTS：user=%s group=%s", event.get("user_id"), event.get("group_id"))
            voice_request = None
        consume_call = getattr(self.reply_engine, "consume_call_request", None)
        call_request = consume_call(conversation_id) if callable(consume_call) else None
        if call_request is not None and not is_event_modality_allowed(event, "voice"):
            LOGGER.info("语音白名单拒绝通话邀请：user=%s group=%s", event.get("user_id"), event.get("group_id"))
            call_request = None
        # 模型在对话中因情绪自主决定要戳当前对话者，这里取出后在回复发送成功时执行。
        consume_poke = getattr(self.reply_engine, "consume_poke_request", None)
        poke_requested = consume_poke(conversation_id) if callable(consume_poke) else False
        spoken_text = ""
        voice_streamed = False
        delivered_text = ""
        if call_request is not None:
            policy = load_voice_behavior()
            session = self.voice_calls.create(
                conversation_id,
                call_request.topic,
                int(policy["call_expiry_minutes"]),
                int(policy["call_max_minutes"]),
            )
            call_url = f"{str(policy['call_base_url']).rstrip('/')}/voice-call?token={session.token}"
            topic_text = f"想继续聊聊“{call_request.topic}”。" if call_request.topic else "想和你继续说说话。"
            outgoing = [
                OutgoingMessage(
                    "text",
                    f"{topic_text}点这里接通语音：{call_url}\n邀请 {policy['call_expiry_minutes']} 分钟内有效。",
                )
            ]
        elif voice_request is not None:
            spoken_text = voice_request.text
            delivered_text = await self._send_streamed_speech_reply(
                websocket,
                event,
                conversation_id,
                voice_request,
            )
            outgoing = []
            voice_streamed = True
        else:
            profile = self.reply_engine.profile_for(profile_id)
            outgoing = build_outgoing_messages(
                reply_text,
                plain_text,
                self.stickers,
                self.config,
                profile,
            )
        delivery_model_type = (
            "voice"
            if voice_transcript is not None or voice_request is not None or voice_streamed
            else "vision"
            if has_current_visual or getattr(tool_context, "visual_data", None)
            else "chat"
        )
        delivery_model_name = self._delivery_model_name(delivery_model_type)
        sent_sticker = any(message.kind in {"image", "sticker", "face"} for message in outgoing)
        if not voice_streamed:
            delivered_text = await self.send_reply(
                websocket,
                event,
                outgoing,
                fallback_text=spoken_text or None,
                model_type=delivery_model_type,
                model_name=delivery_model_name,
            )
        if (voice_streamed or outgoing) and not delivered_text:
            self._record_reply_failure(
                event,
                turn_id=turn_id,
                conversation_id=conversation_id,
                user_text=model_input_text,
                model_reply=reply_text,
                reason="OneBot 未确认消息发送成功",
                tool_context=tool_context,
                reply_focus=group_reply_focus,
                quoted_message_id=quoted_message_id,
                model_type=delivery_model_type,
                model_name=delivery_model_name,
            )
            if self.cognition_shadow is not None:
                self.cognition_shadow.record_live_outcome(
                    turn_id,
                    reply_text=reply_text,
                    delivered_text="",
                    voice_mode=getattr(voice_request, "mode", "") if voice_request else "",
                )
            return
        self._mark_smart_group_reply(event, conversation_id, plain_text)
        if poke_requested and event.get("message_type") == "group":
            # 回复已成功发出，再执行模型自主决定的戳一戳；戳的是当前对话者。
            try:
                await self._send_group_poke(
                    websocket, event.get("group_id"), event.get("user_id")
                )
            except Exception as exc:
                LOGGER.warning(
                    "对话中主动戳人失败 user=%s group=%s: %s",
                    event.get("user_id"),
                    event.get("group_id"),
                    exc,
                )
        if strategy_limits is not None:
            # 只在真正把消息发出去之后才计数，失败/空回复不消耗配额。
            record_reply(
                scope="group" if event.get("message_type") == "group" else "private",
                user_id=str(event.get("user_id") or ""),
                group_id=str(event.get("group_id") or ""),
            )
        recorded_reply = delivered_text
        if self.cognition_shadow is not None:
            self.cognition_shadow.record_live_outcome(
                turn_id,
                reply_text=reply_text,
                delivered_text=recorded_reply,
                voice_mode=getattr(voice_request, "mode", "") if voice_request else "",
            )
        self.reply_engine.record_bot_reply(
            conversation_id,
            recorded_reply,
            sent_sticker,
            profile_id=profile_id,
        )
        self._log_reply_event(
            event,
            conversation_id=conversation_id,
            user_text=plain_text,
            model_reply=reply_text,
            delivered_reply=recorded_reply,
            voice_mode=getattr(voice_request, "mode", "") if voice_request else "",
            material_read_level=str(getattr(tool_context, "read_level", "") or ""),
            reply_focus_source=str(getattr(group_reply_focus, "source", "") or ""),
            reply_focus_text=str(getattr(group_reply_focus, "focus_text", "") or ""),
            quoted_message_id=quoted_message_id,
        )
        if self.insights is not None:
            # submit 只创建一个后台持久化任务；SQLite、向量模型和 DeepSeek
            # 都在独立 worker 中执行，不延长本轮 QQ 回复耗时。
            self.insights.submit_evaluation(
                EvaluationSample(
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    group_id=str(event.get("group_id") or ""),
                    question=model_input_text,
                    answer=reply_text,
                    context=context_from_tool_context(tool_context),
                    model_type=delivery_model_type,
                    model_name=delivery_model_name,
                )
            )

    async def _ensure_quoted_sender_id(
        self,
        websocket: Any,
        event: dict[str, Any],
    ) -> str | None:
        """在触发判断前确定被引用消息的发送者 QQ。

        优先使用 NapCat 顶层附带的 reply 对象；若缺失则用 reply 段里的消息
        ID 调 get_msg 拉取，并把 sender 回填到 event["reply"]，让后续判断函数
        读到确定值。任何失败都返回 None，绝不根据 ID 猜测。
        """
        existing = event.get("reply")
        if isinstance(existing, dict):
            sender = existing.get("sender")
            if isinstance(sender, dict) and sender.get("user_id") not in (None, ""):
                return str(sender.get("user_id"))

        message_id = extract_reply_message_id(event.get("message"))
        if not message_id:
            return None
        request_id = int(message_id) if str(message_id).isdigit() else message_id
        try:
            result = await self.call_action_and_wait(
                websocket,
                "get_msg",
                {"message_id": request_id},
                3.0,
            )
        except Exception:
            LOGGER.debug("get_msg 拉取被引用消息失败", exc_info=True)
            return None
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            return None
        sender = data.get("sender")
        if not isinstance(sender, dict) or sender.get("user_id") in (None, ""):
            return None
        # 回填 event["reply"]，让现有 _quoted_sender_is_bot 逻辑复用确定值。
        reply_obj = dict(existing) if isinstance(existing, dict) else {}
        reply_obj["sender"] = sender
        if data.get("message") is not None:
            reply_obj["message"] = data.get("message")
        event["reply"] = reply_obj
        return str(sender.get("user_id"))

    async def _maybe_ambient_group_reply(
        self,
        event: dict[str, Any],
        plain_text: str,
    ) -> bool:
        """普通群聊里由模型按内容判断是否主动搭话，带冷却；只对有真实文字的普通消息生效。"""
        if not getattr(self.config, "group_ambient_reply_enabled", True):
            return False
        if event.get("message_type") != "group":
            return False
        # 引用回复、戳一戳、@别人都有各自的处理路径，不走随机搭话。
        if _has_reply_segment(event) or event.get("_atri_poke_event"):
            return False
        _, at_other = _at_segment_targets(event, self.config.bot_qq)
        if at_other:
            return False
        # 纯图片/表情等占位消息不搭话，必须有真实文字。
        if not _strip_placeholder_text(str(plain_text or "")):
            return False
        now = time.time()
        group_id = str(event.get("group_id") or "")
        cooldown = max(
            0,
            int(getattr(self.config, "group_ambient_reply_cooldown_seconds", 300)),
        )
        if now - float(self._last_ambient_group_reply_at.get(group_id, 0.0)) < cooldown:
            return False
        # 不再随机掷骰子：交给模型结合聊天内容判断这句话值不值得接。
        # 传入近期群聊上下文，让模型能分辨这条消息是不是别人之间的对话。
        group_context = self._format_ambient_group_context(event)
        if not await self._llm_ambient_gate_should_reply(plain_text, group_context):
            return False
        self._last_ambient_group_reply_at[group_id] = now
        return True

    def _format_ambient_group_context(
        self,
        event: dict[str, Any],
        limit: int = 8,
    ) -> str:
        """格式化近期群聊记录（带说话人昵称），供接话判断理解对话对象。

        当前消息在后续流程才写入历史，因此这里取到的都是它之前的记录，
        无需排除当前消息。任何读取失败都返回空串，退化为只看单条文本。
        """
        group_id = str(event.get("group_id") or "")
        if not group_id:
            return ""
        try:
            entries = self.reply_engine.memory.recent_history(
                f"group:{group_id}", limit=limit
            )
        except Exception:
            LOGGER.debug("读取近期群聊上下文失败", exc_info=True)
            return ""
        lines: list[str] = []
        for entry in entries:
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            if entry.get("role") == "assistant":
                speaker = "亚托莉"
            else:
                speaker = str(entry.get("nickname") or "群友").strip() or "群友"
            lines.append(f"{speaker}: {text[:120]}")
        return "\n".join(lines)

    async def _llm_ambient_gate_should_reply(
        self, plain_text: str, group_context: str = ""
    ) -> bool:
        """调用模型判断当前这条群消息是否值得接话。

        走一次很轻的 chat 调用（只要求回答“接/不接”，max_tokens 极小）。
        若有近期群聊记录则一并提供，帮助模型判断这条消息是不是别人之间的
        对话。任何网络、模型或解析异常都保守返回 False（默认不接），只记日志，
        绝不让接话判断的故障影响主消息链路。
        """
        try:
            from ...prompting import load_prompt
            import httpx

            system_prompt = load_prompt("ambient_gate")
            new_message = str(plain_text or "")[:500]
            if group_context:
                user_content = (
                    "近期群聊记录：\n"
                    f"{group_context}\n\n"
                    "现在收到的新消息（没有艾特你）：\n"
                    f"{new_message}"
                )
            else:
                user_content = new_message
            payload = {
                "model": self.config.openai_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 10,
                "temperature": 0.2,
            }
            headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}
            url = f"{self.config.openai_base_url}/chat/completions"
            async with httpx.AsyncClient(
                timeout=float(getattr(self.config, "chat_request_timeout_seconds", 25.0)),
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
            content = str(
                data["choices"][0].get("message", {}).get("content") or ""
            ).strip()
            # 明确“不接”优先；否则只要结论里含“接”即视为接话。
            if "不接" in content:
                return False
            return "接" in content
        except Exception:
            LOGGER.exception("接话判断模型调用失败，默认不接话")
            return False

    def _evaluate_reply_strategy(
        self,
        event: dict[str, Any],
        *,
        profile_id: str,
        plain_text: str,
        is_owner: bool,
        is_continuation: bool,
    ) -> ReplyDecision:
        """读取好感度/群活跃度后交给回复策略判定。

        策略文件由 WebUI 写入；策略未启用、或记忆层读不到数据时一律放行，
        保证"策略坏了也不会让亚托莉突然不理人"。
        """

        policy = load_reply_strategy()
        if not policy.get("enabled"):
            return ReplyDecision(True, "策略未启用", {})
        scope = "group" if event.get("message_type") == "group" else "private"
        group_id = str(event.get("group_id") or "") if scope == "group" else ""
        user_id = str(event.get("user_id") or "")
        affection: float | None = None
        activity: float | None = None
        active_days: float | None = None
        message_count: int | None = None
        memory = getattr(self.reply_engine, "memory", None)
        now = time.time()
        try:
            if memory is not None:
                user_state = memory.proactive_state(profile_id, is_owner)
                affection = float(user_state.get("affection_score") or 0.0)
                message_count = int(user_state.get("message_count") or 0)
                last_user_at = float(user_state.get("last_user_at") or 0.0)
                if scope == "private" and last_user_at:
                    active_days = max(0.0, (now - last_user_at) / 86400.0)
                if scope == "group":
                    # 群门槛看群自己的活跃度/消息量/沉默天数，而不是某个成员的。
                    group_state = memory.proactive_state(f"group:{group_id}")
                    activity = float(group_state.get("group_activity_score") or 0.0)
                    message_count = int(group_state.get("message_count") or 0)
                    group_last_at = float(group_state.get("last_user_at") or 0.0)
                    if group_last_at:
                        active_days = max(0.0, (now - group_last_at) / 86400.0)
        except Exception as exc:  # 记忆读取失败只降级为"不作门槛判断"
            LOGGER.debug("回复策略读取记忆失败：%s", exc)
        return evaluate_reply(
            scope=scope,
            user_id=user_id,
            group_id=group_id,
            affection=affection,
            activity=activity,
            active_days=active_days,
            message_count=message_count,
            is_mention=is_bot_mentioned(event, self.config.bot_qq, plain_text),
            is_owner=is_owner,
            is_continuation=is_continuation,
            policy=policy,
            now=now,
        )

    def _reply_strategy_system_prompt(
        self, strategy_limits: dict[str, Any] | None
    ) -> str | None:
        """把对象策略里的长度要求转成一条系统提示。

        已回退：不再把「总长 X 字 / 最多 Y 条」作为**死命令**注入系统提示词——那会
        与人设的长回复风格冲突，导致内容偏移、截断、甚至把工具调用 JSON 当正文发出。
        长度与详略交给人格系统提示词 + 模型的"约 N 字"软引导，投递层再用拆分上限兜底。
        """

        return None


