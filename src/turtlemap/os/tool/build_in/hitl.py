#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved 
#
# @Time    : 2026/09/10 11:23
# @Author  : YaHaoo
# @File    : hitl.py

from enum import Enum
from typing import Any, ClassVar
from typing_extensions import override

from pydantic import BaseModel, Field

from turtlemap.kernel.models.enums import ToolExecutionStrategy, ToolResultStatus
from turtlemap.kernel.tool.models import JsonDict, ToolDescriptor, ToolMetadata, ToolResult
from turtlemap.os.tool.build_in.base import BuildinTool
from turtlemap.shared.json_parser import dump_to_static_json
from turtlemap.shared.logger import Logger


K_TOOL_NAME_HITL = "user_approval"
K_OPTION_KEY = "option"
K_HITL_TOOL_ID_PREFIX = "_hitl_"


class HitlOptionType(str, Enum):
    """表示 HitlTool 支持的用户审核选项。"""

    # 用户同意继续执行关联的业务工具。
    APPROVE = "approve"

    # 用户拒绝执行关联的业务工具。
    REJECT = "reject"


class HitlInputModel(BaseModel):

    source_tool_id: str = Field(description="待审批工具id")

    # 用于回忆历史信息的独立查询陈述句。
    options: list[HitlOptionType] = Field(
        default=[HitlOptionType.APPROVE, HitlOptionType.REJECT],
        description=(
            "用户可选择的选项"
        )
    )


class HitlTool(BuildinTool[HitlInputModel]):
    """表示在业务工具执行前收集用户审核结果的异步内置工具。

    说明:
        HitlTool 复用通用异步工具 request/response 链路，外部响应会被转换为
        标准 `ToolResult`，供后续业务工具决定继续执行或终止。
    """

    # 内置工具对模型暴露的稳定名称。
    TOOL_NAME: ClassVar[str] = K_TOOL_NAME_HITL

    def __init__(self, source_tool_id: str) -> None:
        """初始化绑定指定业务工具的审核工具。

        参数:
            source_tool_id: 需要在审核通过后继续执行的业务工具 id。

        返回:
            无返回值。
        """

        descriptor = ToolDescriptor(
            name=self.TOOL_NAME,
            capability="HITL",
        )
        super().__init__(
            tool_metadata=ToolMetadata.model(
                name=descriptor.name,
                tool_id=HitlTool.hitl_tool_id(source_tool_id),
                execution_strategy=ToolExecutionStrategy.ASYNC,
                input_model=HitlInputModel,
            ),
            tool_descriptor=descriptor,
            input_model=HitlInputModel,
            call=self._unsupported_sync_call,
        )
        self.source_tool_id = source_tool_id

    @staticmethod
    def hitl_tool_id(source_tool_id: str) -> str:
        return f"{K_HITL_TOOL_ID_PREFIX}{source_tool_id}"

    @staticmethod
    def is_hitl_tool(hitl_tool_id: str) -> bool:
        return hitl_tool_id.startswith(K_HITL_TOOL_ID_PREFIX)

    @staticmethod
    def _unsupported_sync_call(_: HitlInputModel) -> ToolResult:
        """阻止 HitlTool 被错误地按同步工具直接执行。

        参数:
            _: 当前审核工具输入；正常异步流程不会消费该参数。

        返回:
            无返回值。

        异常:
            RuntimeError: 当前工具未通过异步 request/response 链路执行时抛出。
        """

        raise RuntimeError("HitlTool 只能通过异步 request/response 链路执行")

    @override
    async def override_handle_response(self, raw_data: dict[str, Any]) -> ToolResult:
        option_str = raw_data.get(K_OPTION_KEY)
        approve_content = f"用户批准了 {self.source_tool_id} 执行"
        reject_content = f"用户拒绝了 {self.source_tool_id} 执行"
        try:
            option = HitlOptionType(option_str)
            status = ToolResultStatus.SUCCESS if option == HitlOptionType.APPROVE else ToolResultStatus.FAILED
            reason = approve_content if option == HitlOptionType.APPROVE else reject_content
        except:
            status = ToolResultStatus.FAILED
            reason = reject_content
            Logger.logger.warning(
                f"HITL 审批结果数据异常, tool_name: {self.tool_metadata.name}, raw_data：{raw_data}", 
                exc_info=True
            )
        return ToolResult(
                    status=status,
                    content=reason,
                    raw_data=raw_data
                )
        
    @staticmethod
    def response_data(option: HitlOptionType) -> dict:
        return {
            K_OPTION_KEY: option
        }

    @staticmethod
    def request_params_str(source_tool_id: str) -> str:
        return dump_to_static_json(
            HitlInputModel(source_tool_id=source_tool_id)
        )
