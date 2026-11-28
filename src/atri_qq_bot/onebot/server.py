from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

try:
    from ..cognition import CognitionShadowRunner, evidence_from_tool_context
except Exception as cognition_import_error:  # pragma: no cover - 仅用于缺依赖部署兜底
    CognitionShadowRunner = None  # type: ignore[assignment,misc]

    def evidence_from_tool_context(tool_context: Any | None) -> tuple[Any, ...]:
        return ()

    _COGNITION_IMPORT_ERROR = cognition_import_error
else:
    _COGNITION_IMPORT_ERROR = None
from atri_webui import bind_loop, start_webui, stop_webui

from ..insights import InsightsRuntime
from ..persona import AtriReplyEngine
from ..runtime.control import (
    publish_napcat_runtime_state,
    publish_onebot_event_activity,
    publish_onebot_probe_result,
)
from ..runtime.modality_access import is_event_modality_allowed
from ..voice import evaluate_voice_request as evaluate_voice_request

SMART_GROUP_REPLY_COOLDOWN_SECONDS = 100.0
MESSAGE_DEBOUNCE_SECONDS = 1.2
QUEUE_IDLE_TIMEOUT_SECONDS = 20.0
CONVERSATION_QUEUE_MAXSIZE = 64
VISUAL_CONTEXT_TTL_SECONDS = 15 * 60
PENDING_GROUP_VISUAL_TTL_SECONDS = 60
# 回戳动作：短冷却，正常连续戳都能回戳，只防秒级狂戳刷屏。
POKE_BACK_COOLDOWN_SECONDS = 5.0
# 文字回复：长冷却，避免连续戳产生大段文字刷屏。
POKE_TEXT_COOLDOWN_SECONDS = 60.0
ONEBOT_HEALTH_INTERVAL_SECONDS = 30.0
ONEBOT_HEALTH_FAILURES_BEFORE_MANUAL_LOGIN_NOTICE = 2
NAPCAT_MANUAL_LOGIN_NOTICE_COOLDOWN_SECONDS = 10 * 60
LOGGER = logging.getLogger("atri.onebot")


from .server_parts.helpers import (
    _compose_model_input as _compose_model_input,
    _conversation_id as _conversation_id,
    _event_has_visual_material as _event_has_visual_material,
    _first_reply_delay as _first_reply_delay,
    _is_visual_followup as _is_visual_followup,
    _merge_pending_visual_event as _merge_pending_visual_event,
    _message_queue_id as _message_queue_id,
    _nickname as _nickname,
    _now_text as _now_text,
    _onebot_status_is_healthy as _onebot_status_is_healthy,
    _pending_visual_key as _pending_visual_key,
    _profile_id as _profile_id,
    _send_delay as _send_delay,
    _should_reuse_cached_visual as _should_reuse_cached_visual,
    _typing_delay as _typing_delay,
)
from .server_parts.server_connection import OneBotConnectionMixin
from .server_parts.server_delivery import (
    OneBotDeliveryMixin,
    cached_origin_name,
)
from .server_parts.server_events import OneBotEventMixin
from .server_parts.server_proactive import OneBotProactiveMixin


class OneBotServer(
    OneBotConnectionMixin,
    OneBotEventMixin,
    OneBotDeliveryMixin,
    OneBotProactiveMixin,
):
    """OneBot v11 的接入适配器。

    消息流转为：NapCat 反向 WebSocket -> ``handle_payload`` -> 按会话队列
    防抖合并 -> ``_handle_event``（回复策略、ASR、多模态）-> 人格引擎 ->
    ``send_reply`` -> OneBot action。每个会话独占队列，避免慢模型请求导致
    同一用户的消息乱序。
    """


async def run_server(config: Any) -> None:
    import websockets

    bind_loop(asyncio.get_running_loop())
    reply_engine = AtriReplyEngine(config)
    insights_runtime: InsightsRuntime | None = None
    try:
        insights_runtime = InsightsRuntime()
    except Exception as exc:
        # 评测/总结是纯旁路：初始化失败时只关闭扩展，不影响 OneBot 回复能力。
        LOGGER.warning("评测与群聊总结旁路未启用，继续运行核心回复链路：%s", exc)
    server = OneBotServer(config, reply_engine, insights_runtime)
    # 注入自然语言 HD2 查询执行器：引擎工具循环命中 query_helldivers 时回调，
    # 复用 helldivers 桥接；保持引擎与情报组件解耦。
    reply_engine.set_hd2_tool_executor(server._hd2_nl_executor)
    if server.insights is not None:
        await server.insights.start()

    # 启动 Helldivers 情报模块。它是可选旁路：任何启动异常都只记录并降级，
    # 保证亚托莉的核心对话始终可用。
    if server.helldivers.config.enabled:
        try:
            await server.helldivers.start()
        except Exception as exc:
            LOGGER.warning("Helldivers 情报模块启动失败，已降级为仅对话：%s", exc)

    async with websockets.serve(server.handle_connection, config.host, config.port):
        print(
            f"[onebot] Listening on ws://{config.host}:{config.port}/onebot "
            f"for QQ {config.bot_qq}; reply_mode={config.reply_mode}"
        )
        idle_task = asyncio.create_task(server.idle_nudge_loop())
        morning_task = asyncio.create_task(server.morning_greeting_loop())
        proactive_v2_task = asyncio.create_task(server.proactive_v2_loop())
        group_task = asyncio.create_task(server.group_proactive_loop())
        webui_server = await start_webui(config, server)
        try:
            await asyncio.Future()
        finally:
            await stop_webui(webui_server)
            idle_task.cancel()
            morning_task.cancel()
            proactive_v2_task.cancel()
            group_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle_task
            with contextlib.suppress(asyncio.CancelledError):
                await morning_task
            with contextlib.suppress(asyncio.CancelledError):
                await proactive_v2_task
            with contextlib.suppress(asyncio.CancelledError):
                await group_task
            await server.helldivers.stop()
            await server.shutdown()
            if server.insights is not None:
                await server.insights.close()
            if server.cognition_shadow is not None:
                await server.cognition_shadow.close()
