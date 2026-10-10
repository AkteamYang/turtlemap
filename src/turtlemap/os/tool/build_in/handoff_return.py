#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/10/09 15:47
# @Author  : YaHaoo
# @File    : handoff_return.py

"""os 层内置 handoff 控制权归还工具定义。"""

from __future__ import annotations

from typing import ClassVar

from turtlemap.kernel.models.enums import (
    AgentFrameChangeReason,
    AgentFrameChangeType,
    ToolResultStatus,
)
from turtlemap.kernel.tool import EmptyToolInputModel, ToolDescriptor, ToolMetadata, ToolResult
from turtlemap.kernel.tool.models import K_RAW_DATA_HANDOFF, ToolExecutionContext
from turtlemap.os.tool.enums import BuiltinToolCapabilityCategory
from turtlemap.os.tool.model import AgentFrameChange, ToolInputContext
from turtlemap.shared.typing import ensure_instance

from .base import BuildinTool

K_TOOL_NAME_HANDOFF_RETURN = "_handoff_return"


class HandoffReturnTool(BuildinTool[EmptyToolInputModel]):
    """表示由子 Agent 主动请求归还 handoff 控制权的内置工具。

    说明:
        该工具只声明模型主动归还控制权的语义，不直接修改 Agent frame 或任务状态。
        ToolService 与 Runtime 会通过标准 handoff raw data 识别其结果，并在任务完成后
        执行实际控制权切换。
    """

    # 内置工具对模型暴露的稳定名称。
    TOOL_NAME: ClassVar[str] = K_TOOL_NAME_HANDOFF_RETURN

    def __init__(self) -> None:
        """初始化 handoff 控制权归还工具。"""

        descriptor = ToolDescriptor(
            summary="归还当前会话控制权。",
            capability_category=BuiltinToolCapabilityCategory.RUNTIME.value,
            capability="当前用户输入的问题超出你的能力范围时，归还当前会话控制权上层 Agent 继续处理。",
            use_cases=[
                "当前用户输入的问题超出你已定义的能力范围",
            ],
            anti_use_cases=[
                "当前用户输入的问题仍在你的能力范围内",
                # "仅因当前任务需要继续调用工具、澄清信息或等待用户补充时",
            ],
        )
        super().__init__(
            tool_metadata=ToolMetadata.model(
                name=self.TOOL_NAME,
                input_model=EmptyToolInputModel,
            ),
            tool_descriptor=descriptor,
            input_model=EmptyToolInputModel,
            call=self.call,
        )

    async def call(self, input_model: EmptyToolInputModel) -> ToolResult:
        """构建归还 handoff 控制权所需的标准工具结果。

        参数:
            input_model: 已校验的空输入模型。

        返回:
            携带控制权归还标记的标准工具结果。

        说明:
            本方法只构建标准工具结果，不直接变更 Agent frame；任务完成后由既有 handoff
            控制链读取 `K_RAW_DATA_HANDOFF` 并执行 POP。
        """

        tool_input_context = ensure_instance(
            ToolInputContext.get_input_context(input_model),
            ToolInputContext,
            "handoff 控制权归还工具输入上下文",
        )
        tool_execution_context = ensure_instance(
            tool_input_context.tool_execution_context,
            ToolExecutionContext,
            "handoff 控制权归还工具执行上下文",
        )
        agent_frames = tool_execution_context.session_state.agent_frames
        if len(agent_frames) < 2:
            return ToolResult(
                status=ToolResultStatus.FAILED,
                content="当前不存在可归还控制权的父 Agent。",
                purpose="return handoff control to the parent agent",
            )

        from turtlemap.os.agent import Agent

        source_agent = ensure_instance(
            tool_execution_context.agent_name2agent.get(agent_frames[-1].agent_name),
            Agent,
            "handoff 控制权归还来源 Agent",
        )
        target_agent = ensure_instance(
            tool_execution_context.agent_name2agent.get(agent_frames[-2].agent_name),
            Agent,
            "handoff 控制权归还目标 Agent",
        )

        # 复用 handoff 的稳定 raw data 协议，由任务完成链统一执行对应 POP 操作。
        agent_frame_change = AgentFrameChange(
            source=agent_frames[-1].agent_name,
            target=agent_frames[-2].agent_name,
            type=AgentFrameChangeType.POP,
            reason=AgentFrameChangeReason.HANDOFF_RETURN_TOOL,
            source_display_name=source_agent.name,
            target_display_name=target_agent.name,
        )
        return ToolResult(
            content=(
                f"当前用户输入的问题超出 `{source_agent.name}` 的处理范围，"
                f"会话控制权将归还给 `{target_agent.name}`。"
            ),
            purpose="return handoff control to the parent agent",
            raw_data={K_RAW_DATA_HANDOFF: agent_frame_change},
        )
