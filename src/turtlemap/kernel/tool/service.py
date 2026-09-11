#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 13:41
# @Author  : YaHaoo
# @File    : service.py

"""kernel 层工具运行时对象与服务定义。"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Generic, TypeAlias, TypeVar, cast

from pydantic import BaseModel

from turtlemap.shared.typing import ensure_instance

from ..exceptions import (
    KernelRuntimeError,
    KernelToolConfigurationError,
    KernelToolLookupError,
    KernelUnsupportedOperationError,
)
from .models import (
    DECORATED_TOOL_DESCRIPTOR_ATTR,
    DECORATED_TOOL_INPUT_MODEL_ATTR,
    DECORATED_TOOL_METADATA_ATTR,
    ExecutionUnit,
    ExecutionUnitResult,
    JsonDict,
    ToolDescriptor,
    ToolExecutionContext,
    ToolMetadata,
    ToolResult,
    ValidateExecuteResult,
)

from .decorator import ToolCallbackResult

if TYPE_CHECKING:
    from ..agent import BaseAgent
    from ..models import BaseSessionState, Input, RuntimeArtifact, BaseProcessingTask

InputModelT = TypeVar("InputModelT", bound=BaseModel)


class ExecutableTool(Generic[InputModelT]):
    """表示可被 Runtime 直接调用的工具对象。"""

    __slots__ = (
        "tool_metadata",
        "tool_descriptor",
        "input_model",
        "call",
        "before_execute_callback",
        "validate_execute_hook",
        "after_execute_callback",
    )

    def __init__(
        self,
        tool_metadata: ToolMetadata,
        tool_descriptor: ToolDescriptor | None,
        input_model: type[InputModelT],
        call: Callable[[InputModelT], ToolCallbackResult],
        before_execute_callback: Callable[[InputModelT], None] | None = None,
        validate_execute_hook: Callable[[InputModelT], ValidateExecuteResult] | None = None,
        after_execute_callback: Callable[[InputModelT, ToolResult], None] | None = None,
    ) -> None:
        """初始化可被 Runtime 直接调用的工具对象。

        参数:
            tool_metadata: 当前工具的标准元信息。
            tool_descriptor: 当前工具创建侧的结构化描述对象；为空时表示只有运行时元信息。
            input_model: 当前工具输入模型，负责把字典参数反序列化为稳定模型对象。
            call: 工具主执行逻辑，负责接收已反序列化的输入模型并返回执行结果。
            before_execute_callback: 工具执行前生命周期回调；不负责决定是否允许执行。
            validate_execute_hook: 工具执行前校验逻辑；未提供时默认放行。
            after_execute_callback: 工具执行后生命周期回调；可用于收尾、观测或补充埋点。
        """

        # 工具的标准元信息。
        self.tool_metadata = tool_metadata

        # 工具创建侧的结构化描述对象。
        self.tool_descriptor = tool_descriptor

        # 工具输入模型类型，用于在运行边界反序列化字典参数。
        self.input_model = input_model

        # 工具主执行逻辑，有参工具入参必须是已反序列化的模型对象。
        self.call = call

        # 工具执行前生命周期回调。
        self.before_execute_callback = before_execute_callback

        # 工具执行前校验逻辑。
        self.validate_execute_hook = validate_execute_hook

        # 工具执行后生命周期回调。
        self.after_execute_callback = after_execute_callback

    async def __call__(self, input_model: InputModelT) -> ToolResult:
        """异步执行工具主逻辑。

        参数:
            input_model: 当前工具调用已完成校验和上下文绑定的输入模型。

        返回:
            当前工具执行后的标准 `ToolResult`。
        """

        result = self.call(input_model)

        # 工具主逻辑允许同步返回，也允许返回协程或其他可等待结果。
        if inspect.isawaitable(result):
            result = await cast(Awaitable[ToolResult | str], result)

        return self._normalize_tool_result(result)

    def before_execute(self, input_model: InputModelT) -> None:
        """执行工具执行前生命周期回调。

        参数:
            input_model: 当前工具调用已完成校验和上下文绑定的输入模型。

        说明:
            当前方法只承载生命周期语义，不负责决定工具是否允许执行；
            需要控制执行时，应使用 `validate_execute(...)`。
        """

        if self.before_execute_callback:
            self.before_execute_callback(input_model)
        self.overrideable_before_execute(input_model)

    def validate_execute(self, input_model: InputModelT) -> ValidateExecuteResult:
        """执行工具执行前校验。

        参数:
            input_model: 当前工具调用已完成校验和上下文绑定的输入模型。

        返回:
            当前调用对应的执行前校验结果。

        说明:
            当前方法只负责同步、确定性的本地校验；
            需要用户参与的前置确认由 os 层在工具执行单元内处理。
        """
        result = self.overrideable_validate_execute(input_model)
        if result is None:
            if self.validate_execute_hook:
                result = self.validate_execute_hook(input_model)
        if result is None:
            result = ValidateExecuteResult(allowed=True)
        return result

    async def after_execute(
        self,
        input_model: InputModelT,
        tool_result: ToolResult,
    ) -> None:
        """执行工具执行后生命周期回调。

        参数:
            input_model: 当前工具调用已完成校验和上下文绑定的输入模型。
            tool_result: 当前工具执行得到的标准结果对象。

        说明:
            当前方法只承载执行后的生命周期语义，不负责改写主执行结果。
        """

        if self.after_execute_callback is None:
            return
        
        self.after_execute_callback(input_model, tool_result)
        await self.overrideable_after_execute(input_model, tool_result)

    def validate_tool_input(self, tool_input: JsonDict) -> InputModelT:
        """把字典工具参数校验并反序列化为当前工具输入模型。

        参数:
            tool_input: 当前工具调用的结构化字典参数。

        返回:
            经 `input_model` 校验后的稳定输入模型对象。
        """
        return self.input_model.model_validate(tool_input)

    async def overrideable_start_request(self, input_model: InputModelT):
            ...
    
    async def override_handle_response(self, raw_data: JsonDict) -> ToolResult:
        raise NotImplemented("Async Tool 必须实现此方法")

    def overrideable_before_execute(self, input_model: InputModelT) -> None:
        ...

    def overrideable_validate_execute(self, input_model: InputModelT) -> ValidateExecuteResult | None:
        return None

    async def overrideable_after_execute(
        self,
        input_model: InputModelT,
        tool_result: ToolResult,
    ) -> None:
        ...
        

    def _normalize_tool_result(self, result: ToolResult | str) -> ToolResult:
        """把工具 callback 返回值收敛为标准 `ToolResult`。

        参数:
            result: 工具 callback 的原始返回值。

        返回:
            标准化后的 `ToolResult`。
        """

        if isinstance(result, ToolResult):
            return result

        if isinstance(result, str):
            return ToolResult(content=result)

        raise KernelToolConfigurationError(
            "工具返回值不合法："
            f"tool_name={self.tool_metadata.name}，"
            "仅支持返回 ToolResult 或 str"
        )


class BaseToolService:
    """表示 Agent 维度工具服务基类。"""

    __slots__ = ("tools",)

    def __init__(self, tools: dict[str, ExecutableTool] | None = None) -> None:
        """初始化 Agent 维度工具服务。

        参数:
            tools: 当前 Agent 初始可管理的工具映射；键为 `ToolMetadata.id`。
        """

        # 当前 Agent 可管理的全部工具，键为 `tool_metadata.id`。
        self.tools = dict(tools or {})

    @classmethod
    def from_tools(cls, tools: list[ExecutableTool | Callable[..., Any]]) -> "BaseToolService":
        """根据工具列表构建标准 `BaseToolService`。

        参数:
            tools: 业务侧传入的工具列表，允许混合 `ExecutableTool` 实例
                与已装饰的普通可调用对象。

        返回:
            已完成标准化装配的工具服务对象。

        说明:
            当前方法会先把输入工具统一归一化，再按 `tool_metadata.id`
            建立唯一映射；若出现重复 id，会直接抛出工具配置异常。
        """

        tool_id2tool: dict[str, ExecutableTool] = {}
        for tool in tools:
            executable_tool = cls._normalize_tool(tool)
            if executable_tool.tool_metadata.id in tool_id2tool:
                raise KernelToolConfigurationError(
                    "构建 BaseToolService 时发现重复的工具 id，"
                    f"tool_id={executable_tool.tool_metadata.id}"
                )
            tool_id2tool[executable_tool.tool_metadata.id] = executable_tool
        return cls(tools=tool_id2tool)

    def list_tools(self) -> list[ExecutableTool]:
        """返回当前 Agent 可管理的全部工具对象。

        返回:
            当前工具服务中维护的全部 `ExecutableTool` 列表。
        """

        return list(self.tools.values())

    def register_tool(self, tool: ExecutableTool):
        if tool.tool_metadata.id in self.tools:
            raise KernelRuntimeError(f"添加tools时 id 重复 {tool.tool_metadata.id}")

        self.tools[tool.tool_metadata.id] = tool

    async def get_tools_with_inputs(
        self,
        input: Input,
        agent: BaseAgent,
        session_state: BaseSessionState,
    ) -> list[ExecutableTool]:
        """根据当前输入上下文返回本轮可用工具列表。

        参数:
            input: 当前已进入 Runtime 的输入包。
            agent: 当前拥有这些工具的 Agent。
            session_state: 当前会话状态对象。

        返回:
            当前轮允许暴露给模型或执行链路的工具列表。

        说明:
            基类默认直接返回全部工具，不做输入态、会话态或 Agent 级裁剪；
            更细粒度的工具过滤策略应由 `os` 层子类覆盖实现。
        """

        _ = (input, agent, session_state)
        return self.list_tools()

    def get_tool_by_id(self, tool_id: str) -> ExecutableTool | None:
        """根据工具名称查找工具对象。

        参数:
            tool_name: 待查找的工具名称。

        返回:
            名称匹配的 `ExecutableTool` 对象。

        说明:
            若当前工具服务中不存在对应名称的工具，则抛出工具查找异常。
        """
        return self.tools.get(tool_id, None)

    async def build_tool_execution_units(
        self,
        agent: BaseAgent,
        task: BaseProcessingTask,
        message: RuntimeArtifact,
    ) -> list[ExecutionUnit]:
        """根据模型响应和任务现场构建执行单元列表。

        参数:
            agent: 当前拥有这些工具的 Agent。
            task: 当前正在推进的任务状态对象。
            message: 当前 assistant 生成的 Runtime 产物。

        返回:
            当前轮应挂入 `ToolState.execution_units` 的执行单元列表。

        说明:
            该列表通常既包含真正的工具调用单元，也包含 tool loop
            收尾所需的后续单元，例如继续发起一次 LLM continuation 的
            `llm_call` 单元。`ExecutionUnit` 的具体构造策略属于 `os`
            层职责；`kernel` 只定义统一入口与返回结构，因此基类默认
            不提供实现。当前基类若被直接调用，会抛出能力边界异常。
        """

        _ = (agent, task, message)
        raise KernelUnsupportedOperationError(
            "当前 BaseToolService 未提供执行单元构造实现，请在 os 层子类中覆盖该方法"
        )

    async def execute_unit(
        self,
        context: ToolExecutionContext,
        execution_unit: ExecutionUnit,
    ) -> ExecutionUnitResult:
        """执行一个执行单元并返回统一结果。

        参数:
            context: 当前执行单元所需的运行时上下文。
            execution_unit: 当前待执行的执行单元。

        返回:
            当前执行单元的统一执行结果。

        说明:
            `Runtime` 不应再根据 `unit_type` 做执行分发；
            具体解释与执行策略由 `os` 层工具服务统一决定。
            当前基类若被直接调用，会抛出能力边界异常。
        """

        _ = (context, execution_unit)
        raise KernelUnsupportedOperationError(
            "当前 BaseToolService 未提供执行单元执行实现，请在 os 层子类中覆盖该方法"
        )

    @staticmethod
    def _normalize_tool(tool: ExecutableTool | Callable[..., Any]) -> ExecutableTool:
        """将输入工具归一化成 `ExecutableTool`。

        参数:
            tool: 业务侧传入的工具对象，允许是 `ExecutableTool`
                实例或已完成装饰的可调用对象。

        返回:
            可被 Runtime 直接消费的标准 `ExecutableTool` 对象。

        说明:
            若传入对象不满足工具装配约束，当前方法会抛出工具配置异常。
        """

        if isinstance(tool, ExecutableTool):
            return tool

        tool_metadata = getattr(tool, DECORATED_TOOL_METADATA_ATTR, None)
        if tool_metadata is None:
            raise KernelToolConfigurationError(
                "工具归一化失败：传入对象既不是 ExecutableTool，也不是已装饰工具函数"
            )

        if not callable(tool):
            raise KernelToolConfigurationError("工具归一化失败：已装饰工具对象不可调用")

        input_model = getattr(tool, DECORATED_TOOL_INPUT_MODEL_ATTR, None)
        return ExecutableTool(
            tool_metadata=tool_metadata,
            tool_descriptor=getattr(tool, DECORATED_TOOL_DESCRIPTOR_ATTR, None),
            input_model=cast(Any, input_model),
            call=tool,
        )
