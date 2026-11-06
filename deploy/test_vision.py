"""临时诊断脚本：测试 higanai 云端各模型是否支持图片输入。

运行方式（在服务器 /opt/atri-qq-bot 下）：
    .venv/bin/python deploy/test_vision.py

脚本只读取 .env 里的密钥和地址，依次向每个可用模型发送一张小图，
打印该模型是否接受图片、返回内容或具体错误。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


# 一张公开的小图（PNG，约 11KB），用于探测模型是否接受图片输入。
TEST_IMAGE_URL = "https://httpbin.org/image/png"


async def test_model(client: httpx.AsyncClient, base: str, headers: dict[str, str], model: str) -> None:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image? Answer in one short sentence."},
                    {"type": "image_url", "image_url": {"url": TEST_IMAGE_URL}},
                ],
            }
        ],
        "max_tokens": 100,
    }
    print("-" * 60)
    print(f"测试模型: {model}")
    try:
        response = await client.post(
            f"{base}/chat/completions",
            headers=headers,
            json=payload,
        )
    except Exception as exc:  # 网络层错误
        print(f"  [网络错误] {type(exc).__name__}: {exc}")
        return

    print(f"  HTTP 状态码: {response.status_code}")
    if response.status_code != 200:
        print(f"  [失败] 返回内容: {response.text[:500]}")
        return

    try:
        data = response.json()
        message = data["choices"][0]["message"]
    except Exception as exc:
        print(f"  [解析失败] {type(exc).__name__}: {response.text[:500]}")
        return

    content = message.get("content")
    print(f"  [成功] 模型接受图片，回复: {str(content)[:300]}")


async def main() -> None:
    env = load_env("/opt/atri-qq-bot/.env")
    base = env["OPENAI_BASE_URL"].rstrip("/")
    key = env["OPENAI_API_KEY"]
    headers = {"Authorization": f"Bearer {key}"}

    # 先拉取当前 key 的全部可用模型。
    async with httpx.AsyncClient(timeout=30) as client:
        models_response = await client.get(f"{base}/models", headers=headers)
        models_response.raise_for_status()
        models = [item["id"] for item in models_response.json().get("data", [])]
        print(f"当前可用模型: {json.dumps(models, ensure_ascii=False)}")

        for model in models:
            await test_model(client, base, headers, model)

    print("-" * 60)
    print("测试完成。若某模型显示 [成功]，即可把它配置为视觉模型。")


if __name__ == "__main__":
    asyncio.run(main())
