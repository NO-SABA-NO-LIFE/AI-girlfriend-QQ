path = "/opt/atri-qq-bot/.env"
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
out = []
hits = 0
for line in lines:
    if line.startswith("MAX_TOKENS="):
        hits += 1
        out.append("MAX_TOKENS=350\n")
    else:
        out.append(line)
print("hits:", hits)
if hits != 1:
    raise SystemExit("abort: expected exactly one MAX_TOKENS line")
open(path, "w", encoding="utf-8").write("".join(out))
print("applied")
