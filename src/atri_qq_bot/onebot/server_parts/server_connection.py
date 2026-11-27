from __future__ import annotations

import asyncio
import base64
import logging
import time

from pathlib import Path

from .server_support import OneBotServerMixin, _sync_server_globals

# 显式导入而不是依赖 _sync_server_globals 的全局注入：账号路径是安全关键路径，
# 不应该依赖那个 __getattribute__ 钩子在运行时把名字塞进来。
# 这几个模块本身不 import onebot，所以不会形成循环依赖。
from ...accounts import (  # type: ignore[import-not-found]
    AccountContext,
    AccountSwitchCoordinator,
    bootstrap_from_config,
    build_runtime_resources,
    current_account,
    registry_for_config,
)

# Helldivers 情报桥接。helldivers 包只依赖 message_plan，不反向依赖 onebot，
# 因此在这里导入不会形成循环依赖。
from ...helldivers.bridge import HelldiversBridge
from ...helldivers.config import HelldiversConfig
from ...message_plan import outgoing_to_onebot_message, outgoing_to_segments
from ...runtime.paths import DATA_DIR

# 运行时会由 _sync_server_globals 注入 server.py 的同名 logger；这里显式声明一份，
# 保证模块在被单独导入（例如单元测试）时也能正常记录账号/连接类告警。
LOGGER = logging.getLogger("atri.onebot")

# 自然语言 HD2 结果的复用窗口：与 bridge 侧 300 秒数据缓存对齐。
# 窗口内“再看一眼”直接重发同一份结果，既不重新抓取，也不会发不出来。
_HD2_INTEL_REUSE_SECONDS = 300.0


def _record_hd2_last_intel(
    owner: Any, conversation_id: str, command: str, entity: str = ""
) -> None:
    """记下本会话最近一次 HD2 命令与实体，供引用元信息判断“这是不是刚才那张图”。

    用模块函数而不是实例方法：执行器在单元测试里会被挂在 ``SimpleNamespace``
    替身上，不能假定完整服务对象已经初始化。
    """
    store = getattr(owner, "_last_hd2_intel", None)
    if not isinstance(store, dict):
        store = {}
        owner._last_hd2_intel = store
    store[str(conversation_id)] = (str(command), str(entity or ""), time.time())


def _hd2_last_intel_entry(
    owner: Any, conversation_id: str
) -> tuple[str, str, float] | None:
    """读最近一次情报记录，兼容老的两元组 ``(command, at)``。"""
    store = getattr(owner, "_last_hd2_intel", None)
    if not isinstance(store, dict):
        return None
    raw = store.get(str(conversation_id))
    if not raw:
        return None
    try:
        if len(raw) == 3:
            return str(raw[0]), str(raw[1] or ""), float(raw[2] or 0.0)
        if len(raw) == 2:
            return str(raw[0]), "", float(raw[1] or 0.0)
    except (TypeError, ValueError):
        return None
    return None


# “用户正在引用机器人自己刚发的那张情报图”的时间窗：这段时间内不再补发同一张图。
_HD2_QUOTED_ECHO_SECONDS = 180.0


def _mark_quoted_own_intel(owner: Any, conversation_id: str) -> None:
    """记下本轮引用的是机器人自己的情报图（由引用元信息判定后调用）。

    执行器据此在复用命中时不重复补发同一张图——用户正对着那张图说话，
    再刷一遍就是刷屏。同样用模块函数，``SimpleNamespace`` 替身也安全。
    """
    store = getattr(owner, "_quoted_own_intel_at", None)
    if not isinstance(store, dict):
        store = {}
        owner._quoted_own_intel_at = store
    store[str(conversation_id)] = time.time()


def _clear_quoted_own_intel(owner: Any, conversation_id: str) -> None:
    """本轮引用与自家情报图无关时清掉标记，避免它残留到下一轮。"""
    store = getattr(owner, "_quoted_own_intel_at", None)
    if isinstance(store, dict):
        store.pop(str(conversation_id), None)


def _note_hd2_turn_text(owner: Any, conversation_id: str, text: str) -> None:
    """记下本轮用户原话：执行器据此区分“要新的”和“只是对着图吐槽”。"""
    store = getattr(owner, "_hd2_turn_text", None)
    if not isinstance(store, dict):
        store = {}
        owner._hd2_turn_text = store
    store[str(conversation_id)] = str(text or "")


def _hd2_turn_text_of(owner: Any, conversation_id: str) -> str:
    """取本轮用户原话；没有记录返回空串（按“没有明确诉求”处理）。"""
    store = getattr(owner, "_hd2_turn_text", None)
    if not isinstance(store, dict):
        return ""
    return str(store.get(str(conversation_id)) or "")


def _record_hd2_msg_ids(owner: Any, conversation_id: str, message_ids: Any) -> None:
    """记下这次情报图片实际发出的 message_id，供引用时精确比对。"""
    ids = [str(item).strip() for item in (message_ids or []) if str(item or "").strip()]
    if not ids:
        return
    store = getattr(owner, "_last_hd2_msg_ids", None)
    if not isinstance(store, dict):
        store = {}
        owner._last_hd2_msg_ids = store
    store[str(conversation_id)] = set(ids)


def _hd2_msg_ids_of(owner: Any, conversation_id: str) -> set[str]:
    """本会话最近一次情报图片的 message_id 集合；没有记录时返回空集。"""
    store = getattr(owner, "_last_hd2_msg_ids", None)
    if not isinstance(store, dict):
        return set()
    ids = store.get(str(conversation_id))
    if isinstance(ids, (set, frozenset)):
        return set(ids)
    if isinstance(ids, (list, tuple)):
        return {str(item) for item in ids}
    return set()


