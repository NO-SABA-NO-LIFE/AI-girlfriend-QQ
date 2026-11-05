"""Helldivers 模块统一日志入口。

原 AstrBot 插件通过 ``from astrbot.api import logger`` 记录日志；移植到亚托莉后
改用标准库 logging。业务代码用到的 debug/info/warning/error/exception 方法在
标准 logging 上签名一致，因此只需替换导入语句，日志调用本身无需改动。
"""

from __future__ import annotations

import logging

# 统一挂在 atri 命名空间下，便于与亚托莉主程序日志一起过滤、分级。
logger = logging.getLogger("atri.helldivers")
