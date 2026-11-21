path = "/opt/atri-qq-bot/src/atri_qq_bot/persona/reply_fallbacks.py"
old = open("/opt/atri-qq-bot/old_block.txt", encoding="utf-8").read()
new = open("/opt/atri-qq-bot/new_block.txt", encoding="utf-8").read()
src = open(path, encoding="utf-8").read()
count = src.count(old)
print("matches:", count)
if count != 1:
    raise SystemExit("abort: expected exactly one match")
open(path, "w", encoding="utf-8").write(src.replace(old, new))
print("applied")
