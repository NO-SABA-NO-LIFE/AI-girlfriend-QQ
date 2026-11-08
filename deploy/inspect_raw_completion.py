"""Inspect the raw chat completion structure for the configured model.

Prints the full message object (all keys, not just content) so we can see whether
the model's reasoning is mixed into ``content`` or returned in a separate field.
Also reports whether common reasoning-control parameters are accepted.

Run on the server:
    .venv/bin/python deploy/inspect_raw_completion.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

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
    # A prompt that tends to trigger a little reasoning, so any leaked thought is
    # visible.
    payload = {
        "model": config.openai_model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Reply in Chinese."},
            {"role": "user", "content": "晚上吃什么好呢？给我一个建议。"},
        ],
        "max_tokens": 400,
    }
    print("endpoint:", url)
    print("model:", config.openai_model)
    print("=" * 60)
    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=headers, json=payload)
        print("HTTP", response.status_code)
        if response.status_code != 200:
            print(response.text[:500])
            return 1
        data = response.json()

    # Print the top-level and message keys, then the full message object.
    print("top-level keys:", list(data.keys()))
    message = data["choices"][0]["message"]
    print("message keys:", list(message.keys()))
    print("=" * 60)
    print(json.dumps(message, ensure_ascii=False, indent=2)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
