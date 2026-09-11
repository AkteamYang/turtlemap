#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 22:01
# @Author  : YaHaoo
# @File    : executor.py

"""turtlemap os 层默认后台执行器占位实现。"""

from __future__ import annotations

from pydantic import BaseModel

from turtlemap.kernel.tool import ExecutableTool, ToolResult
from turtlemap.os.protocols import ExecutorProtocol


class NoopExecutor(ExecutorProtocol):
    """表示当前阶段默认不执行后台任务的占位执行器。

    说明:
        当前实现主要用于先完成 OSService 默认装配。后续若真正接入
        background tool 执行链，再在 os 层替换为支持 submit/poll 的正式实现。
    """

    __slots__ = ()

    async def submit(
        self,
        tool_call_id: str,
        tool: ExecutableTool,
        tool_input: BaseModel,
    ) -> None:
        """提交后台任务。

        参数:
            tool_call_id: 当前工具调用的唯一标识。
            tool: 当前待执行的工具对象。
            tool_input: 当前工具调用已完成校验和上下文绑定的输入模型。
        """

        _ = (tool_call_id, tool, tool_input)

    async def poll(self, tool_call_id: str) -> ToolResult | None:
        """查询后台任务稳定结果。

        参数:
            tool_call_id: 当前待查询的工具调用唯一标识。

        返回:
            当前默认实现始终返回 `None`，表示尚无后台结果。
        """

        _ = tool_call_id
        return None
