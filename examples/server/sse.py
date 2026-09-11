#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/02 18:26
# @Author  : YaHaoo
# @File    : sse.py

"""示例服务端 SSE 文本格式化工具。"""

from __future__ import annotations

from typing import Any

from .schemas import ApiCode, ApiResponse, SseEventType


def success_response(data: Any) -> ApiResponse:
    """构建成功统一响应。

    参数:
        data: 响应数据。

    返回:
        ApiResponse。
    """

    return ApiResponse(code=ApiCode.OK, message="ok", success=True, data=data)


def response_json(code: ApiCode, message: str, success: bool, data: Any) -> str:
    """序列化 ApiResponse。

    参数:
        code: 响应码或 SSE start 操作码。
        message: 响应说明。
        success: 是否成功。
        data: 响应数据。

    返回:
        JSON 字符串。
    """

    return ApiResponse(
        code=code,
        message=message,
        success=success,
        data=data,
    ).model_dump_json()


def build_start_sse(
    code: ApiCode,
    message: str,
    session_id: str,
    run_id: str,
) -> str:
    """构建 SSE start 控制事件。

    参数:
        code: SSE start 控制操作码。
        message: 操作码文本。
        session_id: 当前会话 id。
        run_id: 当前 agent 运行 id。

    返回:
        SSE 字符串。
    """

    data = response_json(
        code=code,
        message=message,
        success=True,
        data={
            "session_id": session_id,
            "run_id": run_id,
        },
    )
    return format_sse(sse_id=None, event=SseEventType.START, data=data)


def format_sse(sse_id: str | None, event: SseEventType, data: str) -> str:
    """格式化标准 SSE 字符串。

    参数:
        sse_id: SSE id；为空或为 "null" 时不输出 id 字段。
        event: SSE event 枚举。
        data: SSE data 字符串。

    返回:
        可直接写入 StreamingResponse 的 SSE 文本。
    """

    lines = []
    if sse_id and sse_id != "null":
        lines.append(f"id: {sse_id}")
    lines.append(f"event: {event.value}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"
