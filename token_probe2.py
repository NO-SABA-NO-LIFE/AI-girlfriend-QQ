import httpx, json

env = {}
for line in open("/opt/atri-qq-bot/.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip(chr(34)).strip(chr(39))

base = env["OPENAI_BASE_URL"].rstrip("/")
headers = {"Authorization": "Bearer " + env["OPENAI_API_KEY"], "Content-Type": "application/json"}
sysp = open("/opt/atri-qq-bot/docs/prompts/atri_persona.md", encoding="utf-8").read()
user_msg = "I have had a long day and feel a bit down. Can you chat with me and tell me honestly what you think about me staying up late gaming every night?"

for mt in [350, 1000]:
    payload = {
        "model": env["OPENAI_MODEL"],
        "messages": [{"role": "system", "content": sysp},
                     {"role": "user", "content": user_msg}],
        "max_tokens": mt,
        "temperature": float(env.get("TEMPERATURE", "0.8")),
    }
    with httpx.Client(timeout=100) as client:
        resp = client.post(base + "/chat/completions", headers=headers, json=payload)
        r = resp.json()
    print("==== requested max_tokens =", mt)
    print("usage:", json.dumps(r.get("usage", {}), ensure_ascii=False))
    print("finish_reason:", r["choices"][0].get("finish_reason"))
    print()
