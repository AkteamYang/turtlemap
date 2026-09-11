#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/08 15:56
# @Author  : YaHaoo
# @File    : tool.py

"""turtlemap os 层工具相关数据结构定义。"""

from __future__ import annotations

from pydantic import Field

from turtlemap.kernel.tool import (
    RESERVED_UNIT_TYPE_LLM_CALL,
    RESERVED_UNIT_TYPE_TOOL_CALL,
    ExecutionUnit,
    ExecutionUnitResult,
    ToolResult,
)
from turtlemap.kernel.models import RuntimeArtifact, RuntimeArtifactType
from turtlemap.os.llm.model import LLMCompletionToolCall, LLMMessage
from turtlemap.shared.typing import ensure_instance


@ExecutionUnitResult.register_type
class ToolCallExecutionResult(ExecutionUnitResult):
    """os 层工具调用执行单元结果。"""

    # 当前结果固定对应真实工具调用执行单元。
    type_name: str = Field(default=RESERVED_UNIT_TYPE_TOOL_CALL)

    # 当前工具调用形成的稳定工具结果。
    tool_result: ToolResult = Field(default_factory=ToolResult)


@ExecutionUnitResult.register_type
class LLMCallExecutionResult(ExecutionUnitResult):
    """os 层 LLM 调用执行单元结果。"""

    # 当前结果固定对应 LLM continuation 执行单元。
    type_name: str = Field(default=RESERVED_UNIT_TYPE_LLM_CALL)

    # 本次 LLM 调用返回的标准 assistant 消息。
    message: LLMMessage | None = None


@ExecutionUnit.register_type
@RuntimeArtifact.register_field_type("payload", RuntimeArtifactType.TOOL_CALL_EXE)
class ToolCallExecutionUnit(ExecutionUnit):
    """os 层真实工具调用执行单元。

    说明:
        该单元同时承载工具调用、执行前确认和后台结果等待状态。
        确认请求与异步结果请求分别记录，避免组合场景中互相覆盖。
    """

    # 当前执行单元固定对应真实工具调用。
    type_name: str = Field(default=RESERVED_UNIT_TYPE_TOOL_CALL)

    # 被调用工具的元信息 id。
    tool_meta_id: str = ""

    # 模型生成的完整工具调用信息。
    tool_call: LLMCompletionToolCall

    # 后台工具等待外部结果时对应的中断请求 id。
    async_result_request_id: str | None = None

    @property
    def real_result(self) -> ToolCallExecutionResult:
        return ensure_instance(self.result, ToolCallExecutionResult)


@ExecutionUnit.register_type
@RuntimeArtifact.register_field_type("payload", RuntimeArtifactType.TOOL_LLM_RESPONSE)
class LLMCallExecutionUnit(ExecutionUnit):
    """os 层 LLM 执行单元。"""

    # 当前执行单元固定对应 LLM continuation。
    type_name: str = Field(default=RESERVED_UNIT_TYPE_LLM_CALL)

    # 当tool_call生成失败时，会进入重新生成阶段，此时强制关闭tools生成
    no_tool_call: bool = False

    @property
    def real_result(self) -> LLMCallExecutionResult:
        return ensure_instance(self.result, LLMCallExecutionResult)
