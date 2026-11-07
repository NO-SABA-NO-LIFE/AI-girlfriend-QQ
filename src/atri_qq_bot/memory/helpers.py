from __future__ import annotations

from . import *  # noqa: F403 — compatibility globals are populated by the package.
from . import _memory_core

def _group_member_identity(conversation_id: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"group:([^:]+):user:([^:]+)", str(conversation_id or ""))
    if not match:
        return None
    return match.group(1), match.group(2)


def _merge_group_safe_structured_memory(
    member_item: dict[str, Any],
    canonical_item: dict[str, Any],
    group_scope: str,
) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {"l1": [], "l2": [], "l3": [], "candidates": []}
    member_memory = _ensure_structured_memory(member_item)
    canonical_memory = _ensure_structured_memory(canonical_item)
    for bucket in ("l1", "l2", "candidates"):
        seen: set[str] = set()
        for entry in canonical_memory.get(bucket, []):
            if not isinstance(entry, dict) or not _entry_visible_in_group(entry, group_scope):
                continue
            key = str(entry.get("memory_key") or entry.get("id") or id(entry))
            if key not in seen:
                merged[bucket].append(dict(entry))
                seen.add(key)
        for entry in member_memory.get(bucket, []):
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("memory_key") or entry.get("id") or id(entry))
            if key not in seen:
                merged[bucket].append(dict(entry))
                seen.add(key)
    # 群成员会话的 L3 只给当前成员详情使用，不能把别人的原始对话混进来。
    # 群总览的 L3 仍由 group:<id> 自己维护。
    merged["l3"] = [
        dict(entry)
        for entry in list(member_memory.get("l3") or [])[-8:]
        if isinstance(entry, dict)
    ]
    return merged


def _entry_visible_in_group(entry: dict[str, Any], group_scope: str) -> bool:
    visibility = entry.get("visibility")
    if visibility == "public" or visibility == group_scope:
        return True
    if isinstance(visibility, list) and (
        "public" in visibility or group_scope in visibility
    ):
        return True
    return False


def _proactive_override(value: Any) -> str:
    normalized = str(value or "auto").strip().lower()
    return normalized if normalized in {"auto", "allow", "deny"} else "auto"


def _trust_tier(value: Any) -> str:
    normalized = str(value or "probation").strip().lower()
    return normalized if normalized in {"probation", "approved", "trusted", "blocked"} else "probation"


_PERSONAL_EVENT_HINTS = (
    "生日", "考试", "面试", "答辩", "开会", "会议", "截止", "ddl", "DDL",
    "报名", "航班", "飞机", "火车", "旅行", "纪念日",
)


def _remember_personal_event(item: dict[str, Any], text: str, now: float) -> None:
    source = str(text or "").strip()
    if not source or not any(hint in source for hint in _PERSONAL_EVENT_HINTS):
        return
    local_now = datetime.fromtimestamp(now, safe_zoneinfo("Asia/Shanghai"))
    event_date = None
    if "大后天" in source:
        event_date = (local_now + timedelta(days=3)).date()
    elif "后天" in source:
        event_date = (local_now + timedelta(days=2)).date()
    elif "明天" in source:
        event_date = (local_now + timedelta(days=1)).date()
    elif "今天" in source or "今晚" in source:
        event_date = local_now.date()
    else:
        match = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]?", source)
        if match:
            year = int(match.group(1) or local_now.year)
            try:
                event_date = datetime(year, int(match.group(2)), int(match.group(3))).date()
            except ValueError:
                return
            if not match.group(1) and event_date < local_now.date():
                event_date = event_date.replace(year=event_date.year + 1)
    if event_date is None:
        return

    hour, minute = 9, 0
    time_match = re.search(r"(\d{1,2})[点:：](?:(\d{1,2})分?)?", source)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        if "下午" in source and hour < 12:
            hour += 12
        if "晚上" in source and hour < 12:
            hour += 12
        if hour > 23 or minute > 59:
            return
    due = datetime.combine(event_date, datetime.min.time(), tzinfo=local_now.tzinfo).replace(
        hour=hour,
        minute=minute,
    )
    event_id = f"{event_date.isoformat()}:{hour:02d}{minute:02d}:{next(h for h in _PERSONAL_EVENT_HINTS if h in source).lower()}"
    events = [event for event in (item.get("personal_events") or []) if isinstance(event, dict)]
    if any(str(event.get("id") or "") == event_id for event in events):
        return
    events.append({
        "id": event_id,
        "title": source[:120],
        "kind": "birthday" if "生日" in source else "important_event",
        "due_at": due.timestamp(),
        "created_at": now,
        "status": "scheduled",
        "visibility": "private",
    })
    item["personal_events"] = events[-20:]


