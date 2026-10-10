#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved 
#
# @Time    : 2026/07/21 09:51
# @Author  : YaHaoo
# @File    : model.py

"""os 层工具输入上下文模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel

from turtlemap.kernel.models.enums import RuntimeArtifactType
from turtlemap.kernel.tool.models import ToolExecutionContext
from turtlemap.kernel.models.models import BaseAgentFrameChange, RuntimeArtifact
from turtlemap.kernel.models.polymorphic import BaseStateModel
from turtlemap.os.llm.model import LLMCompletionToolCall, LLMMessage

if TYPE_CHECKING:
    from turtlemap.os.tool.service import ToolService


ATTR_TOOL_INPUT_CONTEXT = "_tool_input_context"


@dataclass
class ToolInputContext:
    """表示一次工具调用绑定到输入模型上的运行时上下文。

    属性:
        event_bus_id: 当前工具调用所属的事件通道标识。
        tool_service: 当前工具调用所属的 ToolService，可用于读取 os 层上下文。
        tool_execution_context: 当前工具调用对应的执行单元运行期上下文。

    说明:
        该上下文通过私有属性挂到工具输入模型上，避免污染工具参数 schema。
    """

    # 当前工具调用所属的事件通道标识。
    event_bus_id: str | None = None

    # 当前工具调用所属的 ToolService，供内置工具读取运行期上下文。
    tool_service: "ToolService | None" = None

    # 当前工具调用对应的执行单元运行期上下文。
    tool_execution_context: ToolExecutionContext | None = None

    def bind_to_input_model(self, input_model: BaseModel) -> None:
        """将当前工具上下文绑定到已反序列化的输入模型。

        参数:
            input_model: 当前工具调用的 pydantic 输入模型实例。

        返回:
            无返回值。
        """

        setattr(input_model, ATTR_TOOL_INPUT_CONTEXT, self)

    @classmethod
    def get_input_context(cls, input_model: BaseModel) -> "ToolInputContext | None":
        """从工具输入模型中读取已绑定的工具上下文。

        参数:
            input_model: 当前工具调用的 pydantic 输入模型实例。

        返回:
            已绑定的工具上下文；未绑定时返回 None。
        """

        return getattr(input_model, ATTR_TOOL_INPUT_CONTEXT, None)


@RuntimeArtifact.register_field_type(
    "payload",
    RuntimeArtifactType.AGENT_FRAME_CHANGE,
)
class AgentFrameChange(BaseAgentFrameChange):
    """表示一次 Agent 控制权栈变更的稳定产物。

    说明:
        `type` 描述控制权栈的 push 或 pop 动作，`reason` 描述触发该动作的
        业务语义。该产物同时作为任务状态与 event bus 通知的共享载荷。
    """

    # 目标 Agent 的对外展示名称。
    target_display_name: str = ""

    # 来源 Agent 的对外展示名称。
    source_display_name: str = ""
