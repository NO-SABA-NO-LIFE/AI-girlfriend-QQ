"""Helldivers 移植版帮助文本。

内容与上游 main.py 的 HELP_TEXT / HDHELP_TEXT 对齐，第二阶段已补齐
Companion 网页截图、B站银河快报与订阅/退订相关条目。
"""

from __future__ import annotations

from .core.plugin_info import PLUGIN_VERSION

HELP_TEXT = f"""\
Helldivers 2 银河战争 {PLUGIN_VERSION}
━━━━━━━━━━━━━━━━━━━━
【战况 · 图片卡片】
/dashboard            银河战争总览（实时数据）
/hd2stats             银河战争详细统计（实时数据）
/order                当前最高命令（同 /major_order）
/map                  战争地图（阵营领土）
/global_events        全球事件
/warfront <阵营>      阵营战线战况
/planet <名称/编号>   星球详情
/dss                  民主空间站图文
/steam                Steam 新闻图片
【网页截图 · 快报】
/hd2news             最新新闻网页截图（失败回退新闻卡）
/companion           Companion 首页截图（同 /首页）
/hd2refresh          强制刷新网页截图缓存
/银河快报             最新 B站银河快报
/hd2news订阅 /退订    新闻更新自动推送到本群
/银河快报订阅 /退订    银河快报自动推送到本群
【文本】
/wiki <关键词>        Helldivers Wiki（加 -f 看全文）
/hd2data <名称>       军需簿：武器/护甲/敌人
/hd2ping              数据源自诊断（健康/缓存）
/hd2help              全部指令说明
/hd2                  本帮助
━━━━━━━━━━━━━━━━━━━━
阵营: Terminids / Automaton / Illuminate
     （终结族 / 机器人 / 光能者）
数据: api.helldivers2.dev · wiki.gg · HD2Tool
为了超级地球！"""


HDHELP_TEXT = f"""\
绝地潜兵2情报助手 {PLUGIN_VERSION} · 全部指令
━━━━━━━━━━━━━━━━━━━━

【核心战况 · 实时数据】
/dashboard            银河战争总览（防御/解放/重点战线）
/hd2stats             在线人数、击杀、命中率等统计
/order                当前最高命令任务卡

【地图与战线】
/map                  银河战争地图
/warfront <阵营>      阵营战线 Top 10
/global_events        全球事件
/planet <名称/编号>   星球详情（支持中文名、编号）
/dss                  民主空间站面板

【Companion 网页截图】
/hd2news             最新新闻网页截图，失败自动回退新闻卡片
/companion           Companion 首页截图（别名 /首页）
/hd2refresh          跳过缓存，强制重新截图并汇总结果

【B站银河快报】
/银河快报             立即获取最新一期银河快报（需配置 B站 Cookie）
/银河快报订阅 /退订    开关本群的快报自动推送

【新闻订阅】
/hd2news订阅 /退订    开关本群的 Companion 新闻自动推送
（需在 .env 开启 HD2_NEWS_PUSH 且后台预热可用）

【外部资讯】
/steam                Steam 更新公告
/wiki <关键词>        Wiki 查询（中文自动译名；加 -f 看全文）
/hd2data <名称>       军需簿：武器/护甲/敌人（俗称：电喷/泰坦）

【帮助】
/hd2                  简短帮助
/hd2help              本说明
/hd2ping              自诊断：数据源健康、缓存年龄
━━━━━━━━━━━━━━━━━━━━
阵营: 终结族 / 机器人 / 光能者
为了超级地球！"""
