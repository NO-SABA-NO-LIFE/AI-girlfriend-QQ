import httpx, time, json

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

variants = [
    ("baseline_mt2000", {"max_tokens": 2000}),
    ("reasoning_effort_low", {"max_tokens": 2000, "reasoning_effort": "low"}),
    ("reasoning_effort_none", {"max_tokens": 2000, "reasoning_effort": "none"}),
    ("thinking_disabled", {"max_tokens": 2000, "thinking": {"type": "disabled"}}),
]

for name, extra in variants:
    payload = {
        "model": env["OPENAI_MODEL"],
        "messages": [{"role": "system", "content": sysp},
                     {"role": "user", "content": user_msg}],
        "temperature": float(env.get("TEMPERATURE", "0.8")),
    }
    payload.update(extra)
    t0 = time.time()
    try:
        with httpx.Client(timeout=110) as client:
            resp = client.post(base + "/chat/completions", headers=headers, json=payload)
            dt = time.time() - t0
            if resp.status_code != 200:
                print(name, "HTTP", resp.status_code, resp.text[:200], "%.1fs" % dt)
                print()
                continue
            r = resp.json()
        c = r["choices"][0]
        content = c["message"]["content"] or ""
        print("====", name, "%.1fs" % dt)
        print("finish_reason:", c.get("finish_reason"), "| completion_tokens:", r.get("usage", {}).get("completion_tokens"), "| visible_chars:", len(content), "| emotion:", "[[emotion:" in content)
        print()
    except Exception as e:
        print(name, "ERROR", repr(e), "%.1fs" % (time.time()-t0))
        print()
