#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/15 13:54
# @Author  : YaHaoo
# @File    : test_event_bus.py

"""os 层 Runtime 事件总线测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from turtlemap.os.event_bus import (
    ContextCompressionEvent,
    ContextCompressionMode,
    EventBus,
    InputEvent,
    MessageEvent,
    ResultCollector,
    RuntimeEventPhase,
    RuntimeEventType,
    RuntimeRunResult,
    ToolCallEvent,
    ToolResultEvent,
)
from turtlemap.os.llm.model import (
    LLMCompletion,
    LLMCompletionChoice,
    LLMCompletionToolCall,
    LLMFunction,
    LLMMessage,
)
from turtlemap.os.context.models import ContextCompressionResult
from turtlemap.kernel.models import Input, MessageRole, ObservableEvent, UserInputPayload
from turtlemap.kernel.models.enums import EventSource, EventType
from turtlemap.kernel.tool import ToolResult
from turtlemap.shared.ids import generate_prefixed_id


@pytest.fixture(autouse=True)
def reset_event_bus() -> Iterator[None]:
    """在每个测试前后重置事件总线类级运行时状态。

    返回:
        pytest fixture 迭代器。
    """

    EventBus.reset()
    yield
    EventBus.reset()


def _build_message_event(
    *,
    event_id: str | None = None,
    content: str = "hello",
) -> MessageEvent:
    """构造测试用普通文本完成事件。

    参数:
        event_id: 可选事件 id；未提供时自动生成。
        content: 当前完成事件承载的文本。

    返回:
        可直接发送到 event bus 的 MessageEvent。
    """

    return MessageEvent(
        event_id=event_id or generate_prefixed_id("event"),
        session_id="session_1",
        agent_name="root_agent",
        completion=_build_completion(content),
        event_phase=RuntimeEventPhase.FINAL,
        sequence=999,
    )


def _build_input_event(*, task_id: str | None = None) -> InputEvent:
    """构造测试用 Runtime 输入事件。

    参数:
        task_id: 当前输入关联的任务 id。

    返回:
        可直接发送到 event bus 的 InputEvent。
    """

    return InputEvent(
        event_id=generate_prefixed_id("event"),
        session_id="session_1",
        agent_name="root_agent",
        task_id=task_id,
        input=Input(
            input_id="input_1",
            events=[
                ObservableEvent(
                    event_id="input_event_1",
                    event_type=EventType.USER_INPUT,
                    source=EventSource.USER,
                    payload=UserInputPayload(content="你好"),
                )
            ],
        ),
    )


def _build_completion(content: str) -> LLMCompletion:
    """构造测试用完整 LLM completion。

    参数:
        content: 当前 assistant 消息文本。

    返回:
        可放入 MessageEvent 的完整 completion。
    """

    return LLMCompletion(
        id=generate_prefixed_id("completion"),
        choices=[
            LLMCompletionChoice(
                index=0,
                message=LLMMessage(
                    role=MessageRole.ASSISTANT,
                    content=content,
                ),
            )
        ],
        created=1,
        model="test-model",
    )


def test_tool_result_event_builds_final_event() -> None:
    """验证工具结果事件可直接从 STARTED / END 链构建 FINAL 事件。"""

    event_id = "event:tool-result"
    started_event = ToolResultEvent(
        event_id=event_id,
        session_id="session:test",
        tool_call=LLMCompletionToolCall(id="call:test"),
        sequence=1,
        created_ts_ms=100,
        event_phase=RuntimeEventPhase.STARTED,
    )
    end_event = ToolResultEvent(
        event_id=event_id,
        session_id="session:test",
        tool_call=LLMCompletionToolCall(id="call:test"),
        sequence=2,
        created_ts_ms=140,
        result=ToolResult(content="工具执行完成"),
        event_phase=RuntimeEventPhase.END,
    )

    final_event = ToolResultEvent.create_final_event_from_events(
        [started_event, end_event]
    )

    assert final_event.event_phase == RuntimeEventPhase.FINAL
    assert final_event.result is not None
    assert final_event.result.content == "工具执行完成"
    assert final_event.duration_ms == 40


def test_event_bus_assigns_sequence_and_logs_listener_errors() -> None:
    """验证 event bus 分配 sequence，并隔离 listener 异常。"""

    async def run_test() -> None:
        """执行异步事件发送验证。"""

        event_bus_id = EventBus.create_event_bus()
        received_sequences: list[int] = []

        async def good_listener(event) -> None:
            """记录正常监听者收到的事件序号。"""

            received_sequences.append(event.sequence)

        async def bad_listener(event) -> None:
            """模拟失败监听者，不应影响其他监听者。"""

            _ = event
            raise RuntimeError("listener failed")

        EventBus.subscribe(event_bus_id, good_listener)
        EventBus.subscribe(event_bus_id, bad_listener)

        event = await EventBus.publish(
            event_bus_id,
            _build_message_event()
        )

        assert event.sequence == 1
        assert received_sequences == [1]

    asyncio.run(run_test())


def test_event_bus_processes_events_in_sequence_order() -> None:
    """验证同一事件通道内事件按 sequence 顺序通知。"""

    async def run_test() -> None:
        """执行异步顺序验证。"""

        event_bus_id = EventBus.create_event_bus()
        received_contents: list[str] = []

        async def listener(event: MessageEvent) -> None:
            """记录事件内容并让首个事件慢一些完成。"""

            if event.sequence == 1:
                await asyncio.sleep(0.01)
            assert event.completion is not None
            received_contents.append(event.completion.choices[0].message.content or "")

        EventBus.subscribe(event_bus_id, listener)

        await asyncio.gather(
            EventBus.publish(
            event_bus_id,
                _build_message_event(
                    content="first",
                )
            ),
            EventBus.publish(
            event_bus_id,
                _build_message_event(
                    content="second",
                )
            ),
        )

        assert received_contents == ["first", "second"]

    asyncio.run(run_test())


def test_event_bus_filters_listener_by_event_types() -> None:
    """验证 subscribe 可按事件类型过滤监听触达。"""

    async def run_test() -> None:
        """执行异步事件类型过滤验证。"""

        event_bus_id = EventBus.create_event_bus()
        received_event_types: list[RuntimeEventType] = []

        async def listener(event: MessageEvent) -> None:
            """记录被过滤后真正触达的事件类型。"""

            received_event_types.append(event.event_type)

        EventBus.subscribe(
            event_bus_id,
            listener,
            event_types=(RuntimeEventType.MESSAGE,),
        )

        await EventBus.publish(
            event_bus_id,
            ToolCallEvent(
                event_id=generate_prefixed_id("event"),
                session_id="session_1",
                agent_name="root_agent",
                tool_call=LLMCompletionToolCall(
                    id="call_1",
                    function=LLMFunction(name="search", arguments="{}"),
                    type="function",
                ),
            )
        )
        await EventBus.publish(
            event_bus_id,
            _build_message_event()
        )

        assert received_event_types == [RuntimeEventType.MESSAGE]

    asyncio.run(run_test())


def test_result_collector_preserves_final_event_sequence() -> None:
    """验证 ResultCollector 只用稳定事件产出结果，并按起始 sequence 排序。"""

    async def run_test() -> None:
        """执行异步结果收集验证。"""

        event_bus_id = EventBus.create_event_bus()
        collector = ResultCollector(event_bus_id=event_bus_id)
        EventBus.subscribe(
            event_bus_id,
            collector.collect,
            event_types=ResultCollector.listen_event_types(),
        )

        message_event_id = generate_prefixed_id("event")
        tool_event_id = generate_prefixed_id("event")

        await EventBus.publish(event_bus_id, _build_input_event())
        await EventBus.publish(
            event_bus_id,
            MessageEvent(
                event_id=message_event_id,
                session_id="session_1",
                agent_name="root_agent",
                completion=_build_completion("hello"),
                event_phase=RuntimeEventPhase.FINAL,
            )
        )
        await EventBus.publish(
            event_bus_id,
            ToolCallEvent(
                event_id=tool_event_id,
                session_id="session_1",
                agent_name="root_agent",
                tool_call=LLMCompletionToolCall(
                    id="call_1",
                    function=LLMFunction(
                        name="search",
                        arguments='{"query": "turtlemap"}',
                    ),
                    type="function",
                ),
            )
        )

        outputs = collector.get_outputs()

        assert len(outputs) == 1
        assert outputs[0].input.input_id == "input_1"
        output_group = outputs[0].outputs
        assert [output.event_id for output in output_group] == [
            message_event_id,
            tool_event_id,
        ]
        assert isinstance(output_group[0], MessageEvent)
        assert output_group[0].completion.choices[0].message.content == "hello"
        assert output_group[0].sequence == 2
        assert isinstance(output_group[1], ToolCallEvent)
        assert output_group[1].tool_call.id == "call_1"

    asyncio.run(run_test())


def test_result_collector_builds_tool_result_state_events_tree() -> None:
    """验证 collector 能把父子事件组装为 tool result 输出树。"""

    async def run_test() -> None:
        """执行异步树形输出收集验证。"""

        event_bus_id = EventBus.create_event_bus()
        collector = ResultCollector(event_bus_id=event_bus_id)
        EventBus.subscribe(
            event_bus_id,
            collector.collect,
            event_types=ResultCollector.listen_event_types(),
        )

        tool_result_event_id = generate_prefixed_id("event")
        child_message_event_id = generate_prefixed_id("event")

        await EventBus.publish(event_bus_id, _build_input_event())
        await EventBus.publish(
            event_bus_id,
            ToolResultEvent(
                event_id=tool_result_event_id,
                session_id="session_1",
                agent_name="root_agent",
                tool_call=LLMCompletionToolCall(id="call_1"),
                event_phase=RuntimeEventPhase.STARTED,
            )
        )
        await EventBus.publish(
            event_bus_id,
            MessageEvent(
                event_id=child_message_event_id,
                parent_event_id=tool_result_event_id,
                session_id="session_1",
                agent_name="child_agent",
                completion=_build_completion("child output"),
                event_phase=RuntimeEventPhase.FINAL,
            )
        )

        await EventBus.publish(
            event_bus_id,
            ToolResultEvent(
                event_id=tool_result_event_id,
                session_id="session_1",
                agent_name="root_agent",
                tool_call=LLMCompletionToolCall(id="call_1"),
                result=ToolResult(content="tool done"),
                event_phase=RuntimeEventPhase.END,
            )
        )

        outputs = collector.get_outputs()

        assert len(outputs) == 1
        assert outputs[0].input.input_id == "input_1"
        output_group = outputs[0].outputs
        assert len(output_group) == 1
        assert isinstance(output_group[0], ToolResultEvent)
        assert output_group[0].result.content == "tool done"
        assert len(output_group[0].state_events) == 1
        assert isinstance(output_group[0].state_events[0], MessageEvent)
        assert output_group[0].state_events[0].parent_event_id == tool_result_event_id

    asyncio.run(run_test())


def test_result_collector_builds_tool_result_output_from_final_event() -> None:
    """验证 collector 支持从单条 FINAL 工具结果事件构建 Runtime 输出。"""

    async def run_test() -> None:
        """执行单条 FINAL 工具结果输出收集验证。"""

        event_bus_id = EventBus.create_event_bus()
        collector = ResultCollector(event_bus_id=event_bus_id)
        EventBus.subscribe(
            event_bus_id,
            collector.collect,
            event_types=ResultCollector.listen_event_types(),
        )

        tool_result_event_id = generate_prefixed_id("event")
        await EventBus.publish(event_bus_id, _build_input_event())
        await EventBus.publish(
            event_bus_id,
            ToolResultEvent(
                event_id=tool_result_event_id,
                session_id="session_1",
                agent_name="root_agent",
                tool_call=LLMCompletionToolCall(id="call_1"),
                result=ToolResult(content="tool done"),
                event_phase=RuntimeEventPhase.FINAL,
            )
        )

        outputs = collector.get_outputs()

        assert len(outputs) == 1
        output_group = outputs[0].outputs
        assert len(output_group) == 1
        assert isinstance(output_group[0], ToolResultEvent)
        assert output_group[0].tool_call.id == "call_1"
        assert output_group[0].result.content == "tool done"

    asyncio.run(run_test())


def test_result_collector_sets_tool_result_final_duration_ms() -> None:
    """验证 collector 会在 tool result FINAL 事件中写入聚合耗时。"""

    async def run_test() -> None:
        """执行工具结果 FINAL 耗时聚合验证。"""

        event_bus_id = EventBus.create_event_bus()
        collector = ResultCollector(event_bus_id=event_bus_id)
        final_events: list[ToolResultEvent] = []

        async def collect_final_event(event: ToolResultEvent) -> None:
            """收集派生出的工具结果 FINAL 事件。"""

            if event.event_phase == RuntimeEventPhase.FINAL:
                final_events.append(event)

        EventBus.subscribe(
            event_bus_id,
            collector.collect,
            completion_listener=collector.finalize_outputs,
            event_types=ResultCollector.listen_event_types(),
        )
        EventBus.subscribe(
            event_bus_id,
            collect_final_event,
            event_types=(RuntimeEventType.TOOL_RESULT,),
        )

        tool_result_event_id = generate_prefixed_id("event")
        started_event = await EventBus.publish(
            event_bus_id,
            ToolResultEvent(
                event_id=tool_result_event_id,
                session_id="session_1",
                agent_name="root_agent",
                tool_call=LLMCompletionToolCall(id="call_1"),
                event_phase=RuntimeEventPhase.STARTED,
            )
        )
        end_event = await EventBus.publish(
            event_bus_id,
            ToolResultEvent(
                event_id=tool_result_event_id,
                session_id="session_1",
                agent_name="root_agent",
                tool_call=LLMCompletionToolCall(id="call_1"),
                result=ToolResult(content="tool done"),
                event_phase=RuntimeEventPhase.END,
            )
        )

        assert len(final_events) == 1
        assert final_events[0].duration_ms == end_event.created_ts_ms - started_event.created_ts_ms

    asyncio.run(run_test())


def test_result_collector_ignores_context_compression_events() -> None:
    """验证 collector 不把异步压缩观测事件写入 Runtime 输出。"""

    async def run_test() -> None:
        """执行异步压缩事件过滤验证。"""

        event_bus_id = EventBus.create_event_bus()
        collector = ResultCollector(event_bus_id=event_bus_id)
        EventBus.subscribe(
            event_bus_id,
            collector.collect,
            event_types=ResultCollector.listen_event_types(),
        )

        event_id = generate_prefixed_id("event")
        await EventBus.publish(event_bus_id, _build_input_event())
        await EventBus.publish(
            event_bus_id,
            ContextCompressionEvent(
                event_id=event_id,
                session_id="session_1",
                agent_name="root_agent",
                compression_mode=ContextCompressionMode.ASYNC,
                event_phase=RuntimeEventPhase.STARTED,
            )
        )
        await EventBus.publish(
            event_bus_id,
            ContextCompressionEvent(
                event_id=event_id,
                session_id="session_1",
                agent_name="root_agent",
                compression_mode=ContextCompressionMode.ASYNC,
                compression_result=ContextCompressionResult(
                    merged=True,
                    compressed_mid_term_memory="summary",
                    compressed_history_count=2,
                ),
                event_phase=RuntimeEventPhase.END,
            )
        )

        outputs = collector.get_outputs()

        assert outputs == []

    asyncio.run(run_test())


def test_result_collector_builds_sync_context_compression_output() -> None:
    """验证 collector 会把同步压缩事件写入 Runtime 输出。"""

    async def run_test() -> None:
        """执行异步同步压缩输出收集验证。"""

        event_bus_id = EventBus.create_event_bus()
        collector = ResultCollector(event_bus_id=event_bus_id)
        EventBus.subscribe(
            event_bus_id,
            collector.collect,
            event_types=ResultCollector.listen_event_types(),
        )

        event_id = generate_prefixed_id("event")
        await EventBus.publish(event_bus_id, _build_input_event())
        await EventBus.publish(
            event_bus_id,
            ContextCompressionEvent(
                event_id=event_id,
                session_id="session_1",
                agent_name="root_agent",
                compression_mode=ContextCompressionMode.SYNC,
                event_phase=RuntimeEventPhase.STARTED,
            )
        )
        await EventBus.publish(
            event_bus_id,
            ContextCompressionEvent(
                event_id=event_id,
                session_id="session_1",
                agent_name="root_agent",
                compression_mode=ContextCompressionMode.SYNC,
                compression_result=ContextCompressionResult(
                    merged=True,
                    compressed_mid_term_memory="summary",
                    compressed_history_count=2,
                ),
                event_phase=RuntimeEventPhase.END,
            )
        )

        outputs = collector.get_outputs()

        assert len(outputs) == 1
        assert outputs[0].input.input_id == "input_1"
        output_group = outputs[0].outputs
        assert len(output_group) == 1
        assert isinstance(output_group[0], ContextCompressionEvent)
        assert output_group[0].compression_mode == ContextCompressionMode.SYNC
        assert output_group[0].compression_result.merged is True
        assert output_group[0].compression_result.compressed_history_count == 2

    asyncio.run(run_test())


def test_result_collector_builds_sync_context_compression_output_from_final_event() -> None:
    """验证 collector 支持从单条 FINAL 同步压缩事件构建 Runtime 输出。"""

    async def run_test() -> None:
        """执行单条 FINAL 同步压缩输出收集验证。"""

        event_bus_id = EventBus.create_event_bus()
        collector = ResultCollector(event_bus_id=event_bus_id)
        EventBus.subscribe(
            event_bus_id,
            collector.collect,
            event_types=ResultCollector.listen_event_types(),
        )

        event_id = generate_prefixed_id("event")
        await EventBus.publish(event_bus_id, _build_input_event())
        await EventBus.publish(
            event_bus_id,
            ContextCompressionEvent(
                event_id=event_id,
                session_id="session_1",
                agent_name="root_agent",
                compression_mode=ContextCompressionMode.SYNC,
                compression_result=ContextCompressionResult(
                    merged=True,
                    compressed_mid_term_memory="summary",
                    compressed_history_count=2,
                ),
                event_phase=RuntimeEventPhase.FINAL,
            )
        )

        outputs = collector.get_outputs()

        assert len(outputs) == 1
        output_group = outputs[0].outputs
        assert len(output_group) == 1
        assert isinstance(output_group[0], ContextCompressionEvent)
        assert output_group[0].compression_mode == ContextCompressionMode.SYNC
        assert output_group[0].compression_result.merged is True
        assert output_group[0].compression_result.compressed_history_count == 2

    asyncio.run(run_test())


def test_runtime_run_result_holds_outputs() -> None:
    """验证 RuntimeRunResult 能持有稳定 FINAL 事件列表。"""

    run_result = RuntimeRunResult(
        session_id="session_1",
        run_id="run_1",
        event_bus_id="event_bus_1",
        outputs=[
            {
                "input": _build_input_event().input,
                "outputs": [
                    MessageEvent(
                        event_id="event_1",
                        session_id="session_1",
                        agent_name="root_agent",
                        sequence=1,
                        completion=_build_completion("hello"),
                        event_phase=RuntimeEventPhase.FINAL,
                    )
                ],
            }
        ],
    )

    assert isinstance(run_result.outputs[0].outputs[0], MessageEvent)
    assert run_result.outputs[0].outputs[0].completion.choices[0].message.content == "hello"


def test_runtime_run_result_restores_polymorphic_outputs() -> None:
    """验证 RuntimeRunResult 能从字典恢复多态 FINAL 事件和 state_events。"""

    run_result = RuntimeRunResult.model_validate(
        {
            "session_id": "session_1",
            "run_id": "run_1",
            "event_bus_id": "event_bus_1",
            "outputs": [
                {
                    "input": _build_input_event().input.model_dump(mode="json"),
                    "outputs": [
                        {
                            "type_name": "tool_result",
                            "event_id": "event_1",
                            "parent_event_id": "event_id-root",
                            "session_id": "session_1",
                            "agent_name": "root_agent",
                            "sequence": 1,
                            "event_phase": "final",
                                "tool_call": {
                                    "id": "call_1",
                                },
                            "result": {
                                "content": "tool done",
                            },
                            "state_events": [
                                {
                                    "type_name": "message",
                                    "event_id": "event_2",
                                    "parent_event_id": "event_1",
                                    "session_id": "session_1",
                                    "agent_name": "child_agent",
                                    "sequence": 2,
                                    "event_phase": "final",
                                    "completion": _build_completion("child output").model_dump(mode="json"),
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    assert isinstance(run_result.outputs[0].outputs[0], ToolResultEvent)
    assert isinstance(run_result.outputs[0].outputs[0].state_events[0], MessageEvent)