def _quoted_own_intel_active(owner: Any, conversation_id: str) -> bool:
    """本轮是否正引用着机器人刚发的情报图（窗口 180 秒）。"""
    store = getattr(owner, "_quoted_own_intel_at", None)
    if not isinstance(store, dict):
        return False
    seen = store.get(str(conversation_id))
    if not seen:
        return False
    return (time.time() - float(seen)) <= _HD2_QUOTED_ECHO_SECONDS


def _hd2_intel_still_usable(messages: Any) -> bool:
    """复用前确认缓存的 HD2 结果还能发出去：图片文件仍在、列表非空。

    文本卡片没有文件依赖，直接可用；图片被缓存/清理掉时返回 False，让执行器
    老老实实重新抓取一次，绝不把“文件不存在”的图片推给 OneBot。
    """
    if not messages:
        return False
    try:
        for message in messages:
            if str(getattr(message, "kind", "") or "") not in {"image", "record"}:
                continue
            content = str(getattr(message, "content", "") or "")
            if not content or not Path(content).exists():
                return False
    except Exception:  # noqa: BLE001 - 任何异常都按“不可复用”处理
        return False
    return True


async def _hd2_arsenal_fact_block(
    owner: Any, command: str, argument: str
) -> str:
    """hd2data 时返回本条目真实数据文本块；其他命令或失败时返回空串。

    用模块函数而不是实例方法：执行器在单元测试里会被挂在 ``SimpleNamespace``
    替身上，不能假定完整服务对象已经初始化。事实文本由 HelldiversService
    本地生成（敌人部位走缓存），失败静默降级，绝不影响情报图发送。
    """
    if command != "hd2data" or not str(argument or "").strip():
        return ""
    helldivers = getattr(owner, "helldivers", None)
    svc = getattr(helldivers, "service", None)
    if svc is None:
        return ""
    try:
        fact_text = await svc.get_arsenal_fact_text(argument)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[HD2] arsenal fact text failed: %s", exc)
        return ""
    if not fact_text:
        return ""
    return (
        "\n本条目真实数据如下（据此回答与追问，不要编造未列出的数值）。"
        "判断适合打哪个阵营/战线时，用穿甲等级对照敌人护甲等级、"
        "用伤害类型对照阵营特点（高穿甲对高护甲目标，范围/持续伤害对集群血肉目标），"
        "结论必须能被这些数据支撑；措辞可以带你的语气，但数值和结论不许编：\n"
        + fact_text
    )

# 账号切换时要整体替换的 server 属性。**每一项都必须换成新值或 None**，
# 绝不能保留上一个账号的对象——那会把 A 账号的数据写进 B 账号的库。
ACCOUNT_SCOPED_ATTRIBUTES = (
    "reply_engine",
    "insights",
    "cognition_shadow",
    "stickers",
    "tools",
    "voice",
)

# 账号切换前等待在途消息处理的超时。宁可超时继续（generation 会兜底），
# 也不能让一个卡住的任务把换账号永久挂死。
ACCOUNT_DRAIN_TIMEOUT_SECONDS = 5.0
ACCOUNT_DRAIN_POLL_SECONDS = 0.05


def _build_sticker_emotion_classifier(config: Any):
    """Return an async vision classifier closure, or None when disabled.

    The closure sends the captured image (base64) to the configured vision
    model with the sticker-emotion prompt and returns a single emotion label.
    It is built here so StickerManager stays decoupled from the vision client.
    """
    if not bool(getattr(config, "sticker_vision_classify_enabled", True)):
        return None

    async def _classify(image_bytes: bytes, mime_type: str, hint_text: str) -> str:
        import httpx

        from ...prompting import load_prompt

        system_prompt = load_prompt("sticker_emotion")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        mime = mime_type or "image/jpeg"
        payload = {
            "model": config.toolbox_vision_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        }
                    ],
                },
            ],
            "temperature": 0.1,
            "max_tokens": 12,
        }
        headers = {
            "Authorization": f"Bearer {config.toolbox_vision_api_key or 'ollama'}"
        }
        url = f"{str(config.toolbox_vision_base_url).rstrip('/')}/chat/completions"
        async with httpx.AsyncClient(
            timeout=float(getattr(config, "toolbox_vision_timeout_seconds", 30.0)),
            headers=headers,
        ) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
        label = str(
            ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
            or ""
        ).strip()
        # The prompt asks for one bare token; keep only the first word defensively.
        return label.split()[0] if label else ""

    return _classify


async def _hd2_internal_fact(
    owner: Any, command: str, argument: str
) -> str:
    """静默内部查证：只返回文本事实，不渲染情报图、不写待发队列。

    用模块函数而非实例方法：单元测试用 SimpleNamespace 作 self 替身。
    配装/对比时模型用它连续核验真实条目；查不到的条目给出明确回执，
    防止模型把不存在的装备写进推荐。
    """
    if command != "hd2data":
        return (
            "internal=true 仅支持 hd2data 的文本事实查证；"
            "该情报命令请用 internal=false 正常调用。"
        )
    helldivers = getattr(owner, "helldivers", None)
    svc = getattr(helldivers, "service", None)
    if svc is None:
        return "数据库当前不可用，不要凭记忆推荐装备。"
    try:
        fact_text = await svc.get_arsenal_fact_text(argument)
    except (TypeError, ValueError, OSError, AttributeError) as exc:
        LOGGER.warning("[HD2] internal fact text failed: %s", exc)
        return "该条目查证失败，请换一个条目再查，不要编造装备。"
    if not fact_text or fact_text[0] in ("❌", "⏳"):
        return (
            f"未查到与“{argument}”匹配的可用条目（{fact_text}）。"
            "不要把它写进推荐，换一个确实存在的条目再查。"
        )
    return (
        "静默查证文本事实如下，本轮不会给用户发任何图，可继续查下一个条目；"
        "只能基于这些真实条目推荐，名称用其中的官方名/玩家俗称：\n"
        + fact_text
    )


