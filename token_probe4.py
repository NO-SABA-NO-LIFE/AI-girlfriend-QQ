import httpx, time

env = {}
for line in open("/opt/atri-qq-bot/.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip(chr(34)).strip(chr(39))

base = env["OPENAI_BASE_URL"].rstrip("/")
headers = {"Authorization": "Bearer " + env["OPENAI_API_KEY"], "Content-Type": "application/json"}
sysp = open("/opt/atri-qq-bot/docs/prompts/atri_persona.md", encoding="utf-8").read()
msgs = [
    "I have had a long day and feel a bit down. Can you chat with me about staying up late gaming?",
    "Tell me a short joke to cheer me up.",
    "What do you think of the weather today?",
    "I finally finished my big project today!",
    "Do you ever get tired of talking to me?",
]

for i, um in enumerate(msgs):
    payload = {
        "model": env["OPENAI_MODEL"],
        "messages": [{"role": "system", "content": sysp},
                     {"role": "user", "content": um}],
        "max_tokens": 350,
        "temperature": float(env.get("TEMPERATURE", "0.8")),
        "reasoning_effort": "low",
    }
    t0 = time.time()
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(base + "/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            r = resp.json()
        c = r["choices"][0]
        content = c["message"]["content"] or ""
        print("#%d" % i, "%.1fs" % (time.time()-t0), "| finish:", c.get("finish_reason"),
              "| ct:", r.get("usage", {}).get("completion_tokens"),
              "| chars:", len(content), "| emotion:", "[[emotion:" in content)
    except Exception as e:
        print("#%d" % i, "ERROR", repr(e))
