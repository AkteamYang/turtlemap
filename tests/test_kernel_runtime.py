#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/02 14:23
# @Author  : YaHaoo
# @File    : test_kernel_runtime.py

"""kernel Runtime 最小主路径测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from turtlemap.kernel.models import (
    EventSource,
    EventType,
    MessageRole,
    SessionStateSaveKind,
    ToolExecutionStrategy,
)
from turtlemap.kernel.interfaces import (
    ContextBuildProviderProtocol,
    ExecutorProtocol,
    ModelClientProviderProtocol,
    StateStoreProtocol,
)
from turtlemap.kernel.models import (
    BaseAgentState,
    Message,
    ObservableEvent,
    SystemDefinition,
)
from turtlemap.os.context.models import ContextBuildInput, ContextBuildResult
from turtlemap.kernel.runtime import BaseRuntime, RuntimeServices
from turtlemap.kernel.tool import ExecutableTool, ExecutionResult, ExecutionUnit, ToolCall, ToolMetadata
from turtlemap.os.agent import Agent
from turtlemap.os.tool import ToolResult, ToolResultStatus


@dataclass
class InMemoryStateStore(StateStoreProtocol):
    """用于测试的内存状态存储。"""

    # 会话标识到会话状态对象的映射。
    session_id2state: dict[str, object] = field(default_factory=dict)

    # `(session_id, agent_name)` 到当前 `BaseAgentState` 的映射。
    key2agent_state: dict[tuple[str, str], BaseAgentState] = field(default_factory=dict)

    async def load_session_state(self, session_id: str):
        """按会话标识加载会话状态。"""

        return self.session_id2state.get(session_id)

    async def save_session_state(self, session_state, save_kind: SessionStateSaveKind):
        """保存会话状态。"""

        _ = save_kind
        current_state = self.session_id2state.get(session_state.session_id)
        session_state.version = (current_state.version if current_state is not None else 0) + 1
        self.session_id2state[session_state.session_id] = session_state

    async def load_agent_state(self, session_id: str, agent_name: str):
        """按会话和 Agent 名称加载当前 Agent 状态。"""

        return self.key2agent_state.get((session_id, agent_name))

    async def save_agent_state(self, session_id: str, agent_state: BaseAgentState):
        """保存 Agent 状态快照。"""

        current_state = self.key2agent_state.get((session_id, agent_state.agent_name))
        agent_state.version = (current_state.version if current_state is not None else 0) + 1
        self.key2agent_state[(session_id, agent_state.agent_name)] = agent_state

    async def save_context_compression_result(
        self,
        session_state,
        agent_name: str,
        compression_result,
    ) -> None:
        """测试占位：保存后台压缩结果。"""

        _ = (session_state, agent_name, compression_result)

class FakeContextBuildProvider(ContextBuildProviderProtocol):
    """用于测试的最小上下文构建器。"""

    async def build(self, build_input: ContextBuildInput) -> ContextBuildResult:
        messages: list[Message] = [
            Message(
                role=MessageRole.SYSTEM,
                content=build_input.owner_agent.system.objective,
            )
        ]
        messages.extend(
            Message(role=message.role, content=message.content)
            for message in build_input.owner_agent_state.history
        )
        tool_schemas = [
            {
                "name": executable_tool.tool_metadata.name,
                "description": executable_tool.tool_metadata.description,
                "input_schema": executable_tool.tool_metadata.input_schema,
            }
            for executable_tool in build_input.available_tools
        ]
        return ContextBuildResult(
            messages=messages,
            tool_schemas=tool_schemas,
            total_tokens=0,
        )

    def build_task_artifacts(
        self,
        owner_agent,
        owner_state,
        task,
        exclude_start_input: bool = False,
    ) -> list[Message]:
        """根据任务生成待写入 history 的最小消息列表。"""

        _ = (owner_agent, owner_state, exclude_start_input)
        if getattr(task.state, "content_delta", ""):
            return [
                Message(
                    role=MessageRole.ASSISTANT,
                    content=task.state.content_delta,
                )
            ]

        tool_state_message = getattr(task.state, "assistant_tool_call", None)
        if tool_state_message is not None and getattr(tool_state_message, "content", ""):
            return [
                Message(
                    role=MessageRole.ASSISTANT,
                    content=tool_state_message.content,
                )
            ]

        return []

    async def schedule_background_compression_if_needed(
        self,
        owner_agent,
        owner_state,
        session_state,
    ) -> None:
        """history 写入后的测试占位治理入口。"""

        _ = (owner_agent, owner_state, session_state)


@dataclass
class QueueModelClient(ModelClientProviderProtocol):
    """按预设顺序返回响应的假模型客户端。"""

    # 按调用顺序依次弹出的模型响应列表。
    responses: list[Message]

    async def generate(self, messages, tool_schemas=None):
        return self.responses.pop(0)


@dataclass
class StaticModelClientProvider(ModelClientProviderProtocol):
    """始终返回同一个模型客户端。"""

    # 当前 provider 固定返回的模型客户端实例。
    client: ModelClientProviderProtocol

    async def generate(self, messages, tool_schemas=None):
        """代理执行一次测试模型推理。"""

        return await self.client.generate(messages=messages, tool_schemas=tool_schemas)


@dataclass
class FakeExecutor(ExecutorProtocol):
    """用于测试的最小执行器。"""

    # 当前执行器可直接调度的工具映射。
    tool_id2tool: dict[str, ExecutableTool] = field(default_factory=dict)

    async def execute(self, execution_unit: ExecutionUnit) -> ExecutionResult | None:
        """直接执行一个工具调用单元。"""

        tool_id = getattr(execution_unit, "tool_id")
        executable_tool = self.tool_id2tool[tool_id]
        return executable_tool(execution_unit.arguments or {})

    async def submit(self, tool_call_id: str, tool: ExecutableTool, tool_input: dict[str, object]) -> None:
        """接收后台任务提交。"""

        _ = (tool_call_id, tool, tool_input)

    async def poll(self, tool_call_id: str) -> ExecutionResult | None:
        """查询后台任务结果。"""

        _ = tool_call_id
        return None


def test_runtime_can_finish_plain_message_path():
    """验证最小 user_input -> assistant 回复主路径。"""

    root_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(role="assistant", objective="reply to user"),
    )
    client = QueueModelClient(
        responses=[
            Message(
                role=MessageRole.ASSISTANT,
                content="hello from assistant",
            )
        ]
    )
    runtime = BaseRuntime(
        root_agent=root_agent,
        services=RuntimeServices(
            model_client_provider=StaticModelClientProvider(client=client),
            context_build_provider=FakeContextBuildProvider(),
            state_store=InMemoryStateStore(),
            executor=FakeExecutor(),
        ),
    )

    session_state = asyncio.run(
        runtime.run(
            input_events=[
                ObservableEvent(
                    event_id="event_1",
                    event_type=EventType.USER_INPUT,
                    source=EventSource.USER,
                    payload={"content": "hello"},
                )
            ]
        )
    )

    owner_state = session_state.agent_name2agent_state["root_agent"]
    assert owner_state.history[-1].content == "hello from assistant"
    assert session_state.input_queue == []


def test_runtime_can_finish_tool_loop_path():
    """验证最小 tool call -> tool result -> continuation 主路径。"""

    def echo_tool(tool_input):
        return ToolResult(status=ToolResultStatus.SUCCESS, content=f"echo:{tool_input['text']}")

    echo_executable_tool = ExecutableTool(
        tool_metadata=ToolMetadata(
            id="tool_echo",
            name="echo_tool",
            description="echo input text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            execution_strategy=ToolExecutionStrategy.SYNC,
        ),
        call=echo_tool,
    )
    root_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(role="assistant", objective="use tool if needed"),
        tools=[echo_executable_tool],
    )
    client = QueueModelClient(
        responses=[
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(
                        tool_call_id="call_1",
                        tool_name="echo_tool",
                        arguments={"text": "ping"},
                    )
                ],
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content="tool loop finished",
            ),
        ]
    )
    runtime = BaseRuntime(
        root_agent=root_agent,
        services=RuntimeServices(
            model_client_provider=StaticModelClientProvider(client=client),
            context_build_provider=FakeContextBuildProvider(),
            state_store=InMemoryStateStore(),
            executor=FakeExecutor(
                tool_id2tool={
                    echo_executable_tool.tool_metadata.id: echo_executable_tool,
                }
            ),
        ),
    )

    session_state = asyncio.run(
        runtime.run(
            input_events=[
                ObservableEvent(
                    event_id="event_1",
                    event_type=EventType.USER_INPUT,
                    source=EventSource.USER,
                    payload={"content": "please use tool"},
                )
            ]
        )
    )

    owner_state = session_state.agent_name2agent_state["root_agent"]
    assert owner_state.history[-1].content == "tool loop finished"
