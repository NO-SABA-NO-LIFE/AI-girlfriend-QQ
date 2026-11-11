"""临时诊断：列出中转站当前可用的全部模型 ID。

在服务器 /opt/atri-qq-bot 下运行：
    .venv/bin/python deploy/list_models.py

只读取 .env 里的地址和密钥，调用 GET /models，逐行打印模型 ID，
便于核对 .env 配置的模型名是否仍存在、正确写法是什么。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx


def _load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


async def main() -> None:
    env = _load_env("/opt/atri-qq-bot/.env")
    base = env["OPENAI_BASE_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {env['OPENAI_API_KEY']}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{base}/models", headers=headers)
        print("HTTP", response.status_code)
        response.raise_for_status()
        ids = sorted(item["id"] for item in response.json().get("data", []))
    print(f"共 {len(ids)} 个模型：")
    for model_id in ids:
        print(model_id)


if __name__ == "__main__":
    asyncio.run(main())
