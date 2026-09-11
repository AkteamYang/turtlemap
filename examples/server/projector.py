#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/02 18:26
# @Author  : YaHaoo
# @File    : projector.py

"""RuntimeEvent 到前端 ServerMessageEvent 的投影工具。"""

from __future__ import annotations

import json

from turtlemap.kernel.models import EventSource, Input, ObservableEvent
from turtlemap.kernel.models.enums import EventType
from turtlemap.kernel.models.models import (
    InterruptionResponsePayload,
    UserInputPayload,
)
from turtlemap.os.event_bus import (
    ContextCompressionEvent,
    ContextCompressionMode,
    InputEvent,
    InterruptedEvent,
    MessageEvent,
    RuntimeEvent,
    RuntimeEventPhase,
    ToolCallEvent,
    ToolResultEvent,
)
from turtlemap.os.llm.model import LLMCompletionToolCall, LLMCompletionUsage
from turtlemap.shared.ids import PREFIX_EVENT_ID, generate_prefixed_id
from turtlemap.shared.typing import ensure_instance

from .schemas import (
    CompleteData,
    ContextCompressionData,
    InputEventData,
    InterruptedEventData,
    JsonDict,
    MessageDeltaData,
    MessageFinalData,
    ServerMessageEvent,
    ServerMessageEventData,
    ServerMessageEventType,
    ToolResultStartData,
    ToolResultData,
    ToolCallData,
)
from .time_utils import now_ms


