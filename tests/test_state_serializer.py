#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/11 09:57
# @Author  : YaHaoo
# @File    : test_state_serializer.py

"""os/store 状态序列化边界测试。"""

from __future__ import annotations

import sys
from pathlib import Path

from turtlemap.kernel.models import (
    EventSource,
    EventType,
    MessageState,
    MessageRole,
    RuntimeArtifactType,
    SessionStateSaveKind,
)
from turtlemap.kernel.models import (
    AgentFrame,
    Input,
    ObservableEvent,
    RuntimeArtifact,
    SystemDefinition,
    UserInputPayload,
)
from turtlemap.os import AgentState, SessionState
from turtlemap.os.event_bus.models import InputEvent, RuntimeEventPhase
from turtlemap.os.models import ProcessingTask
from turtlemap.os.llm.model import LLMCompletionToolCall, LLMFunction, LLMMessage
from turtlemap.kernel.tool import ToolResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from mysql_store import StateSerializer


def test_tool_result_migrates_legacy_error_field() -> None:
    """验证旧版 ToolResult.error 字段会迁移到 content。

    返回:
        无返回值。
    """

    tool_result = ToolResult.model_validate(
        {
            "status": "failed",
            "content": "",
            "raw_data": {"tool_call_id": "tool_call_1"},
            "error": "工具调用失败",
        }
    )

    assert tool_result.content == "工具调用失败"
    assert "error" not in tool_result.model_dump()


def test_state_serializer_round_trips_session_state_with_agent_name2agent_state() -> None:
    """验证 SessionState JSON 直接保存并恢复 AgentState 运行现场。"""

    session_state = SessionState(
        session_id="session_1",
        agent_frames=[AgentFrame(agent_name="root_agent")],
        input_queue=[
            Input(
                input_id="input_1",
                events=[
                    ObservableEvent(
                        event_id="event_1",
                        event_type=EventType.USER_INPUT,
                        source=EventSource.USER,
                        payload=UserInputPayload(content="你好"),
                    )
                ],
            )
        ],
        agent_name2agent_state={
            "root_agent": AgentState(
                agent_name="root_agent",
                system=SystemDefinition(role="assistant", objective="help user"),
            )
        },
    )

    record = StateSerializer.build_session_state_record(
        session_state=session_state,
        save_kind=SessionStateSaveKind.CHECKPOINT,
    )
    restored_session_state = StateSerializer.restore_session_state(record)

    assert restored_session_state.session_id == "session_1"
    assert record.version == 0
    assert record.schema_version == 0
    assert restored_session_state.input_queue[0].events[0].payload == UserInputPayload(content="你好")
    restored_agent_state = restored_session_state.agent_name2agent_state["root_agent"]
    assert restored_agent_state.agent_name == "root_agent"


def test_state_serializer_round_trips_session_state_with_tool_history() -> None:
    """验证 AgentState history 会跟随 SessionState 一起保存和恢复。"""

    tool_call = LLMCompletionToolCall(
        id="tool_call_1",
        function=LLMFunction(
            name="search",
            arguments='{"query": "turtlemap"}',
        ),
        type="function",
    )
    agent_state = AgentState(
        agent_name="root_agent",
            system=SystemDefinition(
                role="assistant",
                objective="help user",
                constraints="简洁回答",
            ),
        history=[
            RuntimeArtifact(
                type=RuntimeArtifactType.INPUT,
                payload=Input(
                    input_id="input_1",
                    events=[
                        ObservableEvent(
                            event_id="event_1",
                            event_type=EventType.USER_INPUT,
                            source=EventSource.USER,
                            payload=UserInputPayload(content="查一下 turtlemap"),
                        )
                    ],
                ),
            ),
            RuntimeArtifact(
                type=RuntimeArtifactType.TOOL_CALL,
                payload=LLMMessage(
                    role=MessageRole.ASSISTANT,
                    content="我来查",
                    tool_calls=[tool_call],
                ),
            ),
        ],
    )

    session_state = SessionState(
        session_id="session_1",
        agent_name2agent_state={"root_agent": agent_state},
    )
    record = StateSerializer.build_session_state_record(
        session_state=session_state,
        save_kind=SessionStateSaveKind.CHECKPOINT,
    )
    restored_session_state = StateSerializer.restore_session_state(record=record)
    restored_agent_state = restored_session_state.agent_name2agent_state["root_agent"]

    assert restored_agent_state.agent_name == "root_agent"
    assert '"history":' in record.state_json
    assert '"agent_name2agent_state":' in record.state_json
    assert len(restored_agent_state.history) == 2
    restored_user_input = Input.model_validate(restored_agent_state.history[0].payload)
    restored_tool_call_message = LLMMessage.model_validate(restored_agent_state.history[1].payload)
    restored_user_payload = UserInputPayload.model_validate(
        restored_user_input.events[0].payload
    )
    assert restored_user_payload.content == "查一下 turtlemap"
    assert restored_tool_call_message.tool_calls[0].function is not None
    assert restored_tool_call_message.tool_calls[0].function.name == "search"


def test_state_serializer_round_trips_processing_task_event_buffer() -> None:
    """验证任务事件 buffer 会随 os ProcessingTask 保存并恢复。

    返回:
        无返回值。
    """

    start_input = Input(
        input_id="input_1",
        events=[
            ObservableEvent(
                event_id="input_event_1",
                event_type=EventType.USER_INPUT,
                source=EventSource.USER,
                payload=UserInputPayload(content="继续处理任务"),
            )
        ],
    )
    processing_task = ProcessingTask(
        task_id="task_1",
        state=MessageState(origin_input_id=start_input.input_id),
        start_input=start_input,
    )
    processing_task.event_buffer.events.append(
        InputEvent(
            event_id="runtime_event_1",
            session_id="session_1",
            agent_name="root_agent",
            task_id=processing_task.task_id,
            input=start_input,
            event_phase=RuntimeEventPhase.FINAL,
        )
    )
    session_state = SessionState(
        session_id="session_1",
        agent_name2agent_state={
            "root_agent": AgentState(
                agent_name="root_agent",
                system=SystemDefinition(role="assistant", objective="help user"),
                processing_tasks=[processing_task],
            )
        },
    )

    record = StateSerializer.build_session_state_record(
        session_state=session_state,
        save_kind=SessionStateSaveKind.CHECKPOINT,
    )
    restored_session_state = StateSerializer.restore_session_state(record=record)
    restored_task = restored_session_state.agent_name2agent_state[
        "root_agent"
    ].processing_tasks[0]

    assert isinstance(restored_task, ProcessingTask)
    assert len(restored_task.event_buffer.events) == 1
    assert isinstance(restored_task.event_buffer.events[0], InputEvent)
    assert restored_task.event_buffer.events[0].event_id == "runtime_event_1"
