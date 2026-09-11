#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/16 16:06
# @Author  : YaHaoo
# @File    : main.py

"""基于 Runtime + event bus 的天气工具交互 demo。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field
from rich.console import Console

from turtlemap import TurtleMapConfig
from turtlemap.kernel.models import Input, ObservableEvent, SystemDefinition, UserInputPayload
from turtlemap.kernel.models.enums import EventSource, EventType, InterruptionRequestType
from turtlemap.kernel.models.models import InterruptionAsyncToolResponse, InterruptionExceptionResponse, InterruptionRequest, InterruptionResponsePayload
from turtlemap.kernel.tool import ToolDescriptor, ToolResult, tool
from turtlemap.os.agent import Agent
from turtlemap.os.context.models import SystemInstruction
from turtlemap.os.event_bus import EventBus, RuntimeRunResult
from turtlemap.os.event_bus.models import (
    ContextCompressionEvent,
    InputEvent,
    InterruptedEvent,
    MessageEvent,
    RuntimeEvent,
    RuntimeEventPhase,
    ToolCallEvent,
    ToolResultEvent,
)
from turtlemap.os.llm.model import LLMCompletionUsage
from turtlemap.os.runtime import Runtime
from turtlemap.os.tool.model import ToolInputContext
from turtlemap.shared.ids import PREFIX_EVENT_ID, PREFIX_OBSERVABLE_EVENT_ID, PREFIX_SESSION_ID, generate_prefixed_id
from turtlemap.shared.logger import Logger
from turtlemap.shared.typing import ensure_instance

from mysql_store import MysqlConfig, MySQLStateStore, init_mysql

config = TurtleMapConfig.from_env(Path(__file__).parent.parent / ".env")
mysql_config = MysqlConfig.from_env(Path(__file__).parent.parent / ".env")
console = Console(highlight=False)

STYLE_STREAM_EVENT = "grey50"
STYLE_FINAL_EVENT = "green"
STYLE_INPUT_EVENT = "magenta"
STYLE_TOOL_CALL_EVENT = "yellow"
STYLE_TOOL_RESULT_EVENT = "orange3"
STYLE_CONTEXT_COMPRESSION_EVENT = "blue"
STYLE_INTERRUPTED_EVENT = "red"
STYLE_RUN_RESULT = "white"


class WeatherQueryInput(BaseModel):

    # 待查询天气的城市名称。
    city: str = Field(description="城市名")


@tool(
    descriptor=ToolDescriptor(
        name="query_weight",
        capability="查询指定城市的 mock 天气数据。",
        use_cases=[],
        anti_use_cases=[]
    ),
    input_model=WeatherQueryInput,
    requires_confirmation=True
)
def query_weather(input_model: WeatherQueryInput) -> ToolResult:
    """返回指定城市的 mock 天气数据。

    参数:
        input_model: 已由 ToolService 校验并绑定上下文的工具输入模型。

    返回:
        可回传给 LLM 的标准工具结果。
    """

    tool_context = ToolInputContext.get_input_context(input_model)
    event_bus_id = tool_context.event_bus_id if tool_context else ""
    city = input_model.city.strip() or "未知城市"
    return ToolResult(
        # content=f"{city} 当前晴，26 摄氏度，东北风 2 级，湿度 48%。这是 mock 天气数据。",
        content=f"{city} 抱歉暂时没有数据。",
        purpose="provide real-time weather information",
        raw_data={
            "city": city,
            "weather": "sunny",
            "temperature_celsius": 26,
            "wind": "东北风 2 级",
            "humidity": "48%",
            "event_bus_id": event_bus_id,
            "mock": True,
        },
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10)
    )


async def print_runtime_event(event: RuntimeEvent) -> None:
    """打印 event bus 中流过的 Runtime 事件。

    参数:
        event: 当前 event bus 通知的运行时事件。

    返回:
        无返回值。
    """

    prefix = (
        f"[event #{event.sequence}] "
        f"type={event.event_type.value} "
        f"phase={event.event_phase.value} "
        f"id={event.event_id} "
        f"parent={event.parent_event_id}"
    )
    if isinstance(event, MessageEvent):
        _print_message_event(prefix, event)
        return
    if isinstance(event, InputEvent):
        _print_input_event(prefix, event)
        return
    if isinstance(event, ToolCallEvent):
        _print_tool_call_event(prefix, event)
        return
    if isinstance(event, ToolResultEvent):
        _print_tool_result_event(prefix, event)
        return
    if isinstance(event, ContextCompressionEvent):
        _print_context_compression_event(prefix, event)
        return
    if isinstance(event, InterruptedEvent):
        _print_interrupted_event(prefix, event)
        return
    _print_event_line(prefix, event)


async def main() -> None:
    """启动一个可反复输入的天气工具 Runtime demo。

    返回:
        无返回值。
    """
    await init_mysql(mysql_config)
    state_store = MySQLStateStore()
    runtime = Runtime(
        root_agent=_build_weather_agent(),
        state_store=state_store
    )
    listener_id = EventBus.subscribe(runtime.event_bus_id, print_runtime_event)
    session_id: str = "session_id:ce0ed8c3-ef9c-4b32-933b-078731929496"
    session_state = await state_store.load_session_state(session_id=session_id, schema_version=4)
    Logger.logger.info("天气工具 demo 已启动，输入 exit / quit / 退出 可结束。")
    try:
        while True:
            user_text = (await asyncio.to_thread(input, "\n你> ")).strip()
            if not user_text:
                continue
            if user_text.lower() in {"exit", "quit"} or user_text == "退出":
                break

            await runtime.init_session(session_state)
            # 输入
            event = _build_user_input_event(user_text)

            # 中断测试
            # event = ObservableEvent(
            #     event_id=generate_prefixed_id(PREFIX_OBSERVABLE_EVENT_ID),
            #     event_type=EventType.INTERRUPTION_RESPONSE,
            #     source=EventSource.USER,
            #     payload=InterruptionResponsePayload(
            #         request_id="interruption_request_id:6854a4c1-0524-4e49-916d-f2f4c373bd5c",
            #         request_type=InterruptionRequestType.EXCEPTION_RESUME,
            #         response=InterruptionExceptionResponse()
            #     ),
            # )

            event = ObservableEvent(
                event_id=generate_prefixed_id(PREFIX_OBSERVABLE_EVENT_ID),
                event_type=EventType.INTERRUPTION_RESPONSE,
                source=EventSource.USER,
                payload=InterruptionResponsePayload(
                    request_id="interruption_request_id:2c470095-5a78-4212-ad1d-feeddd563aa2",
                    request_type=InterruptionRequestType.ASYNC_TOOL_REQUEST,
                    response=InterruptionAsyncToolResponse(data={"option": "approve"})
                ),
            )

            result = await runtime.run(
                input_events=[event],
            )
            session_id = result.session_id
            session_state = runtime.real_session_state
            _print_run_result(result)
    finally:
        EventBus.unsubscribe(runtime.event_bus_id, listener_id)


def _build_weather_agent() -> Agent:
    """构建带 mock 天气工具的根 Agent。

    返回:
        已装配天气工具和默认模型配置的 os Agent。
    """

    return Agent(
        agent_name="weather_agent",
        system=SystemInstruction(
            role="你是一个简洁可靠的通用助手。",
            objective=(
                "理解用户目标并将结果整理成自然、清晰的回答。"
            ),
            constraints=(
                "回答风格应当是拟人的，符合人类用语习惯（追求的标准），"
                "因此回答中不得出现类似：调用工具、上下文检索/搜索、读取记忆等体现系统内部过程的描述"
            ),
            input_format="用户会用自然语言提出问题或任务。",
            output_format="用中文给出简洁回答。",
        ),
        config=config,
        tools=[query_weather],
    )

def _build_user_input_event(content: str) -> ObservableEvent:
    """把控制台文本包装成 Runtime 可接收的用户输入事件。

    参数:
        content: 用户在控制台输入的原始文本。

    返回:
        标准 ObservableEvent。
    """

    return ObservableEvent(
        event_id=generate_prefixed_id(PREFIX_OBSERVABLE_EVENT_ID),
        event_type=EventType.USER_INPUT,
        source=EventSource.USER,
        payload=UserInputPayload(content=content),
    )


def _print_event_line(text: str, event: RuntimeEvent) -> None:
    """按事件阶段打印带颜色的 event bus 观测日志。

    参数:
        text: 已格式化好的事件日志文本。
        event: 当前事件对象，用于判断输出颜色。

    返回:
        无返回值。
    """

    style = _get_event_style(event)
    console.print(f"{_format_log_timestamp()} {text}", style=style, markup=False)


def _get_event_style(event: RuntimeEvent) -> str:
    """根据事件类型和阶段返回控制台显示样式。

    参数:
        event: 当前待打印的 Runtime 事件。

    返回:
        rich console 可识别的样式字符串。
    """

    if isinstance(event, InputEvent):
        return STYLE_INPUT_EVENT
    if isinstance(event, ToolCallEvent):
        return STYLE_TOOL_CALL_EVENT
    if isinstance(event, ToolResultEvent):
        return (
            STYLE_TOOL_RESULT_EVENT
            if event.event_phase == RuntimeEventPhase.FINAL
            else STYLE_STREAM_EVENT
        )
    if isinstance(event, ContextCompressionEvent):
        return STYLE_CONTEXT_COMPRESSION_EVENT
    if isinstance(event, InterruptedEvent):
        return STYLE_INTERRUPTED_EVENT
    if event.event_phase == RuntimeEventPhase.FINAL:
        return STYLE_FINAL_EVENT
    return STYLE_STREAM_EVENT


def _print_result_line(text: str) -> None:
    """打印 RuntimeRunResult 最终结果日志。

    参数:
        text: 已格式化好的最终结果文本。

    返回:
        无返回值。
    """

    console.print(f"{_format_log_timestamp()} {text}", style=STYLE_RUN_RESULT, markup=False)


def _format_log_timestamp() -> str:
    """生成控制台观测日志使用的本地时间戳。

    返回:
        精确到毫秒的本地时间字符串。
    """

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _print_input_event(prefix: str, event: InputEvent) -> None:
    """打印 Runtime 接收到的输入事件。

    参数:
        prefix: 当前事件通用前缀。
        event: 当前输入事件。

    返回:
        无返回值。
    """

    _print_event_line(
        f"{prefix} input_id={event.input.input_id} "
        f"content={_escape_text(_format_input(event.input))}",
        event,
    )


def _print_message_event(prefix: str, event: MessageEvent) -> None:
    """打印 assistant message 事件中的流式片段或最终聚合结果。

    参数:
        prefix: 当前事件通用前缀。
        event: 当前 message 事件。

    返回:
        无返回值。
    """

    if event.completion is not None:
        _print_message_completion(prefix, event)
        return

    if event.chunk is None:
        _print_event_line(prefix, event)
        return

    for choice in event.chunk.choices:
        delta = choice.delta
        tool_deltas: list[str] = []
        for tool_call in delta.tool_calls or []:
            function = tool_call.function
            tool_deltas.append(
                f"index={tool_call.index} "
                f"id={tool_call.id} "
                f"name={function.name if function else None} "
                f"arguments={_escape_text(function.arguments) if function else None}"
            )
        _print_event_line(
            f"{prefix} "
            f"reasoning={_escape_text(delta.reasoning_content or '')} "
            f"content={_escape_text(delta.content or '')} "
            f"tool_delta={' | '.join(tool_deltas)} "
            f"finish_reason={choice.finish_reason or ''} "
            f"usage={_format_usage(event.chunk.usage) if event.chunk.usage else ''}",
            event,
        )


def _print_message_completion(prefix: str, event: MessageEvent) -> None:
    """打印 MessageEvent FINAL 阶段中的完整 completion。

    参数:
        prefix: 当前事件通用前缀。
        event: 当前已携带 completion 的 message 事件。

    返回:
        无返回值。
    """

    if event.completion is None:
        _print_event_line(prefix, event)
        return

    usage = _format_usage(event.completion.usage) if event.completion.usage else ""
    for choice in event.completion.choices:
        message = choice.message
        reasoning = message.reasoning_content or ""
        content = message.content or ""
        finish_reason = choice.finish_reason or ""
        _print_event_line(
            f"{prefix} final_reasoning={_escape_text(reasoning)} "
            f"final_content={_escape_text(content)} "
            f"finish_reason={finish_reason} "
            f"final_usage={usage}",
            event,
        )


def _print_tool_call_event(prefix: str, event: ToolCallEvent) -> None:
    """打印稳定工具调用事件。

    参数:
        prefix: 当前事件通用前缀。
        event: 当前工具调用事件。

    返回:
        无返回值。
    """

    function = event.tool_call.function
    _print_event_line(
        f"{prefix} tool_call_id={event.tool_call.id} "
        f"name={function.name if function else None} "
        f"arguments={_escape_text(function.arguments) if function else None}",
        event,
    )


def _print_tool_result_event(prefix: str, event: ToolResultEvent) -> None:
    """打印工具结果事件。

    参数:
        prefix: 当前事件通用前缀。
        event: 当前工具结果事件。

    返回:
        无返回值。
    """

    if event.result is None:
        _print_event_line(f"{prefix} tool_call_id={event.tool_call_id}", event)
        return
    _print_event_line(
        f"{prefix} tool_call_id={event.tool_call_id} "
        f"status={event.result.status.value} "
        f"result={_escape_text(event.result.__str__())}",
        event,
    )


def _print_context_compression_event(
    prefix: str,
    event: ContextCompressionEvent,
) -> None:
    """打印上下文压缩事件。

    参数:
        prefix: 当前事件通用前缀。
        event: 当前上下文压缩事件。

    返回:
        无返回值。
    """

    if event.compression_result is None:
        _print_event_line(f"{prefix} mode={event.compression_mode.value}", event)
        return

    result = event.compression_result
    _print_event_line(
        f"{prefix} mode={event.compression_mode.value} "
        f"merged={result.merged} "
        f"compressed_history_count={result.compressed_history_count} "
        f"origin_history={len(result.origin_history)} "
        f"compressed_history={len(result.compressed_history)} "
        f"origin_memory_chars={len(result.origin_mid_term_memory)} "
        f"compressed_memory_chars={len(result.compressed_mid_term_memory)}",
        event,
    )


def _print_interrupted_event(prefix: str, event: InterruptedEvent) -> None:
    """打印任务暂停并等待外部响应的中断事件。

    参数:
        prefix: 当前事件通用前缀。
        event: 当前已携带中断请求的暂停事件。

    返回:
        无返回值。
    """

    request = event.payload
    _print_event_line(
        f"{prefix} request_id={request.request_id} "
        f"request_type={event.interruption_type.value} "
        f"task_id={request.task_id} "
        f"reason={_escape_text(request.reason)} "
        f"params={_escape_text(str(request.params))}",
        event,
    )


def _print_run_result(result: RuntimeRunResult) -> None:
    """打印一次 Runtime run 的稳定输出摘要。

    参数:
        result: 当前 run 返回的稳定输出。

    返回:
        无返回值。
    """

    _print_result_line(
        f"[run_result] session_id={result.session_id} "
        f"outputs={len(result.outputs)}"
    )
    for task_output in result.outputs:
        _print_result_line(f"[input] {_escape_text(_format_input(task_output.input))}")
        for output in task_output.outputs:
            if isinstance(output, MessageEvent):
                if output.completion is None:
                    continue
                for choice in output.completion.choices:
                    content = choice.message.content or ""
                    reasoning = choice.message.reasoning_content or ""
                    if reasoning:
                        _print_result_line(f"[thinking...] {_escape_text(reasoning)}")
                    if content:
                        _print_result_line(f"[assistant] {_escape_text(content)}")
                continue
            if isinstance(output, ToolResultEvent) and output.result is not None:
                _print_result_line(f"[tool_result] {_escape_text(output.result.content)}")


def _format_input(input_data: Input) -> str:
    """将 Runtime 输入包整理成适合控制台展示的文本。

    参数:
        input_data: 当前任务开始时进入 Runtime 的输入包。

    返回:
        按事件类型整理后的输入包摘要。
    """

    contents: list[str] = []
    for event in input_data.events:
        match event.event_type:
            case EventType.USER_INPUT:
                payload = ensure_instance(
                    event.payload,
                    UserInputPayload,
                    "用户输入事件载荷",
                )
                contents.append(f"user_input content={payload.content}")
            case EventType.INTERRUPTION_RESPONSE:
                payload = ensure_instance(
                    event.payload,
                    InterruptionResponsePayload,
                    "中断响应事件载荷",
                )
                contents.append(
                    "interruption_response "
                    f"request_id={payload.request_id} "
                    f"request_type={payload.request_type} "
                    f"response={payload.response.model_dump_json()}"
                )
            case _:
                payload = event.payload
                payload_text = (
                    payload.model_dump_json()
                    if hasattr(payload, "model_dump_json")
                    else str(payload)
                )
                contents.append(
                    f"event_type={event.event_type.value} payload={payload_text}"
                )
    return "\n".join(contents)


def _escape_text(text: str | None) -> str:
    """把文本转换成不触发控制台换行且保留中文的展示形式。

    参数:
        text: 待打印文本。

    返回:
        已转义换行、制表等控制字符的字符串。
    """

    if text is None:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _format_usage(usage: LLMCompletionUsage) -> str:
    """格式化 LLM token 用量。

    参数:
        usage: 当前模型调用返回的 token 用量。

    返回:
        单行 token 用量摘要。
    """

    return (
        f"prompt_tokens={usage.prompt_tokens}, "
        f"completion_tokens={usage.completion_tokens}, "
        f"total_tokens={usage.total_tokens}"
    )


if __name__ == "__main__":
    asyncio.run(main())
