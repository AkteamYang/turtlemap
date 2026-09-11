#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/03 22:42
# @Author  : YaHaoo
# @File    : stream_context.py

"""completion SSE 请求上下文。"""

from __future__ import annotations

from dataclasses import dataclass

from turtlemap.kernel.models.enums import EventType
from turtlemap.kernel.models.models import InterruptionResponsePayload


@dataclass(slots=True)
class CompletionStreamContext:
    """表示一次 SSE completion 请求的执行上下文。

    约束:
        普通用户输入携带 query 与 client_event_id；中断恢复输入携带
        interruption_response；页面首次 resume 不携带业务输入，由后台执行阶段
        根据运行时状态决定 resume 或 replay。
    """

    # 当前会话 id。
    session_id: str

    # 输入业务类型；页面首次 resume 时为空。
    event_type: EventType | None

    # 用户输入文本；中断恢复和页面首次 resume 时为空。
    query: str | None

    # 中断恢复的结构化响应；其他输入类型时为空。
    interruption_response: InterruptionResponsePayload | None

    # 客户端已收到的最后一个 SSE id；普通续接时使用。
    last_event_id: str | None

    # 前端幂等 id；页面首次 resume 时为空。
    client_event_id: str | None

    # 是否为页面首次加载恢复请求。
    page_resume: bool

    # 当前 SSE 请求是否已被外部取消；取消后后台逻辑继续执行，但不再发送 SSE。
    request_cancelled: bool = False
