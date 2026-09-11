#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 13:41
# @Author  : YaHaoo
# @File    : models.py

"""kernel 层工具相关数据模型定义。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, SerializeAsAny, field_serializer, field_validator, model_validator

from turtlemap.shared.ids import PREFIX_EXCUTION_UNIT_ID, generate_prefixed_id
from turtlemap.shared.json_schema import pydantic_model_to_json_schema

from ..models import (
    ExecutionUnitStatus,
    TaskSwitchAction,
    ToolExecutionStrategy,
    ToolResultStatus,
)
from ..models import BaseStateModel, PolymorphicStateModel

if TYPE_CHECKING:
    from ..agent import BaseAgent
    from ..models import BaseAgentState, BaseSessionState, BaseProcessingTask

JsonDict = dict[str, Any]
# kernel 内建保留的执行单元类型名。
RESERVED_UNIT_TYPE_TOOL_CALL = "tool_call"
RESERVED_UNIT_TYPE_LLM_CALL = "llm_call"
# 简单式工具装配后挂载在函数对象上的元信息属性名。
DECORATED_TOOL_METADATA_ATTR = "__tool_metadata__"
DECORATED_TOOL_DESCRIPTOR_ATTR = "__tool_descriptor__"
DECORATED_TOOL_INPUT_MODEL_ATTR = "__tool_input_model__"


class ToolFactSourceType(str, Enum):
    """表示工具结果承载事实的来源类型。

    说明:
        该枚举只描述工具结果的补充事实类型。对外开放工具默认视为外部实时
        数据；`RECOLLECTION` 保留给内置 memory 回忆服务使用。
    """

    # 对外开放工具默认产生的外部实时或准实时数据。
    EXTERNAL_REALTIME_DATA = "external_realtime_data"

    # 内置 memory recollection 服务返回的历史回忆结果。
    RECOLLECTION = "recollection"


class ToolCallFunction(BaseStateModel):
    """表示 assistant 工具调用中的函数信息。"""

    # 工具调用参数 JSON 字符串。
    arguments: str | None = None

    # 被调用的工具名称。
    name: str | None = None


class ToolCall(BaseStateModel):
    """表示 assistant 发起的一次工具调用。

    说明:
        字段形态与 LLM 完整返回中的 tool call 保持一致，便于 os 层在
        LLMMessage、工具执行单元和持久化记录之间直接传递。
    """

    # 当前工具调用的唯一标识。
    id: str | None = None

    # 当前工具调用携带的函数调用信息。
    function: ToolCallFunction | None = None

    # 工具调用类型，当前仅支持 function。
    type: Literal["function"] | None = None


class ToolDescriptor(BaseStateModel):
    """表示创建侧的结构化工具描述。"""

    # 工具名称。
    name: str

    # 工具能力说明，描述该工具能提供什么确定性能力。
    capability: str

    # 推荐使用该工具的场景列表。
    use_cases: list[str] = Field(default_factory=list)

    # 不应使用该工具的场景列表。
    anti_use_cases: list[str] = Field(default_factory=list)

    # 帮助模型理解调用方式的示例文本列表。
    examples: list[str] = Field(default_factory=list)

    # 供上层系统使用的标签。
    tags: list[str] = Field(default_factory=list)


class ToolMetadata(BaseStateModel):
    """表示 Runtime 与模型可消费的工具元信息。"""

    # 工具稳定标识。
    id: str

    # 工具对模型暴露的名称。
    name: str

    # 传给模型的输入 JSON Schema。
    input_schema: JsonDict

    # 工具执行策略。
    execution_strategy: ToolExecutionStrategy = ToolExecutionStrategy.SYNC

    # 工具兼容版本。
    version: str | None = None

    # 工具所属命名空间。
    namespace: str | None = None

    # 是否必须在工具执行前获得用户确认。
    requires_confirmation: bool = False

    @classmethod
    def model(
        cls,
        *,
        name: str,
        input_model: type[BaseModel],
        tool_id: str | None = None,
        execution_strategy: ToolExecutionStrategy = ToolExecutionStrategy.SYNC,
        version: str | None = None,
        namespace: str | None = None,
        requires_confirmation: bool = False,
    ) -> "ToolMetadata":
        """根据工具名称和输入模型构建工具元信息。

        参数:
            name: 工具对模型暴露的名称。
            input_model: 工具输入模型；无参工具应传入 `EmptyToolInputModel`。
            tool_id: 可选工具稳定 id；未提供时使用 `name`。
            execution_strategy: 工具执行策略，默认等待工具同步完成。
            version: 可选工具版本。
            namespace: 可选工具命名空间。
            requires_confirmation: 是否必须在工具执行前获得用户确认。

        返回:
            可被 Runtime、ToolService 和模型适配层消费的工具元信息。
        """

        normalized_tool_id = tool_id or name
        assert normalized_tool_id
        return cls(
            id=normalized_tool_id,
            name=name,
            input_schema=pydantic_model_to_json_schema(input_model),
            execution_strategy=execution_strategy,
            version=version,
            namespace=namespace,
            requires_confirmation=requires_confirmation,
        )


class EmptyToolInputModel(BaseModel):
    """表示无需参数的工具输入模型。

    说明:
        当工具不需要任何结构化参数时，Runtime 会使用该类型生成空输入模型，
        便于 os 层仍然可以统一绑定 `ToolInputContext`。
    """

    ...


class ValidateExecuteResult(BaseStateModel):
    """表示工具执行前校验结果。"""

    # 当前调用是否允许继续执行。
    allowed: bool

    # 不允许执行时的原因说明。
    reason: str | None = None


class ToolResult(BaseStateModel):
    """表示工具函数调用形成的稳定结果。"""

    # 工具执行结果状态。
    status: ToolResultStatus = ToolResultStatus.SUCCESS

    # 提供给后续流程或模型消费的主结果文本。
    content: str = ""

    # 当前结果中事实内容的来源类型。
    fact_source_type: ToolFactSourceType = ToolFactSourceType.EXTERNAL_REALTIME_DATA

    # 数据用途说明。
    purpose: str = ""

    # 当前工具事实的过期时间；默认 5 分钟后过期，避免外部实时数据被长期复用。
    expires_at: datetime | None = Field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(minutes=5)
    )

    # 程序侧透传的原始结构化数据。
    raw_data: JsonDict | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_error_field(cls, value: object) -> object:
        """兼容旧版本 ToolResult.error 字段。

        参数:
            value: pydantic 传入的原始待校验数据。

        返回:
            已移除旧 `error` 字段的待校验数据；当旧数据缺少 content 时，
            会把 error 文本迁移到 content。
        """

        if not isinstance(value, dict) or "error" not in value:
            return value

        migrated_value: dict[str, Any] = dict(value)
        legacy_error = migrated_value.pop("error")

        # 旧 checkpoint 可能只在 error 中保存失败原因，恢复时迁移到模型可见 content。
        if not migrated_value.get("content") and legacy_error:
            migrated_value["content"] = str(legacy_error)
        return migrated_value


# 兼容早期执行结果命名，当前工具函数稳定结果统一使用 ToolResult 表达。
ExecutionResult = ToolResult


class ExecutionUnitResult(PolymorphicStateModel):
    """表示执行单元的统一结果基类。

    说明:
        `type_name` 是多态持久化恢复使用的稳定标识；运行时代码仍可通过
        `unit_type` 属性读取同一语义，避免执行链路出现大面积改名。
    """

    # 当前结果对应的执行单元类型。
    type_name: str

    # 当前执行结果提交后，unit 应切换到的下一状态。
    next_unit_status: ExecutionUnitStatus

    # 当前结果要求追加到执行链末尾的新执行单元列表。
    appended_execution_units: list["ExecutionUnit"] = Field(default_factory=list)

    # 当前结果要求 Runtime 执行的任务级切换动作。
    task_switch_action: TaskSwitchAction | None = None

    # 执行结果的原始结构化数据。
    raw_data: JsonDict | None = None

    # 执行失败时的错误信息。
    error: str | None = None

    @property
    def unit_type(self) -> str:
        """返回兼容运行期语义的执行单元类型名。

        返回:
            当前结果对应的执行单元类型名。
        """

        return self.type_name

    @field_validator("appended_execution_units", mode="before")
    @classmethod
    def _validate_appended_execution_units(cls, value: object) -> object:
        """恢复执行结果中追加的多态执行单元列表。

        参数:
            value: pydantic 校验前的原始执行单元列表。

        返回:
            已按 `type_name` 恢复的执行单元列表，或原值。
        """

        if isinstance(value, list):
            return [
                ExecutionUnit.validate_polymorphic(item)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        return value

    @field_serializer("appended_execution_units")
    def _serialize_appended_execution_units(self, value: list["ExecutionUnit"]) -> list[object]:
        """序列化执行结果中追加的多态执行单元列表。

        参数:
            value: 当前执行结果持有的追加执行单元列表。

        返回:
            可 JSON 化的执行单元字典列表。
        """

        return [
            item.model_dump(mode="json") if isinstance(item, ExecutionUnit) else item
            for item in value
        ]


class ExecutionUnit(PolymorphicStateModel):
    """表示可恢复执行单元基类。

    说明:
        `type_name` 是多态持久化恢复使用的稳定标识；运行时代码仍可通过
        `unit_type` 属性读取同一语义，避免 ToolService 分发逻辑大改。
    """

    # 执行单元 id。
    unit_id: str = Field(default_factory=lambda: generate_prefixed_id(PREFIX_EXCUTION_UNIT_ID))

    # 执行单元状态。
    status: ExecutionUnitStatus = ExecutionUnitStatus.PENDING

    # 执行单元执行结果。
    result: SerializeAsAny[ExecutionUnitResult] | None = None

    @property
    def unit_type(self) -> str:
        """返回兼容运行期语义的执行单元类型名。

        返回:
            当前执行单元类型名。
        """

        return self.type_name

    @property
    def is_finished(self) -> bool:
        """判断当前执行单元是否已经进入终态。

        返回:
            当执行单元状态为完成或失败时返回 `True`，其他状态返回 `False`。
        """

        return self.status in (
            ExecutionUnitStatus.COMPLETED,
            ExecutionUnitStatus.FAILED,
        )

    @field_validator("result", mode="before")
    @classmethod
    def _validate_result(cls, value: object) -> object:
        """恢复执行单元持有的多态执行结果。

        参数:
            value: pydantic 校验前的原始执行结果。

        返回:
            已按 `type_name` 恢复的执行结果，或原值。
        """

        if value is None or isinstance(value, ExecutionUnitResult):
            return value
        if isinstance(value, dict):
            return ExecutionUnitResult.validate_polymorphic(value)
        return value


@dataclass(slots=True)
class ToolExecutionContext:
    """表示执行单元运行时所需的上下文依赖。

    说明:
        `Runtime` 负责把当前会话状态、任务现场和外部依赖整理后传入；
        真正如何解释不同 `ExecutionUnit`，由 `ToolService` 在 `os`
        层统一实现，避免 `Runtime` 再按类型分支。
    """

    # 当前执行单元所属的 Agent。
    agent: BaseAgent

    # 当前 Owner Agent 对应状态。
    owner_state: BaseAgentState

    # 当前会话状态。
    session_state: BaseSessionState

    # 当前待推进的任务现场。
    task: BaseProcessingTask
