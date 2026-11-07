"""快速检查上游模型 API 是否可用。

用项目自己的 load_config 读取真实 .env（避免 shell source 遇到特殊字符报错），
再发一个最小的 chat/completions 请求，打印 HTTP 状态码和返回内容。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许在未安装包的情况下直接用源码运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from atri_qq_bot.config.loader import load_config


def main() -> int:
    config = load_config()
    url = config.openai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.openai_model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }
    print("请求地址:", url)
    print("使用模型:", config.openai_model)
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=20.0)
    except Exception as exc:
        print("请求异常:", repr(exc))
        return 1
    print("HTTP状态码:", response.status_code)
    print("返回内容:", response.text[:500])
    return 0 if response.status_code == 200 else 2


if __name__ == "__main__":
    raise SystemExit(main())
