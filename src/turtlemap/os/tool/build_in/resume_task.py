#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/17 17:55
# @Author  : YaHaoo
# @File    : resume_task.py

"""os 层内置任务恢复工具定义。"""

from __future__ import annotations

from itertools import chain
from typing import ClassVar

from pydantic import BaseModel, Field

from turtlemap.kernel.models.enums import EventSource, EventType, InterruptionRequestType, ToolResultStatus
from turtlemap.kernel.models.models import InterruptionAsyncToolResponse, InterruptionResponsePayload, ObservableEvent
from turtlemap.kernel.tool import ToolDescriptor, ToolMetadata, ToolResult
from turtlemap.kernel.tool.models import K_RAW_DATA_EVENT
from turtlemap.os.context.prompt import K_UNFINISHED_TASKS
from turtlemap.os.tool.enums import BuiltinToolCapabilityCategory
from turtlemap.os.tool.model import ToolInputContext
from turtlemap.shared.ids import PREFIX_INTERRUPTION_REQUEST_ID, PREFIX_OBSERVABLE_EVENT_ID, generate_prefixed_id
from turtlemap.shared.typing import ensure_instance

from .base import BuildinTool

K_TOOL_NAME_RESUME_TASK = "_resume_task"


class ResumeTaskInput(BaseModel):
    """表示恢复暂停任务时提交的标准参数。

    说明:
        `request_id` 用于定位唯一的中断请求；`params` 作为请求对应的
        结构化恢复数据，由后续 Runtime 中断响应链路按请求类型解释。
    """

    # 待恢复中断请求的唯一标识。
    request_id: str = Field(description="待恢复中断请求的唯一标识。")

    # 恢复请求携带的结构化参数。
    params: dict[str, object] = Field(
        default_factory=dict,
        description="恢复任务所需的结构化参数，由对应中断请求类型解释。",
    )


class ResumeTaskTool(BuildinTool[ResumeTaskInput]):
    """表示 os 层内置的暂停任务恢复工具。

    说明:
        该工具统一声明恢复任务需要的最小参数。实际任务恢复仍必须通过
        `INTERRUPTION_RESPONSE` 输入按顺序进入 Runtime，避免普通工具调用
        直接改写其他暂停任务的运行状态。
    """

    # 内置工具对模型暴露的稳定名称。
    TOOL_NAME: ClassVar[str] = K_TOOL_NAME_RESUME_TASK

    def __init__(self) -> None:
        """初始化系统级任务恢复工具。"""

        descriptor = ToolDescriptor(
            summary="恢复已中断的任务。",
            capability_category=BuiltinToolCapabilityCategory.RUNTIME.value,
            capability="恢复中断任务",
            use_cases=[
                "在当前语境下能明确推断用户需要恢复某任务",
                "用户明确拒绝恢复某任务，且定义了拒绝语义对应的参数时，也需要执行"
            ],
            anti_use_cases=[
                "用户没有正面表达需要恢复特定任务时，比如：'嗯'、'哦'、'?'、'知道了'、岔开话题等",
                "用户虽然表达肯定或者否定，但是语境与任务已不匹配，比如：用户在其他话题中表示肯定或否定",
                "无法生成对应语义下的工具参数时",
            ],
        )
        super().__init__(
            tool_metadata=ToolMetadata.model(
                name=self.TOOL_NAME,
                input_model=ResumeTaskInput,
            ),
            tool_descriptor=descriptor,
            input_model=ResumeTaskInput,
            call=self.call,
        )

    async def call(self, input_model: ResumeTaskInput) -> ToolResult:
        """构建暂停任务恢复所需的标准工具结果。

        参数:
            input_model: 已校验的恢复请求标识与结构化参数。

        返回:
            携带恢复参数的标准工具结果，供外层恢复入口转换为中断响应输入。
        """
        context = ensure_instance(ToolInputContext.get_input_context(input_model), ToolInputContext)
        
        from turtlemap.os.tool.service import ToolService

        tool_service = ensure_instance(context.tool_service, ToolService)
        tasks = chain(*[x.processing_tasks for x in tool_service.os_service.real_session_state.agent_name2agent_state.values()])
        task = next((t for t in tasks if t.interruption_request and t.interruption_request.request_id == input_model.request_id), None)
        if task is None:
            return ToolResult(
                        status=ToolResultStatus.FAILED,
                        content=f"任务恢复失败：未找到对应的暂停任务，request_id={input_model.request_id}，请检查request_id是否正确，以及是否缺少前缀 `{PREFIX_INTERRUPTION_REQUEST_ID}:`。",
                        purpose="resume a paused task through its interruption request",
                        raw_data={
                            "request_id": input_model.request_id,
                            "params": input_model.params,
                        },
                    )

        event = ObservableEvent(
                    event_id=generate_prefixed_id(PREFIX_OBSERVABLE_EVENT_ID),
                    event_type=EventType.INTERRUPTION_RESPONSE,
                    source=EventSource.TOOL,
                    payload=InterruptionResponsePayload(
                        request_id=input_model.request_id,
                        request_type=InterruptionRequestType.ASYNC_TOOL_REQUEST,
                        response=InterruptionAsyncToolResponse(data=input_model.params)
                    ),
                )
        return ToolResult(
            content=(
                f"已构建任务恢复请求：request_id={input_model.request_id}，"
                "Agent仅需简洁告知用户已恢复，不要扩展话题，运行结果将由系统自动返回，无需再调用任何工具。"
                ),
            purpose="resume a paused task through its interruption request",
            raw_data={
                "request_id": input_model.request_id,
                "params": input_model.params,
                K_RAW_DATA_EVENT: event
            },
        )

    @staticmethod
    def tool_call_prompt(params: str):
        return f"{K_TOOL_NAME_RESUME_TASK}(request_id: request_id, params: {params})"
