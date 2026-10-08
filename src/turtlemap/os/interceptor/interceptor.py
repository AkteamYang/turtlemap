#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/30 15:27
# @Author  : YaHaoo
# @File    : interceptor.py

"""turtlemap os 层 Agent 运行期拦截器定义。"""

from __future__ import annotations

from turtlemap.kernel.models import ObservableEvent
from turtlemap.kernel.tool import ToolResult
from turtlemap.os.event_bus import RuntimeEvent
from turtlemap.os.llm.model import LLMCompletionToolCall

from .context import (
    AgentInterceptorContext,
    MessageContent,
    MessageInterceptionContext,
    MessageModified,
)


class BaseAgentInterceptor:
    """定义 Agent 运行期输入、输出、事件与工具执行的基础 AOP 扩展点。

    说明:
        每个 Agent 可选使用一个拦截器，用于在不侵入 Runtime 主流程的前提下处理
        输入规范化、流式消息缓冲、事件公共字段、工具调用参数和工具结果。所有方法
        默认原样返回输入对象，运行链路可无条件调用；子类仅覆写需要介入的职能方法。
    """

    async def input_before_process(
        self,
        context: AgentInterceptorContext,
        input_events: list[ObservableEvent],
    ) -> list[ObservableEvent]:
        """在 Runtime 消费外部输入前规范化或校验输入事件。

        参数:
            context: 当前输入处理对应的 Agent 与会话上下文；此阶段任务可能为空。
            input_events: 本次进入 Runtime 的外部事件列表。

        返回:
            经处理后的输入事件列表；默认原样返回。

        说明:
            适合处理特殊字符、协议字段修正或业务输入校验。不应在此方法中创建任务、
            发布事件或改变 Runtime 调度状态。
        """

        return input_events

    async def message_should_continue_intercept(
        self,
        context: AgentInterceptorContext,
        interception_context: MessageInterceptionContext,
    ) -> bool:
        """判断当前 LLM 消息流是否继续截流而不立即发布。

        参数:
            context: 当前消息流对应的 Agent、会话与任务上下文。
            interception_context: 当前已截流内容与累计已接收内容的快照。

        返回:
            返回 `True` 表示继续截流；返回 `False` 表示释放当前截流内容。默认不截流。

        说明:
            业务可基于累计已截流内容执行内容校验。该方法只决定是否继续截流，不修改
            文本；释放前的内容修正应通过 `message_before_flush_intercepted` 完成。
        """

        return False

    async def message_before_flush_intercepted(
        self,
        context: AgentInterceptorContext,
        interception_context: MessageInterceptionContext,
        is_message_end: bool,
    ) -> MessageModified:
        """在 Runtime 发布已截流内容前修正其文本快照。

        参数:
            context: 当前消息流对应的 Agent、会话与任务上下文。
            interception_context: 当前待释放的截流上下文。
            is_message_end: 当前 SSE 消息流是否已结束。

        返回:
            修正后待发布的展示与上下文内容及 Runtime 控制参数；默认两个通道都返回
            原始截流内容。

        说明:
            业务可在此进行脱敏、格式修复或替换，并明确决定修改是否进入后续 LLM
            上下文。已发布快照与 chunk 元信息由 MessagePublisher 维护，不允许通过
            该 hook 改写。
        """
        return MessageModified()

    async def message_build_replay_content(
        self,
        context: AgentInterceptorContext,
        received_display_content: MessageContent,
        received_context_content: MessageContent,
        is_message_end: bool,
    ) -> MessageModified | None:
        """判断是否覆盖已展示内容，并构造用于重发的完整快照。

        参数:
            context: 当前消息流对应的 Agent、会话与任务上下文。
            received_display_content: 当前消息流已实际发布给用户的累计文本快照。
            received_context_content: 当前消息流已沉淀到后续 LLM 上下文的累计文本快照。
            is_message_end: 当前 SSE 消息流是否已结束。

        返回:
            需要重发时返回新的展示与上下文完整内容及 Runtime 控制参数；无需重发时
            返回 `None`。

        说明:
            Runtime 后续会将非空结果以同一消息事件流的 `IN_PROGRESS_PARTIAL` 事件
            重发，供展示层覆盖当前已展示内容。
        """
        return None

    async def event_before_publish(
        self,
        context: AgentInterceptorContext,
        event: RuntimeEvent,
    ) -> RuntimeEvent:
        """在 Runtime 事件发布前补全、校验或规范化公共事件字段。

        参数:
            context: 当前事件对应的 Agent、会话与任务上下文。
            event: 当前待发布至 event bus 的运行期事件。

        返回:
            经处理后的运行期事件；默认原样返回。

        说明:
            适合统一处理公共元信息、展示字段和事件载荷校验；不得修改已经由
            event bus 分配的事件顺序信息。
        """
        return event

    async def tool_call_before_execute(
        self,
        context: AgentInterceptorContext,
        tool_call: LLMCompletionToolCall,
    ) -> LLMCompletionToolCall:
        """在工具调用执行前修复或校验模型生成的工具调用参数。

        参数:
            context: 当前工具调用对应的 Agent、会话与任务上下文。
            tool_call: 当前待执行的完整工具调用。

        返回:
            经处理后的工具调用；默认原样返回。

        说明:
            适合修复模型生成的参数格式、补充可确定的默认值或执行业务校验；不得
            将工具调用改写为另一种工具语义。
        """

        return tool_call

    async def tool_result_after_execute(
        self,
        context: AgentInterceptorContext,
        tool_result: ToolResult,
    ) -> ToolResult:
        """在工具执行结束后规范化工具结果。

        参数:
            context: 当前工具结果对应的 Agent、会话与任务上下文。
            tool_result: 当前工具调用形成的标准结果。

        返回:
            经处理后的工具结果；默认原样返回。

        说明:
            适合统一错误表达、脱敏结果内容或补充稳定展示字段；不得隐瞒工具的实际
            执行状态。
        """

        return tool_result