class RuntimeEventProjector:
    """负责把 Runtime 事件投影为前端协议的无状态工具类。

    约束:
        本类不维护 SSE id、不访问数据库、不运行 Agent；SSE id 分配由
        `RuntimeEventHandler` 在发送前统一处理。
    """

    @staticmethod
    def project_runtime_event(
        event: RuntimeEvent,
        run_id: str,
    ) -> list[ServerMessageEvent]:
        """把 RuntimeEvent 投影为前端 ServerMessageEvent。

        参数:
            event: 当前 Runtime 事件。
            run_id: 当前 agent 运行 id。
        返回:
            可发送给前端的一组业务事件。
        """

        # 用户输入只在 FINAL 阶段稳定后展示，避免 STARTED/END 阶段重复入流。
        if isinstance(event, InputEvent) and event.event_phase == RuntimeEventPhase.FINAL:
            return [
                RuntimeEventProjector._build_input_server_event(
                    event=event,
                    run_id=run_id,
                )
            ]

        # MessageEvent 同时承担流式增量和最终消息，由内部方法按阶段继续拆分。
        if isinstance(event, MessageEvent):
            return RuntimeEventProjector._build_message_server_events(
                event=event,
                run_id=run_id,
            )

        # tool_call 来自最终 ToolCall 聚合结果，前端据此创建工具标签。
        if isinstance(event, ToolCallEvent) and event.event_phase == RuntimeEventPhase.FINAL:
            return [
                RuntimeEventProjector._build_tool_call_server_event(
                    event=event,
                    run_id=run_id,
                )
            ]

        # 工具开始执行时先通知前端切换到执行中状态，最终结果仍在 FINAL 阶段发送。
        if isinstance(event, ToolResultEvent) and event.event_phase == RuntimeEventPhase.STARTED:
            return [
                RuntimeEventProjector._build_tool_result_start_server_event(
                    event=event,
                    run_id=run_id,
                )
            ]

        # tool_result 只投影 FINAL 阶段；没有结果时说明该事件还不能形成前端状态更新。
        if isinstance(event, ToolResultEvent) and event.event_phase == RuntimeEventPhase.FINAL:
            if event.result is None:
                return []
            return [
                RuntimeEventProjector._build_tool_result_server_event(
                    event=event,
                    run_id=run_id,
                )
            ]

        # 只有同步压缩会影响当前 SSE 生成，后台异步压缩不打断前端展示。
        if (
            isinstance(event, ContextCompressionEvent)
            and event.event_phase in {
                RuntimeEventPhase.STARTED,
                RuntimeEventPhase.FINAL,
            }
            and event.compression_mode == ContextCompressionMode.SYNC
        ):
            return [
                RuntimeEventProjector._build_context_compression_server_event(
                    event=event,
                    run_id=run_id,
                )
            ]

        # 中断事件是任务暂停时生成的稳定事实，只投影 FINAL 阶段。
        if isinstance(event, InterruptedEvent) and event.event_phase == RuntimeEventPhase.FINAL:
            return [
                RuntimeEventProjector._build_interrupted_server_event(
                    event=event,
                    run_id=run_id,
                )
            ]
        return []

    @staticmethod
    def build_complete_event(
        session_id: str,
        run_id: str,
        task_id: str | None,
        duration_ms: int,
    ) -> ServerMessageEvent:
        """构建生成完成业务事件。

        参数:
            session_id: 当前会话 id。
            run_id: 标识一次完整 agent 运行。
            task_id: 本次生成关联的当前输入任务 id；无稳定 history 时为空。
            duration_ms: 当前 agent 运行到发送 complete 的累计耗时。

        返回:
            前端 complete 事件，data 仅携带耗时。

        约束:
            complete 是业务 chunk，用于确认生成结束；外层 SSE end 只表示传输结束。
        """

        return ServerMessageEvent(
            type=ServerMessageEventType.COMPLETE,
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            event_id=generate_prefixed_id(PREFIX_EVENT_ID),
            parent_event_id=None,
            start_ts_ms=now_ms(),
            is_history_event=False,
            data=CompleteData(duration_ms=duration_ms),
        )

    @staticmethod
    def _build_message_server_events(
        event: MessageEvent,
        run_id: str,
    ) -> list[ServerMessageEvent]:
        """投影 MessageEvent 相关前端事件。

        参数:
            event: 当前 MessageEvent。
            run_id: 当前 agent 运行 id。
        返回:
            投影后的前端事件列表。
        """

        if event.event_phase == RuntimeEventPhase.IN_PROGRESS and event.chunk is not None:
            return RuntimeEventProjector._build_message_delta_server_events(
                event=event,
                run_id=run_id,
            )

        if event.event_phase == RuntimeEventPhase.FINAL and event.completion is not None:
            if not event.completion.choices:
                return []

            choice = event.completion.choices[0]
            message = choice.message
            return [
                RuntimeEventProjector._build_server_event(
                    event_type=ServerMessageEventType.MESSAGE_FINAL,
                    event=event,
                    run_id=run_id,
                    data=MessageFinalData(
                        duration_ms=event.duration_ms,
                        content=message.content or "",
                        reasoning_content=message.reasoning_content,
                        finish_reason=choice.finish_reason,
                        usage=RuntimeEventProjector._dump_usage(event.completion.usage),
                    ),
                )
            ]
        return []

    @staticmethod
    def _build_message_delta_server_events(
        event: MessageEvent,
        run_id: str,
    ) -> list[ServerMessageEvent]:
        """投影 MessageEvent 的流式增量事件。

        参数:
            event: 当前 MessageEvent，必须处于 in_progress 阶段。
            run_id: 当前 agent 运行 id。
        返回:
            文本增量事件列表。
        """

        if event.chunk is None:
            return []

        server_events: list[ServerMessageEvent] = []
        for choice in event.chunk.choices:
            if choice.delta.content is None:
                continue
            server_events.append(
                RuntimeEventProjector._build_server_event(
                    event_type=ServerMessageEventType.MESSAGE_DELTA,
                    event=event,
                    run_id=run_id,
                    data=MessageDeltaData(delta_content=choice.delta.content),
                )
            )
        return server_events

    @staticmethod
    def _build_input_server_event(
        event: InputEvent,
        run_id: str,
    ) -> ServerMessageEvent:
        """构建用户输入前端事件。

        参数:
            event: 当前输入 RuntimeEvent。
            run_id: 当前 agent 运行 id。
        返回:
            前端 input 事件。
        """

        user_input = RuntimeEventProjector._format_input(event.input)
        first_event = event.input.events[0]
        client_event_id = RuntimeEventProjector._extract_client_event_id(first_event)
        interruption_response = None

        # 中断恢复的响应由输入包首事件承载，按 event_type 收窄后完整透传。
        if first_event.event_type == EventType.INTERRUPTION_RESPONSE:
            interruption_response = ensure_instance(
                first_event.payload,
                InterruptionResponsePayload,
                value_name="中断恢复输入载荷",
            )
        return RuntimeEventProjector._build_server_event(
            event_type=ServerMessageEventType.INPUT,
            event=event,
            run_id=run_id,
            data=InputEventData(
                input_id=event.input.input_id,
                event_type=first_event.event_type,
                user_input=user_input,
                interruption_response=interruption_response,
                client_event_id=client_event_id,
            ),
        )

    @staticmethod
    def _build_tool_call_server_event(
        event: ToolCallEvent,
        run_id: str,
    ) -> ServerMessageEvent:
        """构建稳定工具选择前端事件。

        参数:
            event: 当前工具调用事件。
            run_id: 当前 agent 运行 id。
        返回:
            前端 tool_call 事件。
        """

        return RuntimeEventProjector._build_server_event(
            event_type=ServerMessageEventType.TOOL_CALL,
            event=event,
            run_id=run_id,
            data=RuntimeEventProjector._build_tool_call_data(event.tool_call),
        )

    @staticmethod
    def _build_tool_result_server_event(
        event: ToolResultEvent,
        run_id: str,
    ) -> ServerMessageEvent:
        """构建工具结果前端事件。

        参数:
            event: 当前工具结果事件，必须携带 result。
            run_id: 当前 agent 运行 id。
        返回:
            前端 tool_result 事件。
        """

        assert event.result is not None
        return RuntimeEventProjector._build_server_event(
            event_type=ServerMessageEventType.TOOL_RESULT,
            event=event,
            run_id=run_id,
            data=ToolResultData(
                duration_ms=event.duration_ms,
                tool_call_id=event.tool_call.id or "",
                status=event.result.status.value,
                content=event.result.content,
                raw_data=event.result.model_dump(),
            ),
        )

    @staticmethod
    def _build_tool_result_start_server_event(
        event: ToolResultEvent,
        run_id: str,
    ) -> ServerMessageEvent:
        """构建工具开始执行前端事件。

        参数:
            event: 当前工具结果 STARTED 事件。
            run_id: 当前 agent 运行 id。

        返回:
            携带完整工具调用信息的 tool_result_start 事件。
        """

        tool_call_data = RuntimeEventProjector._build_tool_call_data(event.tool_call)
        return RuntimeEventProjector._build_server_event(
            event_type=ServerMessageEventType.TOOL_RESULT_START,
            event=event,
            run_id=run_id,
            data=ToolResultStartData.model_validate(tool_call_data.model_dump()),
        )

    @staticmethod
    def _build_tool_call_data(
        tool_call: LLMCompletionToolCall,
    ) -> ToolCallData:
        """把 LLM 工具调用转换为对外协议载荷。

        参数:
            tool_call: 模型生成的完整工具调用。

        返回:
            含工具 id、名称及参数解析结果的标准 tool_call 载荷。
        """

        function = tool_call.function
        parameters_text = function.arguments if function else None
        parameters = RuntimeEventProjector._parse_json_dict(parameters_text)
        return ToolCallData(
            id=tool_call.id or "",
            name=function.name if function else None,
            parameters=parameters,
            parameters_text=parameters_text if parameters is None else None,
        )

    @staticmethod
    def _build_context_compression_server_event(
        event: ContextCompressionEvent,
        run_id: str,
    ) -> ServerMessageEvent:
        """构建上下文压缩前端事件。

        参数:
            event: 当前上下文压缩事件。
            run_id: 当前 agent 运行 id。
        返回:
            前端 context_compression 事件。
        """

        result = event.compression_result
        event_type = (
            ServerMessageEventType.CONTEXT_COMPRESSION_FINAL
            if event.event_phase == RuntimeEventPhase.FINAL
            else ServerMessageEventType.CONTEXT_COMPRESSION_START
        )
        return RuntimeEventProjector._build_server_event(
            event_type=event_type,
            event=event,
            run_id=run_id,
            data=ContextCompressionData(
                duration_ms=event.duration_ms,
                mode=event.compression_mode.value,
                merged=result.merged if result else False,
                level=result.level if result else None,
                success=result.success if result else False,
                error=result.error if result else "",
                compressed_history_count=(
                    result.compressed_history_count
                    if result is not None
                    else 0
                ),
                compressed_mid_term_memory=result.compressed_mid_term_memory if result else ""
            ),
        )

    @staticmethod
    def _build_interrupted_server_event(
        event: InterruptedEvent,
        run_id: str,
    ) -> ServerMessageEvent:
        """构建任务中断前端事件。

        参数:
            event: 当前 FINAL 阶段的中断 RuntimeEvent。
            run_id: 当前 agent 运行 id。
        返回:
            前端 interrupted 事件。
        """

        payload = event.payload
        return RuntimeEventProjector._build_server_event(
            event_type=ServerMessageEventType.INTERRUPTED,
            event=event,
            run_id=run_id,
            data=InterruptedEventData(
                request_id=payload.request_id,
                task_id=payload.task_id,
                interruption_type=event.interruption_type,
                reason=payload.reason,
                params=payload.params,
            ),
        )

    @staticmethod
    def _build_server_event(
        event_type: ServerMessageEventType,
        event: RuntimeEvent,
        run_id: str,
        data: ServerMessageEventData,
    ) -> ServerMessageEvent:
        """构建标准前端事件。

        参数:
            event_type: 前端事件类型枚举。
            event: 当前 RuntimeEvent。
            run_id: 当前 agent 运行 id。
            data: 前端事件载荷。
        返回:
            标准 ServerMessageEvent。
        """

        return ServerMessageEvent(
            type=event_type,
            session_id=event.session_id,
            run_id=run_id,
            task_id=event.task_id,
            event_id=event.event_id,
            parent_event_id=event.parent_event_id,
            start_ts_ms=now_ms(),
            is_history_event=event.is_history_event,
            data=data,
        )

    @staticmethod
    def _format_input(input_data: Input) -> str:
        """把 Runtime 输入包转换成用户可读文本。

        参数:
            input_data: Runtime 输入包。

        返回:
            输入包中的用户文本。
        """

        event = input_data.events[0]
        if isinstance(event.payload, UserInputPayload):
            return event.payload.content
        return ""

    @staticmethod
    def _extract_client_event_id(event: ObservableEvent) -> str | None:
        """从输入首事件中提取前端幂等 id。

        参数:
            event: Runtime 输入包中的首个 ObservableEvent。

        返回:
            用户输入事件的 source_id；中断恢复输入无需幂等 id，返回 None。

        异常:
            RuntimeError: 用户输入缺少前端幂等 id 时抛出。
        """

        if event.source_id:
            return event.source_id
        raise RuntimeError("缺少 client_event_id")

    @staticmethod
    def _parse_json_dict(value: str | None) -> JsonDict | None:
        """解析 JSON 字符串参数。

        参数:
            value: 待解析的 JSON 字符串。

        返回:
            字典参数；解析失败或非字典时返回 None。
        """

        if not value:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    @staticmethod
    def _dump_usage(usage: LLMCompletionUsage | None) -> JsonDict | None:
        """序列化 LLM usage。

        参数:
            usage: 模型调用用量对象。

        返回:
            JSON 字典；无 usage 时返回 None。
        """

        if usage is None:
            return None
        return usage.model_dump(mode="json")