class OneBotConnectionMixin(OneBotServerMixin):
    def __init__(
        self,
        config: Any,
        reply_engine: AtriReplyEngine,
        insights_runtime: InsightsRuntime | None = None,
    ) -> None:
        _sync_server_globals(self)
        self.config = config
        self.reply_engine = reply_engine
        # 由生命周期入口注入旁路；单元测试和独立适配器构造不会隐式创建数据库。
        self.insights = insights_runtime
        if self.insights is not None:
            # 总结可选发送；人工生成时则按需向当前登录的 QQ 内核读取历史消息。
            self.insights.summaries.set_group_sender(self.send_group_summary)
            self.insights.summaries.set_group_history_fetcher(self.fetch_group_history)
        self.cognition_shadow = None
        if bool(getattr(config, "cognition_shadow_enabled", False)):
            try:
                if CognitionShadowRunner is None:
                    raise RuntimeError(f"认知依赖不可用：{_COGNITION_IMPORT_ERROR}")
                self.cognition_shadow = CognitionShadowRunner.from_config(config)
            except Exception as exc:
                # 扩展架构必须可降级：认知内核不可用时仍保留原有 QQ 回复能力。
                LOGGER.warning("认知影子内核未启用，继续使用稳定回复链路：%s", exc)
        self.stickers = StickerManager(
            config.sticker_dir,
            config.sticker_trigger_file,
            config.sticker_capture_dir,
            emotion_classifier=_build_sticker_emotion_classifier(config),
            review_enabled=bool(getattr(config, "sticker_review_enabled", True)),
        )
        self.tools = ToolAnalyzer(config)
        self.voice = VoiceManager(config)
        self.voice_calls = VoiceCallStore()
        self._active_websockets: set[Any] = set()
        self._pending_actions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._onebot_probe_failures = 0
        # 限频初值用 -inf 而不是 0.0：进程可能在系统开机后 10 分钟内启动，
        # 此时 time.monotonic() 仍小于限频窗口，0.0 会让首条关键告警被静默吞掉，
        # 恰好复现"登录号不一致导致消息全部丢弃却无日志"这类事故。
        self._last_napcat_manual_login_notice_at = float("-inf")
        # NapCat 实际登录号与 BOT_QQ 不一致时，事件会被整体忽略；这种"完全没反应"
        # 必须能在日志里看到原因，所以两条告警各自限频。
        self._account_mismatch_notice_at = float("-inf")
        self._foreign_account_event_notice_at = float("-inf")
        self._message_tasks: set[asyncio.Task[Any]] = set()
        self._conversation_queues: dict[str, asyncio.Queue[tuple[Any, dict[str, Any]]]] = {}
        self._conversation_workers: dict[str, asyncio.Task[Any]] = {}
        # 戳一戳不应因重复通知触发连续大模型请求；按会话做轻量冷却。
        self._last_poke_reply_at: dict[str, float] = {}
        self._last_smart_group_reply_at: dict[str, float] = {}
        # 群内重复内容去重：key=群会话ID，value=[(时间戳, 归一化文本)]，
        # 多个成员发送意思相同的消息时只回复一次。
        self._recent_group_reply_keys: dict[str, list[tuple[float, str]]] = {}
        # 视觉缓存：value=(时间戳, 分析结果, 发送者QQ, 发送者昵称)，保留归属
        # 以便追问“刚才那张图”时区分图片是不是当前用户发的。
        self._recent_visual_contexts: dict[
            str, tuple[float, Any, str, str]
        ] = {}
        # 唱歌使用独立的确定性会话控制器；群聊按“群 + 点歌用户”隔离状态。
        self.singing = SingingConversationController()
        self._group_reply_focus = GroupReplyFocusStore(bot_qq=config.bot_qq)
        self._pending_group_visual_events: dict[
            str,
            tuple[float, dict[str, Any]],
        ] = {}
        # 群维度的近期视觉素材：key=群号，value=按时间排列的
        # (时间戳, 图片段, 发送者QQ, 发送者昵称)。保留发送者归属，避免别人发的图
        # 在合并时被当前触发者“认领”。只在机器人被 @ 回复时才合并分析，平时不调用视觉模型。
        self._recent_group_visuals: dict[
            str, list[tuple[float, dict[str, Any], str, str]]
        ] = {}
        # 普通群聊偶尔搭话的上次时间记录：key=群号，用于冷却控制。
        # 只在真正发出后记账；判定/生成期间用 _ambient_group_not_before 占位防双发。
        self._last_ambient_group_reply_at: dict[str, float] = {}
        self._ambient_group_not_before: dict[str, float] = {}
        self._event_log = PROJECT_ROOT / "logs" / "onebot-events.log"
        self._reply_event_log = PROJECT_ROOT / "logs" / "reply-events.jsonl"
        self._voice_event_log = PROJECT_ROOT / "logs" / "voice-events.log"
        self.proactive_planner = ProactivePlanner(
            reply_engine.memory,
            getattr(config, "owner_qqs", ()),
        )
        # 账号系统（ACCOUNT_SYSTEM_ENABLED，默认关闭）。
        # 关闭时 coordinator 为 None，下面所有账号相关方法都是空操作，
        # 整条链路与账号化之前逐条一致。
        self._account_base_config = config
        self._account_config_lock = asyncio.Lock()
        self.account_coordinator = None
        if bool(getattr(config, "account_system_enabled", False)):
            self.account_coordinator = self._build_account_coordinator(config)

        # Helldivers 情报桥接（可选旁路）：这里只实例化、不发起任何网络请求；
        # 真正的组件装配在 run_server 中 await start()，且启动失败会被捕获降级，
        # 不会影响亚托莉本身的对话能力。
        self.helldivers = HelldiversBridge(
            HelldiversConfig.from_env(DATA_DIR / "helldivers")
        )
        # 注入主动推送能力：订阅的新闻/快报由 bridge 回调本方法，再经当前
        # NapCat 连接发出去。这样 helldivers 包无需知道 OneBot 的存在。
        self.helldivers.set_push_sender(self._hd2_push_sender)
        # 注入在线群列表查询：bridge 退群清理前先确认 QQ 在线且健康，再读取
        # 群列表；任何一步不确定都由 bridge 保留订阅，绝不误删。
        self.helldivers.set_group_list_provider(self._hd2_group_list_provider)
        # 自然语言 HD2 查询在工具循环里产出的待发情报（图片/文本），按会话暂存，
        # 由事件流程在回复发出后统一消费发送；每次 handle 前会先清空，避免串轮。
        self._pending_hd2_nl: dict[str, list[Any]] = {}
        # 最近一次成功产出的 HD2 结果：默认 5 分钟内同命令同实体直接复用，
        # 让「再看一眼」无需重新抓取，也不会因为刷新被拦下发不出来。
        self._hd2_intel_cache: dict[tuple[str, str, str], tuple[float, list[Any]]] = {}
        # 最近一次 HD2 命令（供引用元信息标注“这可能是刚才的战线图”）。
        self._last_hd2_intel: dict[str, tuple[str, str, float]] = {}
        # 本轮是否正引用着机器人自己刚发的 HD2 情报图：命中时复用不再补发同一张图。
        self._quoted_own_intel_at: dict[str, float] = {}
        # 本轮用户原话：用来区分“要新的/想再看”和“只是吐槽”。
        self._hd2_turn_text: dict[str, str] = {}
        # 最近一次情报图片发出后的 message_id：引用时用它精确确认是不是同一张。
        self._last_hd2_msg_ids: dict[str, set[str]] = {}

    async def _hd2_group_list_provider(self) -> list[str]:
        """确认 QQ 在线后返回当前加入的群号字符串列表；不确定即抛错。"""
        websocket = self._first_active_websocket()
        if websocket is None:
            raise RuntimeError("NapCat 当前未连接，无法确认在线群列表")
        # 先确认 QQ 内核在线且健康，连接存在不代表已登录。
        status = await self.call_action_and_wait(
            websocket, "get_status", {}, 5.0
        )
        if not _onebot_status_is_healthy(status):
            raise RuntimeError(
                _onebot_action_error_detail(
                    status, default="QQ 内核报告离线或异常，跳过订阅清理"
                )
            )
        result = await self.call_action_and_wait(
            websocket, "get_group_list", {}, 8.0
        )
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise RuntimeError("get_group_list 未返回列表，跳过订阅清理")
        group_ids: list[str] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("group_id"):
                continue
            group_ids.append(str(item.get("group_id")))
        return group_ids

    async def _hd2_nl_executor(
        self,
        conversation_id: str,
        command: str,
        argument: str,
        freshness: str = "auto",
        explicit: bool = True,
        internal: bool = False,
    ) -> str:
        """执行自然语言 HD2 工具：重建斜杠命令并复用 bridge 情报链路。

        成功时把待发消息暂存到 ``_pending_hd2_nl``，返回给模型的引导回执；
        任何失败都返回文本回执，由模型自然回应，绝不伪造情报数据。

        ``freshness=auto`` 时，本会话几分钟内刚产出过同命令同实体的结果就直接
        复用那份结果重发（不再抓取/渲染），保证「再看一眼」一定发得出来；
        ``latest`` 时跳过复用重新出图，战线类命令还会要求数据侧实时刷新。

        ``internal=True`` 时进入静默内部查证：只返回文本事实，不渲染情报图、
        不写待发队列，专供配装/对比时连续核验真实条目。
        """
        if internal:
            return await _hd2_internal_fact(self, command, argument)
        from ...llm_tools.hd2_intent import (
            build_hd2_slash,
            detect_hd2_redisplay_hint,
            detect_hd2_refresh_hint,
            hd2_entity_label,
            normalize_hd2_entity,
        )

        wants_latest = str(freshness or "auto").strip().lower() == "latest"
        turn_text = _hd2_turn_text_of(self, conversation_id)
        if (
            not wants_latest
            and not explicit
            and detect_hd2_refresh_hint(turn_text)
        ):
            # 模型没给 freshness（等于没表态）时才用用户原话兜底：宁可多抓一次，
            # 也不能拿几分钟前那张图糊弄“我要新的”。模型显式说了 auto 就听它的。
            wants_latest = True
        entity = normalize_hd2_entity(command, argument)
        # 回执里带上规范阵营名：模型看不到图片内容，得靠这句话才知道查的是哪条战线。
        label = hd2_entity_label(command, entity)
        label_note = f"（阵营：{label}）" if label else ""
        # hd2data 把本条目真实数据文本化进回执，模型才能基于数值而非图片作答。
        fact_block = await _hd2_arsenal_fact_block(self, command, argument)
        store = getattr(self, "_hd2_intel_cache", None)
        if not isinstance(store, dict):
            store = {}
            self._hd2_intel_cache = store
        reuse_seconds = _HD2_INTEL_REUSE_SECONDS
        # 实体先归一（终结族 / Terminids 视为同一条），否则同一个阵营换个写法
        # 就会当成新请求，重新抓一遍再补一张图。
        key = (conversation_id, str(command), entity)
        now = time.time()

        # 用户正引用着她刚发的这张图、又没说“要看/要新”：不管这条路是复用还是
        # 重抓，都不要再把同一张图发一遍——先判这件事，再决定要不要真去抓。
        if not wants_latest and not detect_hd2_redisplay_hint(turn_text):
            if _quoted_own_intel_active(self, conversation_id):
                last = _hd2_last_intel_entry(self, conversation_id)
                same_request = last is None or (
                    last[0] == str(command)
                    and (not last[1] or last[1] == entity)
                )
                if same_request:
                    return (
                        f"用户引用的就是你自己刚发出的这份情报图{label_note}，内容在他眼前。"
                        "本轮不要重复发图，用一句自然的话回应即可（可以评论数据，"
                        "需要时提醒他向上翻看）；只有他明确要求再看或刷新时，"
                        "才用 freshness=\"latest\" 再调用一次。"
                    )

        if not wants_latest:
            cached = store.get(key)
            if cached is not None:
                cached_at, cached_messages = cached
                age = now - float(cached_at)
                if age <= reuse_seconds and _hd2_intel_still_usable(cached_messages):
                    # 复用同一份结果：图还是那张图，但用户明确想看时照样发得出来。
                    self._pending_hd2_nl[conversation_id] = cached_messages
                    _record_hd2_last_intel(self, conversation_id, command, entity)
                    when = (
                        "刚刚"
                        if age < 60
                        else f"约 {int(age // 60)} 分钟前"
                    )
                    return (
                        f"已复用{when}发过的同一份情报{label_note}，会随本轮回复一起发出（未重新抓取）。"
                        "你只需用一句简短自然的话引导。"
                        "若用户明确要求刷新到最新数据，请用 freshness=\"latest\" 再调用一次。"
                        f"{fact_block}"
                    )

        slash_text = build_hd2_slash(command, argument)
        realtime = wants_latest and str(command) == "warfront"
        try:
            messages = await self.helldivers.handle(
                slash_text, conversation_id, realtime=realtime
            )
        except TypeError:
            # 兼容旧版 bridge（或不支持 realtime 关键字的替身）：退回两参调用。
            try:
                messages = await self.helldivers.handle(slash_text, conversation_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[HD2] 自然语言情报执行失败：%s", exc)
                return "Helldivers 情报获取失败，请自然地说明稍后再试，不要编造数据。"
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[HD2] 自然语言情报执行失败：%s", exc)
            return "Helldivers 情报获取失败，请自然地说明稍后再试，不要编造数据。"

        if not messages:
            return "该命令没有返回情报，请自然回应，不要编造游戏数据。"
        # 同一轮重复调用时覆盖旧结果，保证只发最后一次查询的情报。
        self._pending_hd2_nl[conversation_id] = messages
        store[key] = (now, list(messages))
        if len(store) > 64:
            oldest_key = min(store, key=lambda item: store[item][0])
            store.pop(oldest_key, None)
        _record_hd2_last_intel(self, conversation_id, command, entity)
        return (
            f"Helldivers 情报已就绪{label_note}，会随本轮回复一起发出。"
            "你只需用一句简短自然的话引导（可带情绪），"
            "不要罗列编号，也不要说自己做不到或正在查询。"
            f"{fact_block}"
        )

    async def _hd2_push_sender(
        self, session_key: str, messages: list[Any]
    ) -> int:
        """把 Helldivers 的消息主动推送到一个群或私聊，成功返回 1。"""
        kind, target = HelldiversBridge.parse_session_key(session_key)
        websocket = self._first_active_websocket()
        if websocket is None:
            raise RuntimeError("NapCat 当前未连接，无法主动推送")
        if kind not in {"group", "private"} or not target:
            raise RuntimeError(f"无效的会话标识：{session_key}")

        # 合并开关开启、且这组是含图片的多条消息时，flatten 成一条整体发送，
        # 避免 B站快报/新闻推送的“文字+图+链接”被拆成多个气泡。
        merge_enabled = getattr(self.config, "hd2_merge_messages", False)
        can_merge = (
            merge_enabled
            and len(messages) > 1
            and any(getattr(item, "kind", "") == "image" for item in messages)
        )
        if can_merge:
            merged_segments: list[dict[str, Any]] = []
            for item in messages:
                merged_segments.extend(outgoing_to_segments(item))
            if merged_segments:
                if kind == "group":
                    action = "send_group_msg"
                    params = {"group_id": target, "message": merged_segments}
                else:
                    action = "send_private_msg"
                    params = {"user_id": target, "message": merged_segments}
                result = await self.call_action_and_wait(
                    websocket, action, params, 15.0
                )
                # 发送失败会抛 RuntimeError，由 bridge 侧兜底并安排重试。
                self._require_message_delivery(result)
            return 1

        for message in messages:
            onebot_message = outgoing_to_onebot_message(message)
            if kind == "group":
                params = {
                    "group_id": target,
                    "message": onebot_message,
                }
                action = "send_group_msg"
            else:
                params = {
                    "user_id": target,
                    "message": onebot_message,
                }
                action = "send_private_msg"
            result = await self.call_action_and_wait(
                websocket, action, params, 15.0
            )
            # 发送失败会抛 RuntimeError，由 bridge 侧兜底并安排重试。
            self._require_message_delivery(result)
        return 1

    def _build_account_coordinator(
        self,
        config: Any,
        *,
        builders: dict[str, Any] | None = None,
        required: Any = None,
    ) -> Any:
        """按配置建立账号协调器；BOT_QQ 在这里被引导成默认账号。

        排空用的是 ``self.drain_account_work``，所以协调器必须在 server 实例上构造。

        ``builders`` / ``required`` 只是给测试注入轻量资源的接缝——生产路径不传，
        走 ``build_runtime_resources`` 的默认表。
        """
        account = bootstrap_from_config(config)
        if account is not None:
            LOGGER.info(
                "账号系统已启用：默认账号 %s（确认状态 %s）",
                account.account_id,
                "已确认" if account.confirmed else "待确认",
            )
        else:
            LOGGER.warning("账号系统已启用，但 BOT_QQ 未设置，无法引导默认账号")
        return AccountSwitchCoordinator(
            config,
            build=lambda target, previous: build_runtime_resources(
                self._account_base_config,
                target,
                builders=builders,
                required=(
                    required
                    if required is not None
                    else frozenset({"reply_engine"})
                ),
            ),
            async_drain=self.drain_account_work,
        )

    async def drain_account_work(self) -> None:
        """账号切换前排空在途的消息处理。

        不能复用 ``shutdown()``——那个是 ``cancel``，会把正在处理的消息直接丢掉。
        这里等它们跑完，并设上限避免被一个卡住的任务永久挂死。
        """
        deadline = time.monotonic() + ACCOUNT_DRAIN_TIMEOUT_SECONDS
        pending: set[Any] = set()
        while True:
            pending = {
                task
                for task in (
                    set(self._message_tasks)
                    | set(self._conversation_workers.values())
                )
                if not task.done()
            }
            if not pending:
                return
            if time.monotonic() >= deadline:
                # 超时不阻断切换：每条任务都持有自己那份上下文快照，
                # 写的是它自己账号的库；generation 兜底会处理延迟解析路径的调用点。
                LOGGER.warning(
                    "账号切换排空超时：仍有 %s 个任务在跑，继续切换", len(pending)
                )
                return
            await asyncio.sleep(ACCOUNT_DRAIN_POLL_SECONDS)

    def apply_account_context(self, context: Any) -> None:
        """把账号上下文里的资源换到 server 上。调用方必须已完成排空。

        **每一项账号作用域资源都换成新值或 None**，绝不保留上一个账号的对象——
        否则新账号的记忆会写进旧账号的库，而且不会报错。
        """
        self.config = context.config
        for name in ACCOUNT_SCOPED_ATTRIBUTES:
            setattr(self, name, context.resources.get(name))

        # 下面这几个跟着 config / reply_engine 走，必须一起重建。
        # 重建失败时**置空而不是保留旧对象**：旧对象绑定的是上一个账号的记忆，
        # 留着它比没有它更危险（会把新账号的数据写进旧账号的库）。
        try:
            self._group_reply_focus = GroupReplyFocusStore(bot_qq=self.config.bot_qq)
        except Exception as exc:
            LOGGER.error("重建群聊焦点存储失败，已置空：%s", exc)
            self._group_reply_focus = None
        if self.reply_engine is not None:
            try:
                self.proactive_planner = ProactivePlanner(
                    self.reply_engine.memory,
                    getattr(self.config, "owner_qqs", ()),
                )
            except Exception as exc:
                LOGGER.error("重建主动消息规划器失败，已置空：%s", exc)
                self.proactive_planner = None
        else:
            self.proactive_planner = None
        if self.insights is not None:
            # 新账号的 insights 是全新实例，发送器与历史读取器要重新挂上。
            self.insights.summaries.set_group_sender(self.send_group_summary)
            self.insights.summaries.set_group_history_fetcher(self.fetch_group_history)

    async def follow_napcat_account(self, peer_uin: int | None) -> None:
        """按 NapCat 实际登录号切换账号。

        仅在账号系统开启时生效。三条边界：

        * ``ACCOUNT_FOLLOW_NAPCAT`` 关闭时，当前账号就是注册表里的默认账号
          （BOT_QQ），所以 NapCat 登录了别的号也只会继续用默认账号——
          这正是今天的"账号不一致就忽略消息"行为。
        * 跟随开启但该号**未在 WebUI 确认过**时 ``current_account`` 返回 None，
          于是不切换，行为同样回落到今天。
        * 目标与当前相同则什么都不做。
        """
        coordinator = self.account_coordinator
        if coordinator is None:
            return

        async with self._account_config_lock:
            await self._follow_napcat_account(coordinator, peer_uin)

    async def _follow_napcat_account(
        self, coordinator: AccountSwitchCoordinator, peer_uin: int | None
    ) -> None:
        base = self._account_base_config
        if peer_uin is not None and bool(getattr(base, "account_follow_napcat", False)):
            try:
                _account, discovered = registry_for_config(base).observe_from_napcat(
                    peer_uin
                )
            except Exception as exc:
                LOGGER.warning("记录 NapCat 登录账号失败：%s", exc)
            else:
                if discovered:
                    LOGGER.warning(
                        "发现新账号 QQ %s（未确认）：确认前不建数据区，"
                        "行为与今天的账号不一致一致",
                        peer_uin,
                    )

        target = current_account(base, napcat_uin=peer_uin)
        if target is None:
            return
        if coordinator.context.account_id == target.account_id:
            return

        try:
            context = await coordinator.aswitch(
                target, reason=f"NapCat 登录 {peer_uin}"
            )
        except Exception as exc:
            # 失败原子性保证旧上下文仍然有效，所以这里只记日志、继续用旧账号。
            LOGGER.error("账号切换失败，继续使用当前账号：%s", exc)
            return
        self.apply_account_context(context)

    async def handle_connection(self, websocket: Any, path: str | None = None) -> None:
        # NapCat 是反向 WebSocket 客户端：连接建立后持续接收事件，并主动探测
        # get_status。连接存在不等于 QQ 已登录，健康状态还要参考 online/good 回执。
        LOGGER.info("NapCat 已连接：remote=%s", getattr(websocket, "remote_address", "unknown"))
        self._active_websockets.add(websocket)
        publish_napcat_runtime_state("probing", detail="NapCat 已连接，正在确认 QQ 登录状态")
        health_task = asyncio.create_task(self.onebot_health_loop(websocket))
        try:
            async for raw_message in websocket:
                await self.handle_payload(websocket, raw_message)
        except Exception as exc:
            LOGGER.warning("NapCat 连接关闭或异常：%s", exc)
        finally:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await health_task
            self._active_websockets.discard(websocket)
            if not self._active_websockets:
                publish_napcat_runtime_state(
                    "disconnected",
                    detail="NapCat 连接已断开，正在等待重连",
                )
            LOGGER.warning("NapCat 已断开，服务继续监听并等待其自动重连")

    async def shutdown(self) -> None:
        """停止消息调度并释放等待中的 OneBot action。"""
        # 先清空待处理事件，避免取消 worker 时其 finally 分支重新拉起 worker。
        for queue in tuple(self._conversation_queues.values()):
            get_nowait = getattr(queue, "get_nowait", None)
            if not callable(get_nowait):
                continue
            while True:
                try:
                    get_nowait()
                except asyncio.QueueEmpty:
                    break

        tasks = {
            task
            for task in (
                set(self._message_tasks)
                | set(self._conversation_workers.values())
            )
            if not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for future in tuple(self._pending_actions.values()):
            if not future.done():
                future.cancel()
        self._pending_actions.clear()
        self._message_tasks.clear()
        self._conversation_workers.clear()
        self._conversation_queues.clear()
        LOGGER.info("OneBot 消息调度已清理：tasks=%s", len(tasks))

    async def probe_onebot_status(self, websocket: Any) -> bool:
        """主动询问 QQ 内核状态；NapCat 心跳本身不能证明 QQ 仍在线。"""
        try:
            result = await self.call_action_and_wait(
                websocket,
                "get_status",
                {},
                5.0,
            )
        except Exception as exc:
            publish_onebot_probe_result(False, detail=f"QQ 状态探测失败：{exc}")
            LOGGER.warning("QQ 状态探测失败：%s", exc)
            return False
        healthy = _onebot_status_is_healthy(result)
        data = result.get("data") if isinstance(result, dict) else None
        peer_uin = await self._napcat_login_uin(websocket)
        if healthy:
            self_id = self.config.bot_qq if isinstance(data, dict) else None
            detail = "QQ 登录与消息服务正常"
            if peer_uin is not None and int(peer_uin) != int(self.config.bot_qq or 0):
                # 账号不一致时机器人会忽略所有消息（事件 self_id 不匹配），
                # 用户看到的现象就是"发消息完全没反应"。
                detail = (
                    f"NapCat 登录 {peer_uin}，项目 BOT_QQ={self.config.bot_qq}；"
                    "账号不一致，机器人会忽略所有消息"
                )
                self._warn_account_mismatch(peer_uin)
            publish_onebot_probe_result(
                True,
                detail=detail,
                self_id=self_id,
                peer_uin=peer_uin,
            )
            # 账号系统开启时按 NapCat 实际登录号切换；未开启时是空操作。
            await self.follow_napcat_account(peer_uin)
            return True
        detail = _onebot_action_error_detail(result, default="QQ 内核报告离线或异常")
        publish_onebot_probe_result(
            False,
            detail=detail,
            self_id=self.config.bot_qq if isinstance(data, dict) else None,
            peer_uin=peer_uin,
        )
        LOGGER.warning("QQ 状态异常：%s", detail)
        return False

    async def _napcat_login_uin(self, websocket: Any) -> int | None:
        """读取 NapCat 实际登录的 QQ 号；失败返回 None（不影响健康判断）。"""

        try:
            result = await self.call_action_and_wait(websocket, "get_login_info", {}, 5.0)
        except Exception:
            return None
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            return None
        try:
            value = int(data.get("user_id") or 0)
        except (TypeError, ValueError):
            return None
        return value or None

    def _warn_account_mismatch(self, peer_uin: int) -> None:
        now = time.monotonic()
        if now - self._account_mismatch_notice_at < 600:
            return
        self._account_mismatch_notice_at = now
        LOGGER.warning(
            "账号不一致：NapCat 登录的是 %s，但项目 BOT_QQ=%s。"
            "这种情况下所有 QQ 消息都会被忽略（既没有回复，也不会有消息决策日志）。"
            "请二选一：把 .env 的 BOT_QQ 改成 %s，或在 NapCat 里换回 %s 后重启项目。",
            peer_uin,
            self.config.bot_qq,
            peer_uin,
            self.config.bot_qq,
        )

    def _warn_foreign_account_event(self, event_self_id: int | None) -> None:
        now = time.monotonic()
        if now - self._foreign_account_event_notice_at < 600:
            return
        self._foreign_account_event_notice_at = now
        LOGGER.warning(
            "忽略其它账号的事件：事件 self_id=%s，配置 BOT_QQ=%s（账号不一致时不会回复）",
            event_self_id,
            self.config.bot_qq,
        )

    async def onebot_health_loop(self, websocket: Any) -> None:
        await asyncio.sleep(1.0)
        while websocket in self._active_websockets:
            healthy = await self.probe_onebot_status(websocket)
            if healthy:
                self._onebot_probe_failures = 0
            else:
                self._onebot_probe_failures += 1
                await self._mark_napcat_manual_login_required()
            await asyncio.sleep(ONEBOT_HEALTH_INTERVAL_SECONDS)

    async def _mark_napcat_manual_login_required(self) -> None:
        if self._onebot_probe_failures < ONEBOT_HEALTH_FAILURES_BEFORE_MANUAL_LOGIN_NOTICE:
            return
        now = time.monotonic()
        if (
            now - self._last_napcat_manual_login_notice_at
            < NAPCAT_MANUAL_LOGIN_NOTICE_COOLDOWN_SECONDS
        ):
            return
        self._last_napcat_manual_login_notice_at = now
        detail = "NapCat/QQ 状态连续异常，请打开 NapCat WebUI 手动扫码登录"
        publish_napcat_runtime_state("login_required", detail=detail)
        LOGGER.warning("%s；项目不自动重启或弹出二维码", detail)

    async def handle_payload(self, websocket: Any, raw_message: str) -> None:
        try:
            event = json.loads(raw_message)
        except json.JSONDecodeError:
            LOGGER.warning("忽略非 JSON OneBot 负载：bytes=%s", len(raw_message))
            return
        if not isinstance(event, dict):
            LOGGER.warning(
                "忽略非对象 OneBot 负载：type=%s",
                type(event).__name__,
            )
            return

        # OneBot 心跳是验证 QQ/NapCat 事件流仍活着的唯一低成本信号；仅有
        # WebSocket TCP 连接并不足以说明机器人还能接收 QQ 消息。
        publish_onebot_event_activity(is_message=event.get("post_type") == "message")

        if "echo" in event and "retcode" in event:
            self._resolve_pending_action(event)
            return

        if event.get("post_type") in ("message", "notice"):
            event_self_id = _as_int(event.get("self_id"))
            if event_self_id is not None and event_self_id != self.config.bot_qq:
                self._warn_foreign_account_event(event_self_id)
                return

        # OneBot 的戳一戳是 notice 事件。只把“目标是亚托莉”的事件归一化，
        # 戳其他人的通知直接忽略，避免误触发正常对话链路。
        if is_poke_event(event):
            event = normalize_poke_event(event, self.config.bot_qq)
            if event is None:
                return

        # 新人入群同样是 notice。归一化成普通群消息后走同一条回复链路，
        # 由它来触发入群欢迎；亚托莉自己进群返回 None，直接忽略。
        if is_group_increase_event(event):
            event = normalize_group_increase_event(event, self.config.bot_qq)
            if event is None:
                return

        if event.get("post_type") == "message":
            if getattr(self, "insights", None) is not None and event.get("message_type") == "group":
                # 在 1.2 秒防抖合并前归档，避免连续群消息被合并后丢失统计粒度。
                self.insights.archive_group_message(
                    event,
                    extract_plain_text(event.get("message")),
                )
            # 不记录原始聊天文本，日志只保留类型和会话标识以保护隐私。
            LOGGER.debug(
                "收到消息事件：type=%s conversation=%s user=%s",
                event.get("message_type"),
                _conversation_id(event),
                event.get("user_id"),
            )
            self._enqueue_message_event(websocket, event)
            return

        task = asyncio.create_task(self._handle_event(websocket, event))
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)
        task.add_done_callback(self._log_task_exception)

    def _enqueue_message_event(self, websocket: Any, event: dict[str, Any]) -> None:
        queue_id = _message_queue_id(event)
        queue = self._conversation_queues.get(queue_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=CONVERSATION_QUEUE_MAXSIZE)
            self._conversation_queues[queue_id] = queue

        if queue.full():
            try:
                _, dropped_event = queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                LOGGER.warning(
                    "会话队列已满，丢弃较旧消息：conversation=%s dropped_message=%s queue_limit=%s",
                    queue_id,
                    dropped_event.get("message_id"),
                    CONVERSATION_QUEUE_MAXSIZE,
                )
        queue.put_nowait((websocket, event))

        worker = self._conversation_workers.get(queue_id)
        if worker is not None and not worker.done():
            return

        self._start_conversation_worker(queue_id)

    def _start_conversation_worker(self, queue_id: str) -> None:
        task = asyncio.create_task(self._conversation_worker(queue_id))
        self._conversation_workers[queue_id] = task
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)
        task.add_done_callback(self._log_task_exception)
        task.add_done_callback(
            lambda done, key=queue_id: (
                self._conversation_workers.pop(key, None)
                if self._conversation_workers.get(key) is done
                else None
            )
        )

    async def _conversation_worker(self, queue_id: str) -> None:
        """在 1.2 秒窗口内合并同一会话的连续输入，再串行处理。"""
        queue = self._conversation_queues[queue_id]
        try:
            while True:
                try:
                    websocket, event = await asyncio.wait_for(
                        queue.get(),
                        timeout=QUEUE_IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    if queue.empty():
                        break
                    continue

                batch = [(websocket, event)]
                await asyncio.sleep(MESSAGE_DEBOUNCE_SECONDS)
                while True:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                merged_websocket, merged_event = _merge_message_batch(batch)
                await self._handle_event(merged_websocket, merged_event)
        finally:
            if queue.empty():
                self._conversation_queues.pop(queue_id, None)
            else:
                self._conversation_workers.pop(queue_id, None)
                self._start_conversation_worker(queue_id)

