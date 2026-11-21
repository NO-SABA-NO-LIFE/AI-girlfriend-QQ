import httpx

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

for mt in [180, 350, 700, 1000]:
    payload = {
        "model": env["OPENAI_MODEL"],
        "messages": [{"role": "system", "content": sysp},
                     {"role": "user", "content": user_msg}],
        "max_tokens": mt,
        "temperature": float(env.get("TEMPERATURE", "0.8")),
    }
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(base + "/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            r = resp.json()
        c = r["choices"][0]
        content = c["message"]["content"] or ""
        print("==== max_tokens =", mt)
        print("finish_reason:", c.get("finish_reason"))
        print("completion_tokens:", r.get("usage", {}).get("completion_tokens"))
        print("has_emotion_tag:", "[[emotion:" in content)
        print("char_len:", len(content))
        print("--- content begin ---")
        print(content)
        print("--- content end ---")
        print()
    except Exception as e:
        print("==== max_tokens =", mt, "ERROR", repr(e))
