"""临时诊断：实测线上视觉分析一次的端到端耗时。

在服务器 /opt/atri-qq-bot 下运行：
    .venv/bin/python deploy/bench_vision_latency.py
    .venv/bin/python deploy/bench_vision_latency.py 8   # 自定义次数

脚本读取真实 .env，构造与线上一致的 ToolAnalyzer，直接调用完整的
``_analyze_image_with_vision``（含真实 prompt、max_tokens、解析与清洗），
用一张带形状和文字的合成图连续发起 N 次独立请求，打印每次的端到端
耗时和成功/失败，最后给出最小/最大/平均/中位数，便于判断单次超时
（55 秒均分值）相对真实延迟是否充裕。不修改任何运行中的服务。
"""
from __future__ import annotations

import asyncio
import io
import statistics
import sys
import time

from PIL import Image, ImageDraw

from atri_qq_bot.config import load_config
from atri_qq_bot.toolbox import ToolAnalyzer


def _build_test_image() -> bytes:
    # 生成一张有形状、有文字的图，避免纯白导致模型秒回，贴近真实素材。
    image = Image.new("RGB", (512, 512), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 260, 260), fill=(90, 140, 220), outline=(20, 40, 90), width=6)
    draw.ellipse((280, 280, 460, 460), fill=(220, 120, 90), outline=(90, 30, 20), width=6)
    draw.line((40, 420, 480, 320), fill=(40, 40, 40), width=8)
    draw.text((90, 300), "HELLDIVERS 2", fill=(20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _run(times: int) -> None:
    config = load_config()
    analyzer = ToolAnalyzer(config)
    image_bytes = _build_test_image()

    print(f"模型: {analyzer.vision_model}")
    print(f"总 deadline: {analyzer.vision_timeout_seconds}s, 重试次数: {analyzer.vision_retry_count}")
    print(f"图片大小: {len(image_bytes)} bytes, 连续测试 {times} 次")
    print("-" * 64)

    latencies: list[float] = []
    successes = 0
    for index in range(times):
        started = time.perf_counter()
        try:
            output, hint = await analyzer._analyze_image_with_vision(
                image_bytes, "bench-image"
            )
        except Exception as exc:  # 整体抛出也算一次失败
            elapsed = time.perf_counter() - started
            latencies.append(elapsed)
            print(f"#{index + 1:>2} {elapsed * 1000:8.0f} ms  异常: {type(exc).__name__}")
            continue
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        ok = bool(output)
        successes += int(ok)
        tag = "成功" if ok else f"空({hint or '无输出'})"
        print(f"#{index + 1:>2} {elapsed * 1000:8.0f} ms  {tag}  输出 {len(output)} 字")

    print("-" * 64)
    if latencies:
        ms = [value * 1000 for value in latencies]
        print(f"成功 {successes}/{times}")
        print(f"最小 {min(ms):.0f} ms | 平均 {statistics.mean(ms):.0f} ms | "
              f"中位 {statistics.median(ms):.0f} ms | 最大 {max(ms):.0f} ms")
        print("参考：55 秒均分给 3 次，首次单次超时约 18333 ms。")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    count = max(1, min(10, count))
    asyncio.run(_run(count))
