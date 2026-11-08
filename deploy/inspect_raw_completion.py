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

    # Each candidate reasoning-control parameter is tried in turn. The first
    # request is the plain baseline. We print content as repr so CJK characters
    # survive the VNC terminal as \\uXXXX escapes.
    variants: list[tuple[str, dict]] = [
        ("baseline", {}),
        ("reasoning_effort=none", {"reasoning_effort": "none"}),
        ("reasoning_effort=minimal", {"reasoning_effort": "minimal"}),
        ("thinking.disabled", {"thinking": {"type": "disabled"}}),
        ("extra_body google budget 0", {"extra_body": {"google": {"thinking_config": {"thinking_budget": 0}}}}),
    ]

    with httpx.Client(timeout=30.0) as client:
        for label, extra in variants:
            run_payload = dict(payload)
            run_payload.update(extra)
            try:
                response = client.post(url, headers=headers, json=run_payload)
            except Exception as exc:  # noqa: BLE001
                print(f"[{label}] request error: {exc!r}")
                print("-" * 60)
                continue
            print(f"[{label}] HTTP {response.status_code}")
            if response.status_code != 200:
                print("  body:", response.text[:300])
                print("-" * 60)
                continue
            body = response.json()
            msg = body["choices"][0]["message"]
            print("  message keys:", list(msg.keys()))
            content = str(msg.get("content") or "")
            # unicode_escape turns every CJK character into literal \\uXXXX ASCII
            # text, which survives the VNC terminal so it can be decoded here.
            ascii_safe = content.encode("unicode_escape").decode("ascii")
            print("  content ascii:", ascii_safe[:1200])
            print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
