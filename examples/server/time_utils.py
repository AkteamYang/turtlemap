#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/02 18:26
# @Author  : YaHaoo
# @File    : time_utils.py

"""示例服务端时间工具。"""

from __future__ import annotations

import time
from datetime import datetime


def datetime_to_ms(value: datetime | None) -> int | None:
    """把 datetime 转换成毫秒时间戳。

    参数:
        value: 可选 datetime。

    返回:
        毫秒时间戳；为空时返回 None。
    """

    if value is None:
        return None
    return int(value.timestamp() * 1000)


def now_ms() -> int:
    """返回当前毫秒时间戳。

    返回:
        当前 Unix 毫秒时间戳。
    """

    return int(time.time() * 1000)
