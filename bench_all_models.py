"""全模型基准：对话 + 视觉的延迟与质量（SSE 流式，单请求 60s 封顶）。

在服务器 /opt/atri-qq-bot 下运行：
    .venv/bin/python bench_all_models.py
结果同时写入 logs/benchmark-<时间戳>.json。
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
# 允许脚本放在 /tmp 等位置运行：找不到 src 就回退到标准部署目录。
if not (ROOT / "src").exists() and Path("/opt/atri-qq-bot/src").exists():
    ROOT = Path("/opt/atri-qq-bot")
sys.path.insert(0, str(ROOT / "src"))

from atri_qq_bot.config import load_config  # noqa: E402
from atri_qq_bot.language_guard.detector import (  # noqa: E402
    has_illegal_language_or_garbage,
)

REQUEST_TIMEOUT = 60.0
CHAT_PROMPT = (
    "用一两句话，以温柔的语气安慰一个刚下班、很累的朋友。"
    "直接说安慰的话，不要解释，不要分点。"
)
VISION_PROMPT = (
    "用一句中文简短回答：这张图的背景主要是什么颜色？中间有什么形状？"
)


def build_test_image() -> tuple[str, str]:
    """红底 + 蓝色实心圆，返回 (base64, mime)。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (480, 360), (220, 35, 35))
    draw = ImageDraw.Draw(img)
    draw.ellipse((150, 70, 330, 250), fill=(30, 90, 220))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode(), "image/png"


def is_repetitive(text: str) -> bool:
    if re.search(r"(.)\1{7,}", text):
        return True
    for size in (4, 6):
        for i in range(0, max(0, len(text) - size * 4)):
            piece = text[i : i + size]
            if text.count(piece) >= 4:
                return True
    return False


def cjk_ratio(text: str) -> float:
    compact = text.replace(" ", "")
    cjk = len(re.findall(r"[一-鿿]", text))
    return cjk / max(1, len(compact))


def leak_flags(text: str) -> list[str]:
    flags = []
    for marker, name in (
        ("```", "代码块"),
        ("<think", "思考链"),
        ("data:", "数据串"),
        ("http", "链接"),
    ):
        if marker in text:
            flags.append(name)
    return flags


def grade_chat(text: str) -> str:
    if not text or not text.strip():
        return "差(空)"
    flags: list[str] = []
    if has_illegal_language_or_garbage(text):
        flags.append("乱码")
    if cjk_ratio(text) < 0.5:
        flags.append("非中文")
    flags.extend(leak_flags(text))
    if is_repetitive(text):
        flags.append("重复")
    if flags:
        return "差(" + ",".join(sorted(set(flags))) + ")"
    n = len(text)
    if 8 <= n <= 120 and cjk_ratio(text) >= 0.7:
        return "优"
    return "良"


def grade_vision(text: str) -> str:
    base = grade_chat(text)
    hit_red = "红" in text
    hit_shape = any(k in text for k in ("圆", "圈", "球", "环"))
    correct = "答对" if hit_red and hit_shape else (
        "部分" if hit_red or hit_shape else "答错"
    )
    if base.startswith("差"):
        return base + "+" + correct
    return correct


async def stream_once(
    client: httpx.AsyncClient, url: str, headers: dict, payload: dict
) -> dict:
    t0 = time.perf_counter()
    ttfb = None
    chunks: list[str] = []
    try:
        async with client.stream(
            "POST", url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (
                    event.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content")
                )
                if delta:
                    if ttfb is None:
                        ttfb = time.perf_counter() - t0
                    chunks.append(delta)
        text = "".join(chunks)
        return {
            "status": "ok",
            "ttfb": round(ttfb, 2) if ttfb is not None else None,
            "total": round(time.perf_counter() - t0, 2),
            "chars": len(text),
            "text": text,
        }
    except httpx.TimeoutException:
        return {"status": "timeout", "ttfb": None, "total": REQUEST_TIMEOUT,
                "chars": 0, "text": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error:" + type(exc).__name__, "ttfb": None,
                "total": round(time.perf_counter() - t0, 2),
                "chars": 0, "text": ""}


def fmt(v, width: int) -> str:
    return str(v).ljust(width)[:width]


async def main() -> None:
    cfg = load_config()
    base = cfg.openai_base_url.rstrip("/")
    url = base + "/chat/completions"
    headers = {"Authorization": "Bearer " + cfg.openai_api_key}

    async with httpx.AsyncClient() as client:
        models_resp = await client.get(
            base + "/models", headers=headers, timeout=20
        )
        models_resp.raise_for_status()
        models = sorted(d["id"] for d in models_resp.json()["data"])

    image_b64, mime = build_test_image()
    data_uri = f"data:{mime};base64,{image_b64}"
    results = []

    async with httpx.AsyncClient() as client:
        for model in models:
            chat_payload = {
                "model": model,
                "messages": [{"role": "user", "content": CHAT_PROMPT}],
                "temperature": 0.7,
                "max_tokens": 200,
                "stream": True,
            }
            vision_payload = {
                "model": model,
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url",
                         "image_url": {"url": data_uri}},
                    ]}
                ],
                "temperature": 0.3,
                "max_tokens": 200,
                "stream": True,
            }
            chat = await stream_once(client, url, headers, chat_payload)
            vision = await stream_once(client, url, headers, vision_payload)
            chat["grade"] = grade_chat(chat["text"])
            vision["grade"] = grade_vision(vision["text"])
            results.append({"model": model, "chat": chat, "vision": vision})
            print("done:", model, flush=True)

    def row(title: str, r: dict) -> str:
        return (
            fmt(r["model"], 26)
            + fmt(r["status"], 10)
            + fmt(r["ttfb"], 7)
            + fmt(r["total"], 7)
            + fmt(r["chars"], 6)
            + fmt(r["grade"], 22)
        )

    print("\n========== 对话（CHAT）==========")
    print(fmt("model", 26) + fmt("status", 10) + fmt("ttfb", 7)
          + fmt("total", 7) + fmt("chars", 6) + fmt("grade", 22))
    for r in results:
        print(row("chat", r["chat"]))

    print("\n========== 视觉（VISION）==========")
    print(fmt("model", 26) + fmt("status", 10) + fmt("ttfb", 7)
          + fmt("total", 7) + fmt("chars", 6) + fmt("grade", 22))
    for r in results:
        print(row("vision", r["vision"]))

    print("\n========== 回复原文（节选）==========")
    for r in results:
        print("\n# " + r["model"])
        print("  chat :", r["chat"]["text"][:140])
        print("  vision:", r["vision"]["text"][:140])

    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    out = log_dir / f"benchmark-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("\nsaved:", out)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
