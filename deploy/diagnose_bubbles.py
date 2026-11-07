"""气泡拆分诊断：在服务器上运行，打印真实配置与拆分结果。

用法（在项目根目录）：
    .venv/bin/python deploy/diagnose_bubbles.py
"""

from __future__ import annotations

from pathlib import Path

from atri_qq_bot.config.loader import load_config
from atri_qq_bot.message_plan.splitting import split_reply_text

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

print("=== .env 中与分段相关的原始设置（没有输出即未设置，用默认值）===")
if ENV.exists():
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if "MESSAGE_SPLIT" in line:
            print("  ", line)
else:
    print("  未找到 .env：", ENV)

config = load_config(ENV)
print()
print("=== 实际加载到的配置值 ===")
print("  message_split_max_chars =", config.message_split_max_chars)
print("  message_split_max_parts =", config.message_split_max_parts)
print("  hd2_merge_messages      =", config.hd2_merge_messages)

SAMPLE = (
    "截至2026年11月7日，我查到最近的官方内容是11月3日上线的新内容："
    "新增了几种带毒性的虫族敌人，虫穴也在银河地图上更多出现。"
    "暂时没查到比这更新的补丁说明；这次更像是敌人与战场内容扩展，不是完整版本更新公告。"
)
print()
print("=== 用真实配置拆分测试文本（总长度", len(SAMPLE), "）===")
parts = split_reply_text(
    SAMPLE, config.message_split_max_chars, config.message_split_max_parts
)
print("  拆成", len(parts), "段：")
for index, part in enumerate(parts, 1):
    print(f"   {index}. {part!r}")
