#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/07 16:00
# @Author  : YaHaoo
# @File    : test_interruption_resume.py

"""任务中断 request / response 基础链路测试。"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from turtlemap.kernel.models import (
    AgentFrame,
    BaseAgentState,
    BaseSessionState,
    EventSource,
    EventType,
    ExecutionUnitStatus,
    Input,
    InterruptionExceptionResponse,
    InterruptionRequest,
    InterruptionRequestType,
    InterruptionResponsePayload,
    MessageRole,
    MessageState,
    ObservableEvent,
    BaseProcessingTask,
    RuntimeArtifact,
    RuntimeArtifactType,
    SystemDefinition,
    TaskStatus,
    ToolState,
)
from turtlemap.kernel.tool import (
    EmptyToolInputModel,
    ExecutableTool,
    ToolExecutionContext,
    ToolMetadata,
    ToolResult,
)
from turtlemap.kernel.runtime import BaseRuntime
from turtlemap.os.context.provider import ContextBuildProvider
from turtlemap.os.event_bus.models import (
    InterruptedEvent,
    RuntimeEvent,
)
from turtlemap.os.llm.model import LLMCompletionToolCall, LLMFunction, LLMMessage
from turtlemap.os.tool.build_in.hitl import HitlOptionType, HitlTool
from turtlemap.os.tool.service import ToolService
from turtlemap.os.tool.tool import (
    LLMCallExecutionResult,
    LLMCallExecutionUnit,
    ToolCallExecutionResult,
    ToolCallExecutionUnit,
)


class _TestRuntime(BaseRuntime):
    """中断恢复测试使用的最小 Runtime 实现。

    说明:
        当前 BaseRuntime 在消费中断响应后会提交 checkpoint。测试只关心内存态
        状态转换，因此这里提供空实现，避免依赖 os 层持久化服务。
    """

    async def _override_service_commit_runtime_checkpoint(self) -> None:
        """跳过真实 checkpoint 提交。

        返回:
            无返回值。
        """

        return None


class _TestToolService(ToolService):
    """为工具中断测试提供最小 OSService 上下文。"""

    def __init__(
        self,
        event_bus_id: str,
        tools: dict[str, ExecutableTool] | None = None,
    ) -> None:
        """初始化测试 ToolService。

        参数:
            event_bus_id: 当前测试使用的事件通道标识。
            tools: 当前测试需要注入的工具映射。
        """

        super().__init__(tools=tools)
        self._test_os_service = SimpleNamespace(
            event_bus_id=event_bus_id,
            real_session_state=SimpleNamespace(session_id="session:test"),
        )

    @property
    def os_service(self) -> object:
        """返回确认单元测试所需的最小 OSService 替身。"""

        return self._test_os_service


def _build_paused_task() -> BaseProcessingTask:
    """构建带单个待响应请求的暂停任务。

    返回:
        可用于恢复测试的暂停任务。
    """

    task = BaseProcessingTask(
        task_id="task:test",
        state=MessageState(status=TaskStatus.PAUSED),
        start_input=Input(
            input_id="input:test",
            events=[
                ObservableEvent(
                    event_id="observable:test",
                    event_type=EventType.USER_INPUT,
                    source=EventSource.USER,
                    payload={"content": "继续处理之前的任务"},
                )
            ],
        ),
    )
    task.interruption_request = InterruptionRequest(
        request_id="request:0",
        task_id=task.task_id,
        type=InterruptionRequestType.EXCEPTION_RESUME,
        reason="测试中断 0",
        origin_task_status=TaskStatus.RUNNING,
    )
    return task


def _build_runtime_with_task(task: BaseProcessingTask) -> _TestRuntime:
    """构建只用于验证中断响应分发的最小 Runtime。

    参数:
        task: 当前会话中待恢复的暂停任务。

    返回:
        已装配最小 SessionState 的 BaseRuntime 对象。
    """

    runtime = _TestRuntime.__new__(_TestRuntime)
    runtime.session_state = BaseSessionState(
        agent_frames=[AgentFrame(agent_name="agent")],
        agent_name2agent_state={
            "agent": BaseAgentState(
                agent_name="agent",
                system=SystemDefinition(),
                processing_tasks=[task],
            )
        },
    )
    return runtime


def test_interruption_response_payload_restores_and_resumes_paused_task() -> None:
    """验证响应载荷强类型恢复，且单个请求响应后暂停任务恢复。"""

    task = _build_paused_task()
    runtime = _build_runtime_with_task(task)

    event = ObservableEvent.model_validate(
        {
            "event_id": "response:0",
            "event_type": EventType.INTERRUPTION_RESPONSE,
            "source": EventSource.USER,
            "payload": {
                "request_id": "request:0",
                "request_type": "_os_async_tool_request",
                "response": {"data": {"option": "approve"}},
            },
        }
    )
    assert isinstance(event.payload, InterruptionResponsePayload)
    asyncio.run(
        runtime._apply_interruption_response(
            Input(input_id="input:response:0", events=[event])
        )
    )
    assert task.state.status == TaskStatus.RUNNING
    assert task.interruption_request is not None
    assert task.interruption_request.response is event.payload


def test_paused_task_builds_lightweight_interruption_history() -> None:
    """验证暂停任务只生成输入和结构化中断请求，不暴露中间执行现场。"""

    task = _build_paused_task()
    owner_state = BaseAgentState(
        agent_name="agent",
        system=SystemDefinition(),
        processing_tasks=[task],
    )
    provider = ContextBuildProvider.__new__(ContextBuildProvider)

    artifacts = provider.build_task_artifacts(
        owner_agent=object(),
        owner_state=owner_state,
        task=task,
    )

    assert [artifact.type for artifact in artifacts] == [
        RuntimeArtifactType.INPUT,
        RuntimeArtifactType.INTERRUPTION_REQUEST,
    ]
    assert isinstance(artifacts[1].payload, InterruptionRequest)
    assert artifacts[1].payload.request_id == "request:0"

    messages = provider._artifact_to_llm_messages(artifacts[1])
    assert "request:0" in messages[0].content
    assert "测试中断 0" in messages[0].content


def test_unavailable_tool_history_round_is_collapsed() -> None:
    """验证当前不可用工具所在历史轮次只保留输入和最终回答。"""

    task = _build_paused_task()
    history_round = [
        RuntimeArtifact(
            type=RuntimeArtifactType.INPUT,
            payload=task.start_input,
        ),
        RuntimeArtifact(
            type=RuntimeArtifactType.TOOL_CALL,
            payload=LLMMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    LLMCompletionToolCall(
                        id="call:unavailable",
                        type="function",
                        function=LLMFunction(
                            name="unavailable_tool",
                            arguments="{}",
                        ),
                    )
                ],
            ),
        ),
        RuntimeArtifact(
            type=RuntimeArtifactType.TOOL_LLM_RESPONSE,
            payload=LLMCallExecutionUnit(
                result=LLMCallExecutionResult(
                    next_unit_status=ExecutionUnitStatus.COMPLETED,
                    message=LLMMessage(
                        role=MessageRole.ASSISTANT,
                        content="这是此前工具调用后的回答。",
                    ),
                )
            ),
        ),
    ]
    provider = ContextBuildProvider.__new__(ContextBuildProvider)

    messages = provider._build_history_llm_messages(
        history=history_round,
        available_tool_names=set(),
    )

    assert [message.role for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert messages[-1].tool_calls == []
    assert messages[-1].content is not None
    assert "这是此前工具调用后的回答。" in messages[-1].content


def test_expired_tool_result_history_round_is_collapsed() -> None:
    """验证工具可用但结果过期时，历史轮次仍会折叠。"""

    task = _build_paused_task()
    tool_call = LLMCompletionToolCall(
        id="call:expired",
        type="function",
        function=LLMFunction(
            name="query_weather",
            arguments="{}",
        ),
    )
    history_round = [
        RuntimeArtifact(
            type=RuntimeArtifactType.INPUT,
            payload=task.start_input,
        ),
        RuntimeArtifact(
            type=RuntimeArtifactType.TOOL_CALL,
            payload=LLMMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[tool_call],
            ),
        ),
        RuntimeArtifact(
            type=RuntimeArtifactType.TOOL_CALL_EXE,
            payload=ToolCallExecutionUnit(
                tool_meta_id="query_weather",
                tool_call=tool_call,
                result=ToolCallExecutionResult(
                    next_unit_status=ExecutionUnitStatus.COMPLETED,
                    tool_result=ToolResult(
                        content="杭州晴，26 摄氏度。",
                        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    ),
                ),
            ),
        ),
        RuntimeArtifact(
            type=RuntimeArtifactType.TOOL_LLM_RESPONSE,
            payload=LLMCallExecutionUnit(
                result=LLMCallExecutionResult(
                    next_unit_status=ExecutionUnitStatus.COMPLETED,
                    message=LLMMessage(
                        role=MessageRole.ASSISTANT,
                        content="这是此前工具调用后的回答。",
                    ),
                )
            ),
        ),
    ]
    provider = ContextBuildProvider.__new__(ContextBuildProvider)

    messages = provider._build_history_llm_messages(
        history=history_round,
        available_tool_names={"query_weather"},
    )

    assert [message.role for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert messages[0].content is not None
    assert "本轮工具结果已过期，工具执行过程已折叠。" in messages[0].content
    assert messages[-1].tool_calls == []
    assert messages[-1].content is not None
    assert "这是此前工具调用后的回答。" in messages[-1].content


def test_interrupted_event_restores_interruption_request_payload() -> None:
    """验证中断事件反序列化后可直接恢复 InterruptionRequest。"""

    interruption_request = InterruptionRequest(
        request_id="request:test",
        task_id="task:test",
        type="_os_exception_resume",
        reason="模型服务不可用",
    )

    event = InterruptedEvent(
        event_id="request:test",
        event_bus_id="event_bus:test",
        session_id="session:test",
        task_id="task:test",
        interruption_type=InterruptionRequestType.EXCEPTION_RESUME,
        payload=interruption_request,
    )

    restored_event = RuntimeEvent.validate_polymorphic(event.model_dump(mode="json"))
    assert isinstance(restored_event, InterruptedEvent)
    assert isinstance(restored_event.payload, InterruptionRequest)
    assert restored_event.payload.request_id == "request:test"
    assert restored_event.payload.reason == "模型服务不可用"


def test_hitl_tool_interprets_async_response() -> None:
    """验证 HitlTool 通过通用异步工具响应解释用户审核结果。"""

    hitl_tool = HitlTool(source_tool_id="tool:test")
    approved_result = asyncio.run(
        hitl_tool.override_handle_response(
            HitlTool.response_data(HitlOptionType.APPROVE)
        )
    )
    rejected_result = asyncio.run(
        hitl_tool.override_handle_response(
            HitlTool.response_data(HitlOptionType.REJECT)
        )
    )

    assert approved_result.status.value == "success"
    assert approved_result.content == "用户同意"
    assert rejected_result.status.value == "failed"
    assert rejected_result.content == "用户拒绝"
