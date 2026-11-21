from pathlib import Path

GF = Path('src/atri_qq_bot/group_reply_focus.py')
SE = Path('src/atri_qq_bot/onebot/server_parts/server_events.py')

old_open = '''
        current_time = time.time() if now is None else float(now)
        self._active[key] = _ActiveGroupSession(
            expires_at=current_time + self.active_session_ttl_seconds,
            nickname=str(nickname or "").strip(),
            opened_at=current_time,
            stage=0,
        )
        self._trim()
'''
new_open = '''
        current_time = time.time() if now is None else float(now)
        existing = self._active.get(key)
        if existing is not None:
            hard_deadline = existing.opened_at + ACTIVE_SESSION_HARD_CAP_SECONDS
            if current_time >= hard_deadline:
                # 已达从首次开窗起的绝对上限：不再因再次 @ 重新开窗，
                # 关闭会话并进入冷却；冷却期内能否回复由 is_suppressed 分支决定。
                self._active.pop(key, None)
                self._suppressed[key] = hard_deadline + END_CONVERSATION_SUPPRESS_SECONDS
                self._trim()
                return
            # 同一场对话内再次 @：保留首次 opened_at，硬上限不重置，
            # 仅延长本段有效期且不突破硬上限。
            existing.expires_at = min(
                current_time + self.active_session_ttl_seconds, hard_deadline
            )
            if nickname:
                existing.nickname = str(nickname).strip()
            self._trim()
            return
        self._active[key] = _ActiveGroupSession(
            expires_at=current_time + self.active_session_ttl_seconds,
            nickname=str(nickname or "").strip(),
            opened_at=current_time,
            stage=0,
        )
        self._trim()
'''
old_hard = '''
        hard_deadline = session.opened_at + ACTIVE_SESSION_HARD_CAP_SECONDS
        if current_time > session.expires_at or current_time >= hard_deadline:
            self._active.pop(key, None)
            return False
'''
new_hard = '''
        hard_deadline = session.opened_at + ACTIVE_SESSION_HARD_CAP_SECONDS
        hit_hard_cap = current_time >= hard_deadline
        if current_time > session.expires_at or hit_hard_cap:
            self._active.pop(key, None)
            # 绝对硬上限到期：自动进入冷却，停止主动续聊/搭话；
            # 仅段内 idle 过期（未过硬上限）不冷却，保留原有行为。
            if hit_hard_cap:
                self._suppressed[key] = hard_deadline + END_CONVERSATION_SUPPRESS_SECONDS
            return False
'''
old_amb = '''
                # 没有任何明确触发时，普通群聊由模型按内容判断是否搭话（带冷却）。
                if not preliminary_reply:
                    preliminary_reply = await self._maybe_ambient_group_reply(
                        event, plain_text
                    )
'''
new_amb = '''
                # 没有任何明确触发时，普通群聊由模型按内容判断是否搭话（带冷却）。
                # 硬上限刚触发冷却时也不得再走随机搭话。
                if (
                    not preliminary_reply
                    and not self._group_reply_focus.is_suppressed(event)
                ):
                    preliminary_reply = await self._maybe_ambient_group_reply(
                        event, plain_text
                    )
'''

def patch(path, pairs):
    text = path.read_text()
    for old, new in pairs:
        c = text.count(old)
        assert c == 1, f'{path}: expected 1, got {c} for {old[:30]!r}'
        text = text.replace(old, new)
    path.write_text(text)
    print('patched', path)

patch(GF, [(old_open, new_open), (old_hard, new_hard)])
patch(SE, [(old_amb, new_amb)])
print('ALL PATCHES APPLIED')