_running_average = _memory_core._running_average
_as_float = _memory_core._as_float
_clamp = _memory_core._clamp
_is_group_conversation = _memory_core._is_group_conversation
_initialize_affection = _memory_core._initialize_affection
_contains_any = _memory_core._contains_any
_classify_affection_event = _memory_core._classify_affection_event
_update_affection = _memory_core._update_affection
_decay_private_affection_for_idle = _memory_core._decay_private_affection_for_idle
_private_nudge_multiplier = _memory_core._private_nudge_multiplier
_decay_group_activity = _memory_core._decay_group_activity
_is_unrelated_negative_group_message = _memory_core._is_unrelated_negative_group_message
_update_group_activity = _memory_core._update_group_activity
_emoji_count = _memory_core._emoji_count
_style_flags = _memory_core._style_flags
_message_features = _memory_core._message_features
_merge_feature_counts = _memory_core._merge_feature_counts
_append_iteration_rule = _memory_core._append_iteration_rule
_iteration_rule_text = _memory_core._iteration_rule_text
_append_history = _memory_core._append_history
_shorten = _memory_core._shorten
_extract_topics = _memory_core._extract_topics
_merge_topics = _memory_core._merge_topics
_safe_topics = _memory_core._safe_topics
_is_safe_topic = _memory_core._is_safe_topic
is_memory_pollution_text = _memory_core.is_memory_pollution_text
_ensure_structured_memory = _memory_core._ensure_structured_memory
_append_session_l3 = _memory_core._append_session_l3
_remember_structured_from_user = _memory_core._remember_structured_from_user
_actionable_style_candidate = _memory_core._actionable_style_candidate
_style_rule_value = _memory_core._style_rule_value
_extract_l1_candidates = _memory_core._extract_l1_candidates
_extract_l2_events = _memory_core._extract_l2_events
_candidate = _memory_core._candidate
_upsert_l1_candidate = _memory_core._upsert_l1_candidate
_upsert_l2 = _memory_core._upsert_l2
_find_memory = _memory_core._find_memory
_append_source = _memory_core._append_source
_memory_id = _memory_core._memory_id
_link_related_memories = _memory_core._link_related_memories
_apply_user_corrections = _memory_core._apply_user_corrections
_decay_event_memories = _memory_core._decay_event_memories
_structured_memory_profile = _memory_core._structured_memory_profile
_format_recall_context = _memory_core._format_recall_context
_natural_memory_line = _memory_core._natural_memory_line
_natural_predicate = _memory_core._natural_predicate
_memory_relevant = _memory_core._memory_relevant
_personal_question_interval = _memory_core._personal_question_interval
_remember_important_interaction = _memory_core._remember_important_interaction
_important_interaction_key = _memory_core._important_interaction_key
_attach_affection_metadata = _memory_core._attach_affection_metadata
_affection_state_text = _memory_core._affection_state_text
_group_activity_state_text = _memory_core._group_activity_state_text
_affection_summary_text = _memory_core._affection_summary_text
_affection_set_text = _memory_core._affection_set_text
_affection_reset_text = _memory_core._affection_reset_text
_event_key = _memory_core._event_key
_clean_value = _memory_core._clean_value
_valid_preference_value = _memory_core._valid_preference_value
_is_noisy_for_long_memory = _memory_core._is_noisy_for_long_memory
_is_negative_quality_complaint = _memory_core._is_negative_quality_complaint


# ---------------------------------------------------------------------------
# 全局人物身份：以 QQ 号为稳定锚点，跨私聊/群聚合同一个人的公开信息，
# 不改变任何写入结构，只在读取时构建一张“人物卡”。
# ---------------------------------------------------------------------------

