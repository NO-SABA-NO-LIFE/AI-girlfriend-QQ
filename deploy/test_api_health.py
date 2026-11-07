"""快速检查上游模型 API 是否可用。

用项目自己的 load_config 读取真实 .env（避免 shell source 遇到特殊字符报错），
再对一个或多个模型发最小 chat/completions 请求，打印每个模型的 HTTP 状态码和耗时。

用法：
    python deploy/test_api_health.py            # 测试当前模型 + 几个常见模型
    python deploy/test_api_health.py gpt-5.6    # 只测指定模型
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# 允许在未安装包的情况下直接用源码运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from atri_qq_bot.config.loader import load_config

# 同一中转站里可用于对比的模型，帮助判断是整站故障还是单模型故障。
_COMPARISON_MODELS = ["gpt-5.6", "gpt-6-sol", "gpt-6.1-sol"]


def _probe(client: httpx.Client, url: str, headers: dict, model: str) -> None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }
    started = time.perf_counter()
    try:
        response = client.post(url, headers=headers, json=payload)
        elapsed = time.perf_counter() - started
        print(f"[{model}] HTTP {response.status_code}  耗时 {elapsed:.1f}s")
        if response.status_code != 200:
            print("        返回:", response.text[:200])
    except Exception as exc:
        elapsed = time.perf_counter() - started
        print(f"[{model}] 异常 {type(exc).__name__}: {exc}  耗时 {elapsed:.1f}s")


def main() -> int:
    config = load_config()
    url = config.openai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.openai_api_key}",
        "Content-Type": "application/json",
    }
    models = sys.argv[1:] or [config.openai_model] + [
        m for m in _COMPARISON_MODELS if m != config.openai_model
    ]
    print("请求地址:", url)
    print("测试模型:", ", ".join(models))
    print("-" * 48)
    with httpx.Client(timeout=20.0) as client:
        for model in models:
            _probe(client, url, headers, model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
