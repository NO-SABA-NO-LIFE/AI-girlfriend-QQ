from pathlib import Path
p = Path('src/atri_qq_bot/onebot/server_parts/server_events.py')
old = '''
            if not (at_bot or quotes_bot):
                return
            preliminary_reply = True
'''
new = '''
            # 冷却期重新裁决：仅 @ 或引用可回复，其余一律压回 False
            # （覆盖前面 should_reply 因身份关键词等给出的 True）。
            # 不提前 return：消息继续向下完成上下文与记忆记录，
            # 仅在统一发送门处被拦下，避免冷却期丢失该成员的上下文。
            preliminary_reply = bool(at_bot or quotes_bot)
'''
t = p.read_text()
assert t.count(old) == 1, t.count(old)
p.write_text(t.replace(old, new))
print('SOURCE PATCH APPLIED')