def parse_identity_user_id(profile_id: str) -> str | None:
    """从 private:qq 或 group:gid:user:qq 解析出 QQ 号；其它形态返回 None。"""
    text = str(profile_id or "")
    private_match = re.fullmatch(r"private:([^:]+)", text)
    if private_match:
        return private_match.group(1)
    member = _group_member_identity(text)
    if member:
        return member[1]
    return None


def _identity_scopes_for_user(conversations: dict, user_id: str) -> list[str]:
    """该用户的私聊会话 + 所有群成员会话。"""
    scopes = [f"private:{user_id}"]
    prefix = ":user:" + user_id
    for cid in conversations:
        if cid.endswith(prefix) and cid.startswith("group:"):
            scopes.append(cid)
    return scopes


def _identity_entry_visible(
    entry: dict, source_cid: str, is_private: bool, current_group: str | None
) -> bool:
    """按当前场景判断一条记忆是否可用，私密/跨群内容不泄露。"""
    visibility = entry.get("visibility")
    is_public = visibility == "public" or (
        isinstance(visibility, list) and "public" in visibility
    )
    if is_private:
        # 私聊里：本人私聊会话全部可见；群里的话只取标记为公开的。
        if source_cid.startswith("private:"):
            return True
        return is_public
    # 群聊里：本群说的可见；公开项可见；明确授权本群的可见。
    if entry.get("source_context") == current_group:
        return True
    if is_public:
        return True
    return visibility == current_group or (
        isinstance(visibility, list) and current_group in visibility
    )


def _dedupe_identity_entries(entries: list[dict], limit: int) -> list[dict]:
    """按 key+value 去重，保留最近更新的条目，再截断。"""
    unique: dict[tuple, dict] = {}
    for entry in entries:
        key = (str(entry.get("category")), str(entry.get("key")), str(entry.get("value")))
        previous = unique.get(key)
        if previous is None or float(entry.get("updated_at") or 0) >= float(
            previous.get("updated_at") or 0
        ):
            unique[key] = entry
    ordered = sorted(
        unique.values(),
        key=lambda item: float(item.get("updated_at") or item.get("at") or 0),
        reverse=True,
    )
    return ordered[:limit]


def build_global_identity(
    data: dict, profile_id: str, current_nickname: str | None = None
) -> dict:
    """构建以 QQ 号为准的全局人物卡；没有可用信息时返回空 dict。"""
    user_id = parse_identity_user_id(profile_id)
    if not user_id:
        return {}
    conversations = data.get("conversations", {})
    is_private = str(profile_id).startswith("private:")
    current_group = None if is_private else f"group:{_group_member_identity(profile_id)[0]}"

    l1_entries: list[dict] = []
    l2_entries: list[dict] = []
    nicknames: list[str] = []

    def remember_nickname(name: Any) -> None:
        name = str(name or "").strip()
        if name and name not in nicknames:
            nicknames.append(name)

    if current_nickname:
        remember_nickname(current_nickname)

    for scope in _identity_scopes_for_user(conversations, user_id):
        item = conversations.get(scope)
        if not isinstance(item, dict):
            continue
        memory = item.get("structured_memory")
        if isinstance(memory, dict):
            for entry in memory.get("l1", []) or []:
                if isinstance(entry, dict) and _identity_entry_visible(
                    entry, scope, is_private, current_group
                ):
                    l1_entries.append(entry)
            for entry in memory.get("l2", []) or []:
                if isinstance(entry, dict) and _identity_entry_visible(
                    entry, scope, is_private, current_group
                ):
                    l2_entries.append(entry)
        for history_entry in item.get("history", []) or []:
            if isinstance(history_entry, dict):
                remember_nickname(history_entry.get("nickname"))

    # 当前昵称固定排在最前，其余作为曾用昵称。
    known_nicknames = nicknames[:6]
    result = {
        "user_id": user_id,
        "current_nickname": current_nickname or (known_nicknames[0] if known_nicknames else ""),
        "known_nicknames": known_nicknames,
        "l1": _dedupe_identity_entries(l1_entries, 8),
        "l2": _dedupe_identity_entries(l2_entries, 6),
    }
    if not result["l1"] and not result["l2"] and not result["current_nickname"]:
        return {}
    return result

