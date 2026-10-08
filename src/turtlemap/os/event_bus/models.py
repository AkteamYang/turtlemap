#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/15 13:51
# @Author  : YaHaoo
# @File    : models.py

"""os 层 Runtime 事件总线数据模型。"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from enum import Enum
from typing import Any, List, Union

from pydantic import BaseModel, Field, SerializeAsAny, field_validator

from turtlemap.kernel.models.enums import InterruptionRequestType, MessageRole
from turtlemap.kernel.models.models import Input, InterruptionRequest
from turtlemap.kernel.models.polymorphic import (
    BaseStateModel,
    PolymorphicStateModel,
)
from turtlemap.kernel.tool.models import ToolResult
from turtlemap.os.exceptions import OSRuntimeError
from turtlemap.os.llm.model import (
    LLMCompletion,
    LLMCompletionChoice,
    LLMCompletionChunk,
    LLMCompletionToolCall,
    LLMCompletionUsage,
    LLMFunction,
    LLMMessage,
)
from turtlemap.os.context.models import ContextCompressionResult
from turtlemap.os.tool.model import AgentFrameChange
from turtlemap.shared.ids import PREFIX_TOOL_CALL_ID, generate_prefixed_id
from turtlemap.shared.typing import ensure_instance

EventModel = Union[
    "InputEvent",
    "MessageEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "ContextCompressionEvent",
    "InterruptedEvent",
    "AgentFrameChangeEvent",
    "CustomEvent"
]


EVENT_BUS_ROOT_EVENT_ID = "event_id-root"


class RuntimeEventPhase(str, Enum):
    """Runtime 事件生命周期阶段。"""

    # 事件链路开始。
    STARTED = "started"

    # 事件链路处于过程推进中。
    IN_PROGRESS = "in_progress"

    # 事件链路中的局部内容覆盖快照。
    IN_PROGRESS_PARTIAL = "in_progress_partial"

    # 事件链路最后一个事件。
    END = "END"

    # 事件链路最终完整结果。
    FINAL = "final"


class RuntimeEventType(str, Enum):
    """Runtime 事件类型。"""

    # 自定义事件
    CUSTOM = "custom"

    # 用户输入事件。
    INPUT = "input"

    # assistant 普通文本事件。
    MESSAGE = "message"

    # 工具调用事件。
    TOOL_CALL = "tool_call"

    # 工具执行结果事件。
    TOOL_RESULT = "tool_result"

    # 上下文压缩事件。
    CONTEXT_COMPRESSION = "context_compression"

    # Runtime 任务暂停并等待外部响应的通知事件。
    INTERRUPTED = "interrupted"

    # Agent 控制权栈变更事件。
    AGENT_FRAME_CHANGE = "agent_frame_change"


class ContextCompressionMode(str, Enum):
    """上下文压缩执行模式。"""

    # 同步压缩，通常用于硬限制兜底，会阻塞当前上下文构建。
    SYNC = "sync"

    # 异步压缩，通常用于软限制后台治理，不阻塞当前请求。
    ASYNC = "async"


class RuntimeEvent(PolymorphicStateModel):
    """表示 event bus 传输的 Runtime 事件基类。

    说明:
        `type_name` 是多态恢复使用的稳定类型标识；运行期通过 `event_type`
        属性读取对应枚举语义。`sequence` 由 event bus 在发送时覆盖分配，
        发送方不应自行维护顺序。
    """

    # 同一逻辑事件链路的稳定标识。
    event_id: str

    # 父级事件 id；为空表示当前事件链对应顶层输出。
    parent_event_id: str | None = None

    # 当前会话标识。
    session_id: str

    # 当前事件所属 Agent 名称。
    agent_name: str | None = None

    # 当前事件所属 Agent 的对外展示名称。
    agent_display_name: str | None = None

    # 当前事件关联的任务标识。
    task_id: str | None = None

    # event bus 自动分配的全局递增序号。
    sequence: int = 0

    # event bus 发布当前事件的毫秒时间戳。
    created_ts_ms: int = -1

    # 当前事件链路从 STARTED 到 END 的耗时；FINAL 阶段可由 ResultCollector 计算。
    duration_ms: int = 0

    # 当前事件生命周期阶段。
    event_phase: RuntimeEventPhase = RuntimeEventPhase.STARTED

    # 中断前的历史事件
    is_history_event_for_interruption: bool = Field(default=False)

    # 是否是重放事件，重放时会赋值，不持久化
    is_replay_event: bool = Field(default=False, exclude=True)

    @property
    def event_type(self) -> RuntimeEventType:
        """返回当前事件类型枚举。

        返回:
            由 `type_name` 翻译得到的事件类型。
        """

        return RuntimeEventType(self.type_name)

    @staticmethod
    def validate_parent_event_id(events: Sequence["RuntimeEvent"]) -> None:
        """校验同一事件链路中的父事件 id 保持一致。

        参数:
            events: 同一 event_id 下的事件序列。

        返回:
            无返回值；父事件 id 不一致时抛出异常。
        """

        parent_event_ids = {event.parent_event_id for event in events}
        if len(parent_event_ids) > 1:
            raise OSRuntimeError(
                f"同一 event_id 存在多个 parent_event_id, events={events}"
            )

    @staticmethod
    def validate_stream_phases(events: Sequence["RuntimeEvent"]) -> None:
        """校验流式事件序列必须符合 STARTED -> 过程事件* -> END。

        参数:
            events: 同一 event_id 下的事件序列。

        返回:
            无返回值；事件阶段不满足流式约束时抛出异常。
        """

        if events[0].event_phase != RuntimeEventPhase.STARTED:
            raise OSRuntimeError(f"stream event 首事件非 STARTED, event {events[0]}")
        if events[-1].event_phase != RuntimeEventPhase.END:
            raise OSRuntimeError(f"stream event 尾事件非 END, event {events[-1]}")
        for event in events[1:-1]:
            if event.event_phase not in {
                RuntimeEventPhase.IN_PROGRESS,
                RuntimeEventPhase.IN_PROGRESS_PARTIAL,
            }:
                raise OSRuntimeError(
                    f"stream event 中间事件不是过程事件, event={event}"
                )

    @staticmethod
    def calculate_duration_ms(events: Sequence["RuntimeEvent"]) -> int:
        """计算同一事件链路首尾事件间的持续时间。

        参数:
            events: 同一 event_id 下按任意顺序收集的事件序列。

        返回:
            首尾事件发布时间戳差值，单位毫秒；时间缺失或倒退时返回 0。
        """

        if len(events) < 2:
            return 0

        sorted_events = sorted(events, key=lambda item: item.sequence)
        start_event = sorted_events[0]
        end_event = sorted_events[-1]
        if not start_event.created_ts_ms or not end_event.created_ts_ms:
            return 0
        return max(end_event.created_ts_ms - start_event.created_ts_ms, 0)


class RunntimeEventBuffer(BaseModel):
    """表示 event bus 事件流的可序列化缓冲区。

    说明:
        该模型用于把一组多态 RuntimeEvent 作为整体保存或传递。
        反序列化时会根据每个事件的 `type_name` 恢复成真实事件子类，
        避免调用方拿到裸 dict 后丢失事件类型能力。
    """

    # Event bus 已发送或待恢复的事件流，按原始顺序保存。
    events: List[SerializeAsAny[RuntimeEvent]] = Field(default_factory=list)

    # 是否已重放，一次 run 中断后重运行时会先重放历史产物，首次从持久化恢复，这个值为False，runtime需要flush
    did_replay: bool = Field(default=False, exclude=True)

    @field_validator("events", mode="before")
    @classmethod
    def _validate_events(cls, value: Any) -> Any:
        """恢复缓冲区中的多态 RuntimeEvent 列表。

        参数:
            value: pydantic 校验前的事件列表原始值。

        返回:
            若输入是事件 dict 列表，则按 `type_name` 恢复为具体事件子类；
            其他输入保持原值交给 pydantic 后续校验。
        """

        if isinstance(value, list):
            return [
                RuntimeEvent.validate_polymorphic(item)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        return value


@RuntimeEvent.register_type
class InputEvent(RuntimeEvent):
    """表示 Runtime 接收到的输入事件。

    说明:
        InputEvent 用于在 event bus 中观测一次输入包进入 Runtime 的事实。
        输入本身由 kernel `Input` 模型承载，collector 当前不将输入事件聚合为
        RuntimeRunResult 输出。
    """

    # 当前事件固定为输入事件。
    type_name: str = Field(default=RuntimeEventType.INPUT)

    # Runtime 接收到的标准输入包。
    input: Input

    # 输入事件是稳定事实，默认以 FINAL 形态发送。
    event_phase: RuntimeEventPhase = RuntimeEventPhase.FINAL


@RuntimeEvent.register_type
class MessageEvent(RuntimeEvent):
    """表示 assistant 普通文本事件。

    说明:
        `event_phase` 为 `IN_PROGRESS` 时，`content` 表示流式片段；
        `event_phase` 为 `COMPLETED` 时，`content` 表示稳定完整内容。
    """

    # 当前事件固定为普通文本事件。
    type_name: str = Field(default=RuntimeEventType.MESSAGE)

    # 流式输出内容, 非FINAL状态有值
    chunk: LLMCompletionChunk | None = None

    # 完整结果，FINAL状态是有值
    completion: LLMCompletion | None = None

    # 普通文本事件默认作为过程事件发送。
    event_phase: RuntimeEventPhase = RuntimeEventPhase.IN_PROGRESS

    @classmethod
    def create_final_event_from_events(
        cls,
        events: list["MessageEvent"],
    ) -> "MessageEvent":
        """根据同一 message 事件链构建携带完整 completion 的 FINAL 事件。

        参数:
            events: 同一 message event_id 下收集到的事件序列。

        返回:
            保留起始事件标识并携带完整 completion 的 FINAL `MessageEvent`。
        """

        if not events:
            raise OSRuntimeError("创建 message FINAL 事件时 events 为空")

        sorted_events = sorted(events, key=lambda item: item.sequence)
        cls.validate_parent_event_id(sorted_events)
        final_event = sorted_events[-1]
        if final_event.event_phase == RuntimeEventPhase.FINAL:
            if final_event.completion is None:
                raise OSRuntimeError(f"message FINAL 事件没有 completion, event={final_event}")
            return deepcopy(final_event)

        cls.validate_stream_phases(sorted_events)
        idx2choice: dict[int, LLMCompletionChoice] = {}
        choice_idx2tool_idx2tool: dict[int, dict[int, LLMCompletionToolCall]] = {}
        completion_id = ""
        created = 0
        model = ""
        usage: LLMCompletionUsage | None = None

        for item in sorted_events:
            # STARTED / END 只描述生命周期，实际消息内容只存在于过程事件。
            if item.event_phase in {RuntimeEventPhase.STARTED, RuntimeEventPhase.END}:
                continue
            if item.chunk is None:
                raise OSRuntimeError(
                    f"IN_PROGRESS 状态的 message event 缺少 chunk, event={item}"
                )

            if item.chunk.id and not completion_id:
                completion_id = item.chunk.id
            if item.chunk.model and not model:
                model = item.chunk.model
            if item.chunk.created > created:
                created = item.chunk.created
            if item.chunk.usage and usage is None:
                usage = item.chunk.usage

            for choice in item.chunk.choices:
                completion_choice = idx2choice.setdefault(
                    choice.index,
                    LLMCompletionChoice(
                        index=choice.index,
                        message=LLMMessage(role=MessageRole.ASSISTANT),
                    ),
                )
                tool_idx2tool = choice_idx2tool_idx2tool.setdefault(choice.index, {})

                if choice.finish_reason:
                    completion_choice.finish_reason = choice.finish_reason
                if choice.delta.runtime_params:
                    completion_choice.message.runtime_params.update(
                        choice.delta.runtime_params
                    )
                if choice.delta.role:
                    completion_choice.message.role = choice.delta.role

                is_partial = item.event_phase == RuntimeEventPhase.IN_PROGRESS_PARTIAL
                if is_partial:

                    # PARTIAL 只覆盖 interceptor 显式改写的派生通道；None 表示保持原值。
                    if choice.delta.display_content is not None:
                        completion_choice.message.display_content = (
                            choice.delta.display_content
                        )
                    if choice.delta.display_reasoning_content is not None:
                        completion_choice.message.display_reasoning_content = (
                            choice.delta.display_reasoning_content
                        )
                    if choice.delta.context_content is not None:
                        completion_choice.message.context_content = (
                            choice.delta.context_content
                        )
                    if choice.delta.context_reasoning_content is not None:
                        completion_choice.message.context_reasoning_content = (
                            choice.delta.context_reasoning_content
                        )
                else:

                    # 普通过程事件是增量，原始、展示和上下文三条通道都按增量追加。
                    display_content = choice.delta.display_content
                    if display_content is None:
                        display_content = choice.delta.content
                    display_reasoning_content = choice.delta.display_reasoning_content
                    if display_reasoning_content is None:
                        display_reasoning_content = choice.delta.reasoning_content
                    context_content = choice.delta.context_content
                    if context_content is None:
                        context_content = choice.delta.content
                    context_reasoning_content = choice.delta.context_reasoning_content
                    if context_reasoning_content is None:
                        context_reasoning_content = choice.delta.reasoning_content
                    completion_choice.message.content = cls._merge_message_content(
                        completion_choice.message.content,
                        choice.delta.content,
                    )
                    completion_choice.message.reasoning_content = cls._merge_message_content(
                        completion_choice.message.reasoning_content,
                        choice.delta.reasoning_content,
                    )
                    completion_choice.message.display_content = cls._merge_message_content(
                        completion_choice.message.display_content,
                        display_content,
                    )
                    completion_choice.message.display_reasoning_content = cls._merge_message_content(
                        completion_choice.message.display_reasoning_content,
                        display_reasoning_content,
                    )
                    completion_choice.message.context_content = cls._merge_message_content(
                        completion_choice.message.context_content,
                        context_content,
                    )
                    completion_choice.message.context_reasoning_content = cls._merge_message_content(
                        completion_choice.message.context_reasoning_content,
                        context_reasoning_content,
                    )

                for tool_call in choice.delta.tool_calls or []:
                    completion_tool_call = tool_idx2tool.setdefault(
                        tool_call.index,
                        LLMCompletionToolCall(id=generate_prefixed_id(PREFIX_TOOL_CALL_ID), function=LLMFunction()),
                    )
                    if tool_call.id and completion_tool_call.id.startswith(PREFIX_TOOL_CALL_ID):
                        completion_tool_call.id = tool_call.id
                    if tool_call.type and not completion_tool_call.type:
                        completion_tool_call.type = tool_call.type
                    if tool_call.function:
                        if completion_tool_call.function is None:
                            completion_tool_call.function = LLMFunction()
                        if (
                            tool_call.function.name
                            and not completion_tool_call.function.name
                        ):
                            completion_tool_call.function.name = tool_call.function.name
                        if tool_call.function.arguments:
                            completion_tool_call.function.arguments = (
                                (completion_tool_call.function.arguments or "")
                                + tool_call.function.arguments
                            )

        choices: list[LLMCompletionChoice] = []
        for choice_index in sorted(idx2choice):
            choice = idx2choice[choice_index]
            for tool_index in sorted(choice_idx2tool_idx2tool[choice_index]):
                tool = choice_idx2tool_idx2tool[choice_index][tool_index]
                choice.message.tool_calls.append(tool)

            # 派生通道与原始内容一致时无需重复存储，读取侧会回退到原始字段。
            message = choice.message
            if message.display_content == message.content:
                message.display_content = None
            if message.display_reasoning_content == message.reasoning_content:
                message.display_reasoning_content = None
            if message.context_content == message.content:
                message.context_content = None
            if message.context_reasoning_content == message.reasoning_content:
                message.context_reasoning_content = None
            choices.append(choice)

        final_event = deepcopy(ensure_instance(sorted_events[0], MessageEvent))
        final_event.event_phase = RuntimeEventPhase.FINAL
        final_event.chunk = None
        final_event.completion = LLMCompletion(
            id=completion_id,
            choices=choices,
            created=created,
            model=model,
            usage=usage,
        )
        final_event.duration_ms = cls.calculate_duration_ms(sorted_events)
        return final_event

    @staticmethod
    def _merge_message_content(
        accumulated_content: str | None,
        delta_content: str | None,
    ) -> str | None:
        """将普通过程事件的文本增量合并到已有消息内容。

        参数:
            accumulated_content: 当前已聚合的完整内容。
            delta_content: 当前过程事件携带的文本增量。

        返回:
            当前增量为空时保留已有内容；否则返回追加后的完整内容。

        说明:
            该方法只服务普通 `IN_PROGRESS` 的追加语义。`IN_PROGRESS_PARTIAL` 是完整
            快照，必须在主流程中直接覆盖，不可调用此方法。
        """

        if delta_content is None:
            return accumulated_content
        return (accumulated_content or "") + delta_content


@RuntimeEvent.register_type
class ToolCallEvent(RuntimeEvent):
    """表示模型生成的工具调用事件。"""

    # 当前事件固定为工具调用事件。
    type_name: str = Field(default=RuntimeEventType.TOOL_CALL)

    # 模型生成的完整工具调用信息。
    tool_call: LLMCompletionToolCall

    # 工具调用事件在模型输出稳定后发送。
    event_phase: RuntimeEventPhase = RuntimeEventPhase.FINAL

    @classmethod
    def create_final_event_from_events(
        cls,
        events: list["ToolCallEvent"],
    ) -> "ToolCallEvent":
        """校验并复制单条稳定工具调用 FINAL 事件。

        参数:
            events: 同一 tool call event_id 下收集到的事件序列。

        返回:
            可作为稳定输出消费的 FINAL `ToolCallEvent`。
        """

        if len(events) != 1:
            raise OSRuntimeError(f"tool_call FINAL 事件数量异常: event_count={len(events)}")

        final_event = deepcopy(ensure_instance(events[0], ToolCallEvent))
        if final_event.event_phase != RuntimeEventPhase.FINAL:
            raise OSRuntimeError(f"tool_call 不是 FINAL 事件: event={final_event}")
        return final_event


@RuntimeEvent.register_type
class ToolResultEvent(RuntimeEvent):
    """表示工具执行结果事件。"""

    # 当前事件固定为工具结果事件。
    type_name: str = Field(default=RuntimeEventType.TOOL_RESULT)

    # 模型生成的完整工具调用信息。
    tool_call: LLMCompletionToolCall

    # 工具执行形成的稳定结果；END 阶段必须携带。
    result: ToolResult | None = None

    # 工具执行期间产生的子事件树；仅 FINAL 事件携带，子项同样是 FINAL 事件。
    state_events: list[SerializeAsAny[RuntimeEvent]] = Field(default_factory=list)

    # 工具结果统一走 STARTED -> END 流式生命周期。
    event_phase: RuntimeEventPhase = RuntimeEventPhase.IN_PROGRESS

    @field_validator("state_events", mode="before")
    @classmethod
    def _validate_state_events(cls, value: object) -> object:
        """恢复工具结果 FINAL 事件中嵌套的多态子事件。

        参数:
            value: Pydantic 校验前的子事件列表原始值。

        返回:
            已按 `type_name` 恢复的具体 RuntimeEvent 子类列表，或原值。
        """

        if isinstance(value, list):
            return [
                RuntimeEvent.validate_polymorphic(item)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        return value

    @classmethod
    def create_final_event_from_events(
        cls,
        events: list["ToolResultEvent"],
    ) -> "ToolResultEvent":
        """根据同一工具结果事件链构建携带稳定结果的 FINAL 事件。

        参数:
            events: 同一 tool result event_id 下收集到的事件序列。

        返回:
            保留起始事件标识、携带工具结果的 FINAL `ToolResultEvent`。
        """

        if not events:
            raise OSRuntimeError("创建 tool_result FINAL 事件时 events 为空")

        sorted_events = sorted(events, key=lambda item: item.sequence)
        cls.validate_parent_event_id(sorted_events)
        final_event = sorted_events[-1]
        if final_event.event_phase == RuntimeEventPhase.FINAL:
            if final_event.result is None:
                raise OSRuntimeError(
                    f"tool_result FINAL 事件没有 result, event={final_event}"
                )
            return deepcopy(final_event)

        cls.validate_stream_phases(sorted_events)
        end_event = ensure_instance(sorted_events[-1], ToolResultEvent)
        if end_event.result is None:
            raise OSRuntimeError(f"tool_result END 事件没有 result, event={end_event}")

        final_event = deepcopy(ensure_instance(sorted_events[0], ToolResultEvent))
        final_event.event_phase = RuntimeEventPhase.FINAL
        final_event.result = end_event.result
        final_event.duration_ms = cls.calculate_duration_ms(sorted_events)
        return final_event


@RuntimeEvent.register_type
class ContextCompressionEvent(RuntimeEvent):
    """表示上下文压缩生命周期事件。

    说明:
        压缩事件统一使用 STARTED -> END 生命周期。STARTED 表示压缩开始，
        END 表示压缩结束并携带 `compression_result`。
    """

    # 当前事件固定为上下文压缩事件。
    type_name: str = Field(default=RuntimeEventType.CONTEXT_COMPRESSION)

    # 当前压缩执行模式，区分同步硬限制压缩与异步后台压缩。
    compression_mode: ContextCompressionMode

    # 压缩结束后的结果；END 阶段必须携带。
    compression_result: ContextCompressionResult | None = None

    # 压缩事件统一走 STARTED -> END 生命周期。
    event_phase: RuntimeEventPhase = RuntimeEventPhase.STARTED

    @classmethod
    def create_final_event_from_events(
        cls,
        events: list["ContextCompressionEvent"],
    ) -> "ContextCompressionEvent":
        """根据同步压缩事件链构建携带压缩结果的 FINAL 事件。

        参数:
            events: 同一 context compression event_id 下收集到的事件序列。

        返回:
            保留起始事件标识、携带压缩结果的 FINAL `ContextCompressionEvent`。
        """

        if not events:
            raise OSRuntimeError("创建 context_compression FINAL 事件时 events 为空")

        sorted_events = sorted(events, key=lambda item: item.sequence)
        cls.validate_parent_event_id(sorted_events)
        start_event = ensure_instance(sorted_events[0], ContextCompressionEvent)
        if start_event.compression_mode != ContextCompressionMode.SYNC:
            raise OSRuntimeError(
                f"context_compression FINAL 事件只支持同步压缩, event={start_event}"
            )

        final_event = sorted_events[-1]
        if final_event.event_phase == RuntimeEventPhase.FINAL:
            if final_event.compression_result is None:
                raise OSRuntimeError(
                    f"context_compression FINAL 事件没有 compression_result, event={final_event}"
                )
            return deepcopy(final_event)

        cls.validate_stream_phases(sorted_events)
        end_event = ensure_instance(sorted_events[-1], ContextCompressionEvent)
        if end_event.compression_result is None:
            raise OSRuntimeError(
                f"context_compression END 事件没有 compression_result, event={end_event}"
            )

        final_event = deepcopy(start_event)
        final_event.event_phase = RuntimeEventPhase.FINAL
        final_event.compression_result = end_event.compression_result
        final_event.duration_ms = cls.calculate_duration_ms(sorted_events)
        return final_event


@RuntimeEvent.register_type
class InterruptedEvent(RuntimeEvent):
    """表示任务已暂停并等待外部 response 的通知事件。"""

    # 当前事件固定为任务中断事件。
    type_name: str = Field(default=RuntimeEventType.INTERRUPTED)

    # 当前中断请求类型，与 payload.type 使用同一套稳定枚举。
    interruption_type: InterruptionRequestType

    # 当前中断请求，与暂停任务持有的 request 为同一份状态事实。
    payload: InterruptionRequest

    # 中断通知是稳定事实，固定以 FINAL 形态发送。
    event_phase: RuntimeEventPhase = RuntimeEventPhase.FINAL

    @classmethod
    def create_final_event_from_events(
        cls,
        events: list["InterruptedEvent"],
    ) -> "InterruptedEvent":
        """校验并复制单条稳定工具调用 FINAL 事件。

        参数:
            events: 同一 tool call event_id 下收集到的事件序列。

        返回:
            可作为稳定输出消费的 FINAL `InterruptedEvent`。
        """

        if len(events) != 1:
            raise OSRuntimeError(f"interrupt FINAL 事件数量异常: event_count={len(events)}")

        final_event = deepcopy(ensure_instance(events[0], InterruptedEvent))
        if final_event.event_phase != RuntimeEventPhase.FINAL:
            raise OSRuntimeError(f"interrupt 不是 FINAL 事件: event={final_event}")
        return final_event


@RuntimeEvent.register_type
class AgentFrameChangeEvent(RuntimeEvent):
    """表示 Agent 控制权栈发生变化的稳定通知事件。"""

    # 当前事件固定为 Agent 控制权栈变更事件。
    type_name: str = Field(default=RuntimeEventType.AGENT_FRAME_CHANGE)

    # 控制权栈变更是稳定事实，固定以 FINAL 形态发送。
    event_phase: RuntimeEventPhase = RuntimeEventPhase.FINAL

    # 当前 Agent 控制权栈变更的完整描述。
    payload: AgentFrameChange


@RuntimeEvent.register_type
class CustomEvent(RuntimeEvent):
    """自定义事件
    """

    # 当前事件固定为上下文压缩事件。
    type_name: str = Field(default=RuntimeEventType.CUSTOM)

    # 自定义事件载荷
    payload: dict = Field(default={})


class TaskOutput(BaseStateModel):
    """表示一次任务输入及其产生的稳定输出集合。

    说明:
        一次 Runtime run 可能连续消费多个输入并推进多个任务。TaskOutput 将
        单个任务的起始输入和对应 FINAL RuntimeEvent 绑定在一起，避免业务层再从
        扁平输出数组中按 task_id 反向推断。
    """

    # 当前任务开始时对应的输入包。
    input: Input

    # 当前输入任务产生的顶层 FINAL 事件，按起始事件 sequence 排序。
    outputs: Sequence[SerializeAsAny[RuntimeEvent]] = Field(default_factory=list)

    @field_validator("outputs", mode="before")
    @classmethod
    def _validate_outputs(cls, value: object) -> object:
        """恢复任务输出中的多态 FINAL RuntimeEvent 列表。

        参数:
            value: Pydantic 校验前的原始输出列表。

        返回:
            已按 `type_name` 恢复的具体事件列表，或原值。
        """

        if isinstance(value, list):
            return [
                RuntimeEvent.validate_polymorphic(item)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        return value


class RuntimeRunResult(BaseStateModel):
    """表示一次 os Runtime run 调用的稳定返回结果。

    说明:
        该对象面向调用方消费，不承载完整 SessionState。`outputs` 按任务
        输入组织，每个 TaskOutput 内部再按对应起始事件的 sequence 排序。
    """

    # 当前会话标识。
    session_id: str

    # run_id
    run_id: str

    # 本次 run 对应的事件通道标识。
    event_bus_id: str

    # 本次 run 形成的任务输出列表，每个元素包含任务输入和对应稳定输出。
    outputs: Sequence[TaskOutput] = Field(default_factory=list)
    
