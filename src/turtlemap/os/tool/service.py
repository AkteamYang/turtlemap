#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/08 15:56
# @Author  : YaHaoo
# @File    : service.py

"""turtlemap os 层工具服务实现。"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Tuple

from pydantic import BaseModel
from typing_extensions import override

from turtlemap.kernel.agent import BaseAgent
from turtlemap.kernel.models import (
    ExecutionUnitStatus,
    InterruptionAsyncToolResponse,
    InterruptionRequest,
    InterruptionRequestType,
    TaskSwitchAction,
    ToolExecutionStrategy,
    ToolResultStatus,
)
from turtlemap.kernel.exceptions import (
    KernelToolConfigurationError,
    KernelRuntimeError,
    KernelToolLookupError,
    KernelUnsupportedOperationError,
)
from turtlemap.kernel.models import BaseProcessingTask, ToolState
from turtlemap.kernel.models.models import BaseSessionState, Input, RuntimeArtifact, RuntimeArtifactType
from turtlemap.kernel.tool.models import (
    RESERVED_UNIT_TYPE_LLM_CALL,
    RESERVED_UNIT_TYPE_TOOL_CALL,
)
from turtlemap.os.event_bus.event_bus import EventBus
from turtlemap.os.event_bus.models import (
    InterruptedEvent,
    RuntimeEventPhase,
    ToolCallEvent,
    ToolResultEvent,
)
from turtlemap.os.exceptions import OSRuntimeError
from turtlemap.os.llm.model import LLMCompletionToolCall, LLMFunction, LLMMessage
from turtlemap.kernel.tool import (
    BaseToolService,
    EmptyToolInputModel,
    ExecutionUnitResult,
    ExecutionUnit,
    ExecutableTool,
    JsonDict,
    ToolResult,
    ToolExecutionContext,
)
from turtlemap.os.models import ProcessingTask
from turtlemap.os.tool.build_in.hitl import K_HITL_TOOL_ID_PREFIX, HitlTool
from turtlemap.os.tool.enums import AsyncToolType
from turtlemap.shared.ids import (
    PREFIX_EVENT_ID,
    PREFIX_INTERRUPTION_REQUEST_ID,
    PREFIX_TOOL_CALL_ID,
    PREFIX_TOOL_ID,
    generate_prefixed_id,
)
from turtlemap.shared.json_parser import sanitize_json_string
from turtlemap.shared.logger import Logger
from turtlemap.shared.typing import ensure_instance

from .model import ToolInputContext
from .tool import (
    LLMCallExecutionUnit,
    LLMCallExecutionResult,
    ToolCallExecutionResult,
    ToolCallExecutionUnit,
)

if TYPE_CHECKING:
    from turtlemap.os.service import OSService

K_ASYNC_TOOL_NAME = "name"
K_ASYNC_TOOL_ID = "tool_call_id"
K_ASYNC_TOOL_PARAMETERS = "parameters"


class _ToolCallExecutionFailure(Exception):
    """表示工具调用单元执行过程中可反馈给 LLM 的失败。

    说明:
        该异常只在当前模块内部使用，用于把各执行阶段的失败原因统一带回
        `_execute_tool_call_unit`，再转换为标准 `ToolCallExecutionResult`。
    """

    def __init__(self, reason: str, raw_data: JsonDict | None = None) -> None:
        """初始化工具调用失败异常。

        参数:
            reason: 面向 LLM 与开发者可读的失败原因。
            raw_data: 附加排障信息。

        返回:
            无返回值。
        """

        super().__init__(reason)
        self.reason = reason
        self.raw_data = raw_data or {}

    def __str__(self) -> str:
        return self.reason


class ToolService(BaseToolService):
    """os 层默认工具管理与执行单元构造实现。"""

    _os_service: Optional["OSService"] = None

    @property
    def os_service(self) -> "OSService":
        from turtlemap.os.service import OSService

        return ensure_instance(self._os_service, OSService)

    @override
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
        """

        match execution_unit.unit_type:
            case value if value == RESERVED_UNIT_TYPE_TOOL_CALL:
                tool_call_execution_unit = ensure_instance(
                    execution_unit,
                    ToolCallExecutionUnit,
                    "工具调用执行单元",
                )
                return await self._execute_tool_call_unit(context, tool_call_execution_unit)
            case value if value == RESERVED_UNIT_TYPE_LLM_CALL:
                llm_call_execution_unit = ensure_instance(
                    execution_unit,
                    LLMCallExecutionUnit,
                    "LLM 调用执行单元",
                )
                return await self._execute_llm_call_unit(context, llm_call_execution_unit)
            case _:
                raise KernelUnsupportedOperationError(
                    f"当前 ToolService 暂不支持该执行单元类型：{execution_unit.unit_type}"
                )

    @override
    async def build_tool_execution_units(
        self,
        agent: BaseAgent,
        task: BaseProcessingTask,
        message: RuntimeArtifact,
    ) -> list[ExecutionUnit]:
        """根据 assistant 工具调用产物构建工具执行单元。

        参数:
            agent: 当前拥有控制权的 Agent。
            task: 当前正在推进的任务状态对象。
            message: 当前已完成的 assistant Runtime 产物。

        返回:
            可交给 kernel ToolState 推进的执行单元列表。

        说明:
            kernel 不读取 `RuntimeArtifact.payload`；这里在 os 边界将 payload
            收窄为 `LLMMessage`，并按 LLM tool_calls 构建真实工具调用单元。
            对缺少必要字段或当前不可用的 tool_call，也在这里直接过滤并回写
            assistant message，保证 kernel 保存的是已经过 os 层校验的产物。
        """

        assistant_message = ensure_instance(
            message.payload,
            LLMMessage,
            "assistant 工具调用消息",
        )
        execution_units: list[ExecutionUnit] = []
        valid_tool_calls: list[LLMCompletionToolCall] = []
        for tool_call in assistant_message.tool_calls:
            tool_call_id = tool_call.id
            function = tool_call.function
            if tool_call_id is None or function is None or function.name is None:
                Logger.logger.warning(f"assistant 工具调用消息缺少必要的 id 或 function.name, agent_name {agent.agent_name}, input {task.start_input}, tool_call {tool_call}", exc_info=True)
                continue

            executable_tool = agent.tool_provider.get_tool_by_id(function.name)
            if not executable_tool:
                Logger.logger.warning(f"assistant 工具调用无效的工具名: {function.name}, agent_name {agent.agent_name}, input {task.start_input}, tool_call {tool_call}", exc_info=True)
                continue

            # 增加前置 hitl 节点
            if executable_tool.tool_metadata.requires_confirmation:
                hitl_tool_id = HitlTool.hitl_tool_id(executable_tool.tool_metadata.id)
                hitl_execution_unit = ToolCallExecutionUnit(
                    tool_meta_id=hitl_tool_id,
                    tool_call=LLMCompletionToolCall(
                        id=generate_prefixed_id(PREFIX_TOOL_CALL_ID),
                        function=LLMFunction(
                            name=hitl_tool_id,
                            arguments=HitlTool.request_params_str(source_tool_id=executable_tool.tool_metadata.id)
                        )
                    )
                )
                execution_units.append(hitl_execution_unit)
            tool_execution_unit = ToolCallExecutionUnit(
                tool_meta_id=executable_tool.tool_metadata.id,
                tool_call=tool_call,
            )
            execution_units.append(tool_execution_unit)
            valid_tool_calls.append(tool_call)

            # 发送 tool call 事件
            await ProcessingTask.publish(
                task,
                self.os_service.event_bus_id,
                event=ToolCallEvent(
                    event_id=generate_prefixed_id(PREFIX_EVENT_ID),
                    session_id=self.os_service.real_session_state.session_id,
                    agent_name=agent.agent_name,
                    task_id=task.task_id,
                    event_phase=RuntimeEventPhase.FINAL,
                    tool_call=tool_call,
                )
            )
        if execution_units:

            # 工具执行结束后补一轮 continuation LLM 调用，保持 tool loop 统一闭合。
            execution_units.append(
                LLMCallExecutionUnit()
            )

        # kernel 不理解 LLMMessage 细节，有效 tool_calls 的裁剪必须在 os 边界完成。
        assistant_message.tool_calls = valid_tool_calls
        return execution_units

    @override
    async def get_tools_with_inputs(
        self,
        input: Input,
        agent: BaseAgent,
        session_state: BaseSessionState,
    ) -> list[ExecutableTool]:
        tools = await super().get_tools_with_inputs(
            input=input,
            agent=agent,
            session_state=session_state
        )
        tools = [t for t in tools if not t.tool_metadata.id.startswith(K_HITL_TOOL_ID_PREFIX)]
        return tools

    async def _execute_tool_call_unit(
        self,
        context: ToolExecutionContext,
        execution_unit: ToolCallExecutionUnit,
    ) -> ExecutionUnitResult:
        """执行一个真实工具调用单元。

        参数:
            context: 当前执行单元所需的运行时上下文。
            execution_unit: 当前待执行的工具调用单元。

        返回:
            当前工具调用单元对应的标准执行结果。

        说明:
            当前流程先处理执行前确认，再依次执行参数校验、生命周期
            回调与真实工具调用；后台工具会在同一单元内等待外部结果。
        """
        start_ts_ms = int(time.time() * 1000)
        tool_call = execution_unit.tool_call
        tool_call_id = tool_call.id or ""
        tool_name = tool_call.function.name if tool_call.function else ""
        tool_id = execution_unit.tool_meta_id
        tool_metadata = {
            "tool_name": tool_name,
            "tool_id": tool_id,
        }
        event_id = generate_prefixed_id(PREFIX_EVENT_ID)

        async def publish_event(event_phase: RuntimeEventPhase, result: ToolResult | None = None,):
            if HitlTool.is_hitl_tool(execution_unit.tool_meta_id):
                return

            """发布当前工具调用对应的阶段事件。"""
            await ProcessingTask.publish(
                context.task,
                self.os_service.event_bus_id,
                event=ToolResultEvent(
                    event_id=event_id,
                    session_id=self.os_service.real_session_state.session_id,
                    agent_name=context.agent.agent_name,
                    task_id=context.task.task_id,
                    event_phase=event_phase,
                    tool_call=tool_call,
                    created_ts_ms=start_ts_ms if event_phase == RuntimeEventPhase.STARTED else -1,
                    result=result,
                ),
            )

        try:
            function = self._resolve_tool_call_function(
                execution_unit=execution_unit,
            )
            executable_tool = self._resolve_executable_tool(
                execution_unit=execution_unit,
                function=function,
            )
            self._resolve_confirmation(
                context=context,
                execution_unit=execution_unit,
                executable_tool=executable_tool
            )                
            tool_input = self._parse_tool_arguments(
                execution_unit=execution_unit,
                executable_tool=executable_tool,
                function=function,
            )
            input_model = self._validate_tool_input_model(
                execution_unit=execution_unit,
                executable_tool=executable_tool,
                function=function,
                tool_input=tool_input,
            )
            self._bind_tool_input_context(input_model)
            self._run_before_execute_hooks(
                execution_unit=execution_unit,
                executable_tool=executable_tool,
                function=function,
                input_model=input_model,
                tool_input=tool_input,
            )

            # Async Tool 调用
            tool_result: ToolResult
            if executable_tool.tool_metadata.execution_strategy == ToolExecutionStrategy.ASYNC:

                # 获取异步 tool 结果
                _t = await self._get_async_tool_response(
                            context=context,
                            execution_unit=execution_unit,
                            executable_tool=executable_tool,
                        )
                
                # 没有结果时，执行 Async 工具请求
                if _t is None:
                    result = await self._try_perform_async_tool_request(
                        context=context,
                        execution_unit=execution_unit,
                        executable_tool=executable_tool,
                        tool_call_id=tool_call_id,
                        input_model=input_model
                    )
                    result.tool_result.raw_data = {
                        **(result.tool_result.raw_data or {}),
                        **tool_metadata,
                    }
                    return result

                tool_result, request = _t
                start_ts_ms = request.created_ts_ms  # 更新开始时间到真实开始时间
                await publish_event(event_phase=RuntimeEventPhase.STARTED)

            # Sync Tool 调用
            else:
                await publish_event(event_phase=RuntimeEventPhase.STARTED)
                tool_result = await self._execute_sync_tool(
                    context=context,
                    execution_unit=execution_unit,
                    executable_tool=executable_tool,
                    input_model=input_model,
                    tool_call_id=tool_call_id,
                )

            # 工具执行完成前补齐元信息，确保 END 事件携带完整 ToolResult.raw_data。
            tool_result.raw_data = {
                **(tool_result.raw_data or {}),
                **tool_metadata,
            }
            await publish_event(event_phase=RuntimeEventPhase.END, result=tool_result)
            await executable_tool.after_execute(input_model, tool_result)
            next_unit_status = self._resolve_next_unit_status_from_tool_result(
                tool_result
            )
            return ToolCallExecutionResult(
                next_unit_status=next_unit_status,
                tool_result=tool_result,
                raw_data=tool_result.raw_data,
                error=tool_result.content if tool_result.status == ToolResultStatus.FAILED else None,
            )
        except _ToolCallExecutionFailure as exc:
            result = await self._build_failed_tool_call_execution_result(
                tool_call_id=tool_call_id,
                publish_event=publish_event,
                reason=exc.reason,
                raw_data={
                    **exc.raw_data,
                    **tool_metadata,
                },
            )
        return result

    async def _execute_llm_call_unit(
        self,
        context: ToolExecutionContext,
        execution_unit: LLMCallExecutionUnit,
    ) -> ExecutionUnitResult:
        """执行一个 LLM continuation 单元。

        参数:
            context: 当前执行单元所需的运行时上下文。
            execution_unit: 当前待执行的 LLM continuation 单元。

        返回:
            当前 LLM continuation 单元对应的标准执行结果。
        """

        _ = execution_unit

        if not isinstance(context.task.state, ToolState):
            raise KernelRuntimeError(
                "执行 LLM continuation 单元时，任务状态对象不是 ToolState，"
                f"task_id={context.task.task_id}"
            )

        if context.task.start_input is None:
            raise KernelRuntimeError(
                "工具循环继续发起 LLM 调用时缺少任务起点输入，"
                f"task_id={context.task.task_id}"
            )

        # tool loop continuation 由上下文构建器根据当前 task 展开已完成的工具执行产物。
        artifact = await self.os_service.generate_assistant_message_with_task(
            owner_agent=context.agent,
            task=context.task,
            no_tool_call=execution_unit.no_tool_call
        )
        appended_execution_units: list[ExecutionUnit] = []

        # 若 continuation 仍然产生 tool_calls，就继续展开新的执行单元链；
        # 若没有 tool_calls，则本次 llm_call 单元到此闭合。
        if artifact.type == RuntimeArtifactType.TOOL_CALL:
            appended_execution_units = await self.build_tool_execution_units(
                agent=context.agent,
                task=context.task,
                message=artifact,
            )

            # 当tool_call无效时，强制关闭tool，重新生成
            if not appended_execution_units:
                Logger.logger.warning(f"{type(execution_unit)} 生成tool_call失败，禁用tool，待重新生成")
                execution_unit.no_tool_call = True
                return LLMCallExecutionResult(
                    next_unit_status=ExecutionUnitStatus.PENDING,
                )

        return LLMCallExecutionResult(
            message=ensure_instance(artifact.payload, LLMMessage),
            next_unit_status=ExecutionUnitStatus.COMPLETED,
            appended_execution_units=appended_execution_units,
        )

    def _resolve_confirmation(
        self,
        context: ToolExecutionContext,
        execution_unit: ToolCallExecutionUnit,
        executable_tool: ExecutableTool
    ) -> None:
        """校验需审核工具前置 HitlTool 的执行结果。

        参数:
            context: 当前工具调用所属的任务执行上下文。
            execution_unit: 当前待执行的业务工具单元。
            executable_tool: 当前业务工具对象。

        返回:
            审核通过时无返回值。

        异常:
            OSRuntimeError: 缺失或错误配置前置 HitlTool 时抛出。
            _ToolCallExecutionFailure: 用户拒绝审核时抛出，供外层转为工具失败结果。

        说明:
            `requires_confirmation` 只负责在构建单元时插入异步 `HitlTool`。
            该内置工具通过通用异步中断链路产出结果，业务工具只读取其标准结果。
        """

        if not executable_tool.tool_metadata.requires_confirmation:
            return

        tool_state = ensure_instance(context.task.state, ToolState)
        idx = tool_state.execution_units.index(execution_unit)
        previous_idx = idx - 1
        if previous_idx < 0:
            raise OSRuntimeError("需要审批的tool前未添加审核节点")

        hitl_unit = ensure_instance(tool_state.execution_units[previous_idx], ToolCallExecutionUnit)
        hitl_tool = self._get_tool_by_meta_id(hitl_unit.tool_meta_id)
        if not hitl_tool.tool_metadata.id.startswith(K_HITL_TOOL_ID_PREFIX):
            raise OSRuntimeError(f"前置审核节点无效，tool_metadata_id {hitl_tool.tool_metadata.id}")

        # 审批成功
        tool_result = ensure_instance(
            hitl_unit.result,
            ToolCallExecutionResult,
            "前置 HitlTool 执行结果",
        ).tool_result
        if tool_result.status == ToolResultStatus.SUCCESS:
            return

        # 审批失败
        raise _ToolCallExecutionFailure(
            reason=tool_result.content,
            raw_data={
                "failure_stage": "resolve_confirmation",
                "tool_call_id": execution_unit.tool_call.id,
                "tool_name": executable_tool.tool_metadata.name,
            },
        )

    def _resolve_tool_call_function(
        self,
        execution_unit: ToolCallExecutionUnit,
    ) -> LLMFunction:
        """解析工具调用单元中的函数调用信息。

        参数:
            execution_unit: 当前待执行的工具调用单元。

        返回:
            LLM 生成的完整函数调用信息。

        异常:
            _ToolCallExecutionFailure: 当 tool_call 缺少 id、function 或函数名时抛出。
        """

        tool_call = execution_unit.tool_call
        if not tool_call.id or tool_call.function is None or not tool_call.function.name:
            raise _ToolCallExecutionFailure(
                reason=(
                    "工具调用执行单元缺少必要字段："
                    f"tool_meta_id={execution_unit.tool_meta_id}，"
                    f"tool_call_id={tool_call.id}，"
                    f"function={tool_call.function}"
                ),
                raw_data={
                    "failure_stage": "resolve_tool_call",
                    "tool_meta_id": execution_unit.tool_meta_id,
                    "tool_call_id": tool_call.id,
                },
            )
        return tool_call.function

    def _resolve_executable_tool(
        self,
        execution_unit: ToolCallExecutionUnit,
        function: LLMFunction,
    ) -> ExecutableTool:
        """根据执行单元中的工具元信息 id 获取工具对象。

        参数:
            execution_unit: 当前待执行的工具调用单元。
            function: LLM 生成的函数调用信息。

        返回:
            当前执行单元匹配到的工具对象。

        异常:
            _ToolCallExecutionFailure: 当工具元信息 id 无法匹配到工具时抛出。
        """

        try:
            if HitlTool.is_hitl_tool(execution_unit.tool_meta_id):
                self._register_hitl_tool_if_needed(hitl_tool_id=execution_unit.tool_meta_id)
            return self._get_tool_by_meta_id(execution_unit.tool_meta_id)
        except Exception as exc:
            raise _ToolCallExecutionFailure(
                reason=(
                    "工具调用执行单元找不到工具定义："
                    f"tool_meta_id={execution_unit.tool_meta_id}，error={exc}"
                ),
                raw_data={
                    "failure_stage": "lookup_tool",
                    "tool_meta_id": execution_unit.tool_meta_id,
                    "tool_name": function.name,
                },
            ) from exc

    def _parse_tool_arguments(
        self,
        execution_unit: ToolCallExecutionUnit,
        executable_tool: ExecutableTool,
        function: LLMFunction,
    ) -> JsonDict:
        """解析工具调用参数为 JSON object。

        参数:
            execution_unit: 当前待执行的工具调用单元。
            executable_tool: 当前匹配到的工具对象。
            function: LLM 生成的函数调用信息。

        返回:
            解析后的工具输入字典。

        异常:
            _ToolCallExecutionFailure: 当参数不是合法 JSON object 时抛出。
        """

        if executable_tool.input_model is EmptyToolInputModel:
            return {}

        try:
            arguments = sanitize_json_string(function.arguments or "{}", empty_str="{}")
            raw_tool_input = json.loads(arguments)
        except Exception as exc:
            raise _ToolCallExecutionFailure(
                reason=(
                    "工具调用参数 JSON 清洗或解析失败："
                    f"error={exc}"
                ),
                raw_data={
                    "failure_stage": "parse_arguments",
                    "tool_meta_id": execution_unit.tool_meta_id,
                    "tool_name": function.name,
                    "arguments": function.arguments,
                },
            ) from exc

        if not isinstance(raw_tool_input, dict):
            raise _ToolCallExecutionFailure(
                reason=(
                    "工具调用参数结构非法：解析后的 arguments 必须是 JSON object，"
                    f"实际类型为 {type(raw_tool_input).__name__}"
                ),
                raw_data={
                    "failure_stage": "parse_arguments",
                    "tool_meta_id": execution_unit.tool_meta_id,
                    "tool_name": function.name,
                    "arguments": function.arguments,
                    "parsed_type": type(raw_tool_input).__name__,
                },
            )
        return raw_tool_input

    def _validate_tool_input_model(
        self,
        execution_unit: ToolCallExecutionUnit,
        executable_tool: ExecutableTool,
        function: LLMFunction,
        tool_input: JsonDict,
    ) -> BaseModel:
        """把工具输入字典校验并反序列化为工具输入模型。

        参数:
            execution_unit: 当前待执行的工具调用单元。
            executable_tool: 当前匹配到的工具对象。
            function: LLM 生成的函数调用信息。
            tool_input: 已解析的工具输入字典。

        返回:
            已校验完成的 Pydantic 输入模型。

        异常:
            _ToolCallExecutionFailure: 当输入模型校验失败时抛出。
        """
        try:
            return executable_tool.validate_tool_input(tool_input)
        except KernelToolConfigurationError as exc:
            raise _ToolCallExecutionFailure(
                reason=(
                    "工具输入模型校验失败："
                    f"error={exc}"
                ),
                raw_data={
                    "failure_stage": "validate_input_model",
                    "tool_meta_id": execution_unit.tool_meta_id,
                    "tool_name": function.name,
                    "arguments": function.arguments,
                    "parsed_arguments": tool_input,
                },
            ) from exc

    def _bind_tool_input_context(self, input_model: BaseModel) -> None:
        """把 os 层工具上下文绑定到已校验的工具输入模型。

        参数:
            input_model: 当前工具调用的输入模型实例。

        返回:
            无返回值。
        """

        ToolInputContext(
            event_bus_id=self.os_service.event_bus_id,
            tool_service=self,
        ).bind_to_input_model(input_model)

    def _run_before_execute_hooks(
        self,
        execution_unit: ToolCallExecutionUnit,
        executable_tool: ExecutableTool,
        function: LLMFunction,
        input_model: BaseModel,
        tool_input: JsonDict,
    ) -> None:
        """执行工具生命周期前置回调与业务校验。

        参数:
            execution_unit: 当前待执行的工具调用单元。
            executable_tool: 当前匹配到的工具对象。
            function: LLM 生成的函数调用信息。
            input_model: 已反序列化的工具输入模型。
            tool_input: 已解析的工具输入字典，用于失败排障。

        返回:
            无返回值。

        异常:
            _ToolCallExecutionFailure: 当前置回调异常或业务校验拒绝执行时抛出。
        """
        try:
            executable_tool.before_execute(input_model)
            validate_execute_result = executable_tool.validate_execute(input_model)
        except Exception as exc:
            raise _ToolCallExecutionFailure(
                reason=(
                    "工具执行前校验链路异常："
                    f"error={exc}"
                ),
                raw_data={
                    "failure_stage": "before_or_validate_execute",
                    "tool_meta_id": execution_unit.tool_meta_id,
                    "tool_name": function.name,
                    "arguments": function.arguments,
                    "parsed_arguments": tool_input,
                },
            ) from exc

        if not validate_execute_result.allowed:
            raise _ToolCallExecutionFailure(
                reason=validate_execute_result.reason or "工具执行前校验未通过",
                raw_data={
                    "failure_stage": "validate_execute",
                    "execution_unit_id": execution_unit.unit_id,
                    "tool_meta_id": execution_unit.tool_meta_id,
                    "tool_name": function.name,
                },
            )

    async def _execute_sync_tool(
        self,
        context: ToolExecutionContext,
        execution_unit: ToolCallExecutionUnit,
        executable_tool: ExecutableTool,
        input_model: BaseModel,
        tool_call_id: str,
    ) -> ToolResult:
        """按工具执行策略执行同步工具或读取异步工具响应。

        参数:
            context: 当前执行单元所需的运行时上下文。
            execution_unit: 当前待执行的工具调用单元。
            executable_tool: 当前匹配到的工具对象。
            input_model: 已反序列化的工具输入模型。
            tool_call_id: 当前工具调用 id。

        返回:
            当前工具执行生成的标准结果；首次发起异步工具请求时返回暂停型执行结果。
        """
        try:
            return await executable_tool(input_model)
        except Exception as exc:
            reason=(
                "工具执行过程发生异常："
                f"error={exc}"
            )
            raw_data={
                "failure_stage": "execute_tool",
                "tool_meta_id": execution_unit.tool_meta_id,
                "tool_name": executable_tool.tool_metadata.name,
                "tool_call_id": tool_call_id,
            }
            return ToolResult(
                    status=ToolResultStatus.FAILED,
                    content=reason,
                    raw_data=raw_data,
                )

    async def _get_async_tool_response(
        self, 
        context: ToolExecutionContext,
        execution_unit: ToolCallExecutionUnit,
        executable_tool: ExecutableTool
    ) -> Tuple[ToolResult, InterruptionRequest] | None:
        """读取异步工具中断请求对应的外部响应。

        参数:
            context: 当前执行单元所需的运行时上下文。
            execution_unit: 当前待恢复的工具调用单元。

        返回:
            外部系统提交的异步工具结果。

        异常:
            _ToolCallExecutionFailure: 当恢复时缺少匹配响应时抛出。
        """

        # 获取异步结果
        request = context.task.interruption_request
        if (
            request is not None
            and request.request_id != execution_unit.async_result_request_id
        ):
            request = None
        if request is None or request.response is None:
            return None

        try:
            aync_tool_response = ensure_instance(
                request.response.response,
                InterruptionAsyncToolResponse,
                "异步工具响应",
            )
            tool_result = await executable_tool.override_handle_response(aync_tool_response.data)
        except Exception as exc:
            raise _ToolCallExecutionFailure(
                reason=(
                    "恢复异步工具执行单元时响应类型非法："
                    f"真实类型={type(request.response.response)}，"
                    f"目标类型={InterruptionAsyncToolResponse}，"
                    f"error={exc}"
                ),
                raw_data={
                    "failure_stage": "async_tool_response_type",
                    "request_id": request.request_id,
                    "tool_call_id": execution_unit.tool_call.id,
                    "tool_meta_id": execution_unit.tool_meta_id,
                },
            ) from exc
        
        return tool_result, request

    async def _try_perform_async_tool_request(
        self,
        context: ToolExecutionContext,
        execution_unit: ToolCallExecutionUnit,
        executable_tool: ExecutableTool,
        tool_call_id: str,
        input_model: BaseModel
    ) -> ToolCallExecutionResult:
        """为异步工具创建中断请求并暂停当前任务。

        参数:
            context: 当前执行单元所需的运行时上下文。
            execution_unit: 当前待执行的工具调用单元。
            executable_tool: 当前匹配到的异步工具对象。
            tool_call_id: 当前工具调用 id。

        返回:
            使当前执行单元保持 pending、任务进入 paused 的执行结果。
        """
        # Tool 执行请求动作
        await executable_tool.overrideable_start_request(input_model)

        # 发送中断请求
        tool_type = AsyncToolType.HITL if HitlTool.is_hitl_tool(execution_unit.tool_meta_id) else AsyncToolType.NORMAL
        match tool_type:
            case AsyncToolType.HITL:
                hitl_tool = ensure_instance(executable_tool, HitlTool)
                reason = f"工具 {hitl_tool.source_tool_id} 执行需要用户审批。"
            case _:
                reason = f"工具 {executable_tool.tool_metadata.name} 已发起异步执行。"
        request, _ = await self.os_service.perform_interruption_request(
            task=ensure_instance(context.task, ProcessingTask),
            agent_name=context.agent.agent_name,
            type=InterruptionRequestType.ASYNC_TOOL_REQUEST,
            reason=reason,
            request_params={
                "tool_id": execution_unit.tool_meta_id,
                "type": tool_type,
                "data": input_model.model_dump()
            }
        )
        execution_unit.async_result_request_id = request.request_id
        tool_result = ToolResult(
                        status=ToolResultStatus.PENDING,
                        raw_data={
                            "request_id": request.request_id,
                        },
                    )
        return ToolCallExecutionResult(
            next_unit_status=ExecutionUnitStatus.PENDING,
            task_switch_action=TaskSwitchAction.PAUSE,
            tool_result=tool_result,
        )

    async def _build_failed_tool_call_execution_result(
        self,
        *,
        tool_call_id: str,
        reason: str,
        publish_event: Callable[[RuntimeEventPhase, Optional[ToolResult]], Awaitable[None]],
        raw_data: JsonDict | None = None,
    ) -> ToolCallExecutionResult:
        """构建可反馈给 LLM 的失败型工具调用结果。

        参数:
            tool_call_id: 当前模型生成的工具调用 id，用于重建 tool message。
            reason: 面向 LLM 与开发者可读的失败原因。
            publish_event: 当前工具调用单元的事件发布函数。
            raw_data: 附加排障信息；会合并到 `ToolResult.raw_data` 中。

        返回:
            供 Runtime 继续推进的失败型 `ToolCallExecutionResult`。
        """

        failure_raw_data: JsonDict = {
            "tool_call_id": tool_call_id,
        }
        if raw_data:
            failure_raw_data.update(raw_data)

        tool_result = ToolResult(
            status=ToolResultStatus.FAILED,
            content=reason,
            raw_data=failure_raw_data,
        )
        await publish_event(RuntimeEventPhase.STARTED, None)
        result = ToolCallExecutionResult(
            next_unit_status=ExecutionUnitStatus.COMPLETED,
            tool_result=tool_result,
            raw_data=tool_result.raw_data,
            error=reason,
        )
        await publish_event(RuntimeEventPhase.END, tool_result)
        return result

    def _resolve_next_unit_status_from_tool_result(
        self,
        tool_result: ToolResult,
    ) -> ExecutionUnitStatus:
        """根据工具结果推导执行单元下一状态。

        参数:
            tool_result: 当前工具函数调用形成的稳定结果。

        返回:
            当前工具调用单元应切换到的下一状态。
        """

        if tool_result.status == ToolResultStatus.FAILED:
            return ExecutionUnitStatus.FAILED
        
        return ExecutionUnitStatus.COMPLETED

    def _get_background_tool_placeholder(self, tool_result: ToolResult) -> str:
        """从 ToolResult 中提取后台工具占位描述文本。"""

        raw_data = tool_result.raw_data
        if not isinstance(raw_data, dict):
            return ""

        background_tool_placeholder = raw_data.get("background_tool_placeholder")
        if isinstance(background_tool_placeholder, str):
            return background_tool_placeholder

        return ""

    def _get_tool_by_meta_id(self, tool_meta_id: str) -> ExecutableTool:
        """根据工具元信息 id 查找已装配的工具对象。

        参数:
            tool_meta_id: 当前工具元信息中的稳定 id。

        返回:
            已装配的工具对象。
        """

        executable_tool = self.tools.get(tool_meta_id)
        if executable_tool is None:
            raise KernelToolLookupError(
                f"未找到指定元信息 id 的工具：tool_meta_id={tool_meta_id}"
            )
        return executable_tool

    def _register_hitl_tool_if_needed(self, source_tool_id: str="", hitl_tool_id: str="") -> HitlTool:
        if not source_tool_id:
            if not hitl_tool_id:
                raise OSRuntimeError("注册 HitlTool 失败，未传 source_tool_id 或者 hitl_tool_id")

            source_tool_id = hitl_tool_id.lstrip(K_HITL_TOOL_ID_PREFIX)
        hitl_tool = HitlTool(source_tool_id=source_tool_id)
        if self.get_tool_by_id(hitl_tool.tool_metadata.id) is None:
            self.register_tool(hitl_tool)
        return hitl_tool
