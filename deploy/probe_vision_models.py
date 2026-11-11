"""临时诊断：对照探测各 flash 模型的纯文本与图片调用是否可用。

在服务器 /opt/atri-qq-bot 下运行：
    .venv/bin/python deploy/probe_vision_models.py

对每个候选模型分别发一次纯文本请求和一次带图请求，打印 HTTP 状态，
用于区分“模型整体 404（列表有但后端未接入）”与“仅图片路由 404”，
并直接找出能正常处理图片的模型。
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import httpx

# 1x1 PNG，体积最小，仅用于探测多模态路由。
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


async def _post(client: httpx.AsyncClient, base: str, headers: dict, payload: dict) -> str:
    try:
        response = await client.post(
            f"{base}/chat/completions", headers=headers, json=payload
        )
    except Exception as exc:
        return f"{type(exc).__name__}"
    if response.status_code == 200:
        return "200 OK"
    return f"{response.status_code} {response.text[:120]}"


async def main() -> None:
    env = _load_env("/opt/atri-qq-bot/.env")
    base = env["OPENAI_BASE_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {env['OPENAI_API_KEY']}"}
    data_url = (
        f"data:image/png;base64,{base64.b64encode(_TINY_PNG).decode('ascii')}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        models_response = await client.get(f"{base}/models", headers=headers)
        models = [
            item["id"]
            for item in models_response.json().get("data", [])
            if "flash" in item["id"] or "argon" in item["id"]
        ]
        print(f"探测 {len(models)} 个 flash/argon 模型")
        print("=" * 70)
        for model in sorted(models):
            text_payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with: ok"}],
                "max_tokens": 10,
            }
            image_payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What is in this image? One short sentence."},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "max_tokens": 60,
            }
            text_result = await _post(client, base, headers, text_payload)
            image_result = await _post(client, base, headers, image_payload)
            print(f"{model}")
            print(f"  纯文本: {text_result}")
            print(f"  带图片: {image_result}")
        print("=" * 70)
        print("选带图片显示 200 OK 的模型作为 TOOLBOX_VISION_MODEL。")


if __name__ == "__main__":
    asyncio.run(main())
