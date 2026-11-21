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

time_block = (
    "系统运行时刻：2026-11-19 00:54:55 +0800。这是当前真实日期与时刻。 "
    "构造\"最近/最新/今天\"的搜索关键词时必须以此为准， "
    "不得使用模型训练记忆里的旧年月； "
    "日常对话中，凡涉及\"现在/今天/早晚/作息/早安晚安/该不该睡\"的判断，也必须以此刻为准； "
    "历史对话可能来自更早的时间，不要把历史里的时段或话题（如昨晚说睡觉）当成当前， "
    "跨过午夜即为新的一天；需要精确时间时仍调用 get_current_time。"
)
focus_block = (
    "本轮强制回复焦点：\n"
    "回复对象：群友\n"
    "焦点内容：早上好\n"
    "第一句必须先回应上面的焦点内容。其他群友消息只能作为背景， "
    "除非当前回复对象明确要求讨论别人，否则不得优先回答其他人的消息。"
)
system_content = sysp + "\n\n" + time_block

for i in range(4):
    payload = {
        "model": "gemini-3.6-flash",
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "system", "content": focus_block},
            {"role": "user", "content": "@亚托莉"},
        ],
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
        content = c["message"]["content"]
        print("#%d %.1fs finish=%s ct=%s" % (i, time.time()-t0, c.get("finish_reason"), r.get("usage", {}).get("completion_tokens")))
        print(content)
        print("======")
    except Exception as e:
        print("#%d ERROR %r" % (i, e))
