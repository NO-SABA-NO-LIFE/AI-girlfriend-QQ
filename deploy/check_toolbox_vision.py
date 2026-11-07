"""直接走 toolbox 完整流程，检测视觉分析当前是否可用。

和 test_vision.py（只测裸 API）不同，这个脚本构造真实的 ToolAnalyzer，
让它经历下载、编码、调用云端视觉模型、解析结果的完整链路，
最贴近 QQ 里实际发图时的行为。

用法：
    .venv/bin/python deploy/check_toolbox_vision.py [图片URL]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atri_qq_bot.config.loader import load_config
from atri_qq_bot.toolbox import ToolAnalyzer

# 内容明确的公开小图（PNG），可被命令行参数覆盖。
DEFAULT_IMAGE_URL = "https://httpbin.org/image/png"


async def main() -> int:
    image_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMAGE_URL
    config = load_config()
    print("视觉开关 toolbox_vision_enabled:", config.toolbox_vision_enabled)
    print("视觉模型:", config.toolbox_vision_model or config.openai_model)
    print("视觉地址:", config.toolbox_vision_base_url or config.openai_base_url)
    print("测试图片:", image_url)
    print("-" * 52)

    analyzer = ToolAnalyzer(config)
    event = {"message": [{"type": "image", "data": {"url": image_url}}]}
    try:
        result = await analyzer.analyze(event, "")
    except Exception as exc:
        print("分析抛出异常:", type(exc).__name__, exc)
        return 1

    if result is None:
        print("返回 None：工具关闭或没有识别到图片材料")
        return 2

    print("read_level（读取深度）:", result.read_level)
    print("findings（分析结论）:")
    for line in result.findings:
        print("  -", line)
    if result.limitations:
        print("limitations（限制说明）:")
    for line in result.limitations:
        print("  *", line)
    print("-" * 52)
    # 只有真正读到了画面内容才算视觉可用。
    usable = result.read_level not in {"metadata_only", "unknown"}
    print("结论：视觉分析", "可正常使用" if usable else "未能读到画面（在降级运行）")
    return 0 if usable else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
