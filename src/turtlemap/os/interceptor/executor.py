#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/30 15:56
# @Author  : YaHaoo
# @File    : executor.py

"""turtlemap os 层 Agent 运行期拦截器执行器。"""

from __future__ import annotations

import inspect
from dataclasses import replace

from typing_extensions import override

from turtlemap.kernel.models import ObservableEvent
from turtlemap.kernel.tool import ToolResult
from turtlemap.os.event_bus import RuntimeEvent
from turtlemap.os.exceptions import OSRuntimeError
from turtlemap.os.llm.model import LLMCompletionToolCall

from .context import (
    AgentInterceptorContext,
    MessageContent,
    MessageInterceptionContext,
    MessageModified,
)
from .interceptor import BaseAgentInterceptor


class InterceptorExecutor(BaseAgentInterceptor):
    """按注册顺序执行 Agent 运行期拦截器。

    说明:
        执行器只负责串行调度 hook 并传递处理结果，不持有或构建动态运行上下文。
        `AgentInterceptorContext` 必须由 Runtime 在每个生命周期节点根据当前 Agent、
        会话与任务现场实时创建后传入。
    """

    def __init__(self, interceptors: list[BaseAgentInterceptor]) -> None:
        """初始化拦截器执行器。

        参数:
            interceptors: Runtime 已保存的有序拦截器列表。

        说明:
            执行器保留该列表引用，使 Runtime 后续对已保存列表的显式调整能立即生效。
        """

        self._interceptors = interceptors
        self._validate_hook_overrides()

    @override
    async def input_before_process(
        self,
        context: AgentInterceptorContext,
        input_events: list[ObservableEvent],
    ) -> list[ObservableEvent]:
        """按注册顺序处理进入 Runtime 前的输入事件。

        参数:
            context: 当前输入处理对应的动态运行上下文。
            input_events: 当前调用方提交给 Runtime 的输入事件列表。

        返回:
            所有输入拦截器顺序处理后的输入事件列表。

        说明:
            后一个拦截器消费前一个拦截器的返回值，便于组合字符规范化、输入校验和
            业务协议修正等处理。
        """

        processed_input_events = input_events

        # 拦截器链按 Runtime 注册顺序串行执行，后一个消费前一个的处理结果。
        for interceptor in self._interceptors:
            processed_input_events = await interceptor.input_before_process(
                context=context,
                input_events=processed_input_events,
            )
        return processed_input_events

    @override
    async def message_should_continue_intercept(
        self,
        context: AgentInterceptorContext,
        interception_context: MessageInterceptionContext,
    ) -> bool:
        """按注册顺序判断当前消息流是否继续截流。

        参数:
            context: 当前消息流对应的动态运行上下文。
            interception_context: 当前消息流的截流与累计接收内容快照。

        返回:
            任一 interceptor 返回 `True` 时立即停止后续判断并继续截流；全部返回
            `False` 时释放当前内容。没有 interceptor 时默认返回 `False`。
        """

        if not self._interceptors:
            return False

        for interceptor in self._interceptors:
            should_continue = await interceptor.message_should_continue_intercept(
                context=context,
                interception_context=interception_context,
            )
            if should_continue:
                return True

        return False

    @override
    async def message_before_flush_intercepted(
        self,
        context: AgentInterceptorContext,
        interception_context: MessageInterceptionContext,
        is_message_end: bool,
    ) -> MessageModified:
        """按注册顺序修正即将发布的截流内容。

        参数:
            context: 当前消息流对应的动态运行上下文。
            interception_context: 当前待释放的截流上下文。
            is_message_end: 当前 SSE 消息流是否已结束。

        返回:
            所有 interceptor 顺序修正后待发布的内容及 Runtime 控制参数。
        """

        processed_interception_context = interception_context
        processed_modified = MessageModified(
            display_content=interception_context.intercepted_display_content,
            context_content=interception_context.intercepted_context_content,
        )
        for interceptor in self._interceptors:
            interceptor_modified = await interceptor.message_before_flush_intercepted(
                context=context,
                interception_context=processed_interception_context,
                is_message_end=is_message_end,
            )

            processed_modified = MessageModified(
                display_content=interceptor_modified.display_content or processed_modified.display_content,
                context_content=interceptor_modified.context_content or processed_modified.context_content,
                runtime_params={
                    **processed_modified.runtime_params,
                    **interceptor_modified.runtime_params,
                },
            )

            # 后续 interceptor 应基于前一个的内容修订继续处理，但不能改写发布器状态。
            processed_interception_context = replace(
                processed_interception_context,
                intercepted_display_content=processed_modified.display_content,
                intercepted_context_content=processed_modified.context_content,
            )
        return processed_modified

    @override
    async def message_build_replay_content(
        self,
        context: AgentInterceptorContext,
        received_display_content: MessageContent,
        received_context_content: MessageContent,
        is_message_end: bool,
    ) -> MessageModified | None:
        """按注册顺序查询是否需要重发当前消息内容快照。

        参数:
            context: 当前消息流对应的动态运行上下文。
            received_display_content: 当前消息流已实际发布给用户的累计文本快照。
            received_context_content: 当前消息流已沉淀到 LLM 上下文的累计文本快照。
            is_message_end: 当前 SSE 消息流是否已结束。

        返回:
            所有请求重发的 interceptor 顺序修订后的内容快照；全部无需重发时返回 `None`。

        说明:
            每个 interceptor 都有机会参与重放修订。后一个 interceptor 消费前一个的
            修订结果；只有所有 interceptor 都返回 `None` 时才不发送重放事件。
        """

        processed_modified: MessageModified | None = None
        processed_display_content = received_display_content
        processed_context_content = received_context_content
        for interceptor in self._interceptors:
            interceptor_modified = await interceptor.message_build_replay_content(
                context=context,
                received_display_content=processed_display_content,
                received_context_content=processed_context_content,
                is_message_end=is_message_end,
            )
            if interceptor_modified is None:
                continue

            processed_display_content = interceptor_modified.display_content or processed_display_content
            processed_context_content = interceptor_modified.context_content or processed_context_content
            processed_modified = MessageModified(
                display_content=processed_display_content,
                context_content=processed_context_content,
                runtime_params={
                    **(processed_modified.runtime_params if processed_modified else {}),
                    **interceptor_modified.runtime_params,
                },
            )
        return processed_modified

    @override
    async def event_before_publish(
        self,
        context: AgentInterceptorContext,
        event: RuntimeEvent,
    ) -> RuntimeEvent:
        """按注册顺序处理待发布的 Runtime 事件。

        参数:
            context: 当前事件对应的动态运行上下文。
            event: 当前待发布的 Runtime 事件。

        返回:
            所有事件拦截器顺序处理后的 Runtime 事件。
        """

        processed_event = event
        for interceptor in self._interceptors:
            processed_event = await interceptor.event_before_publish(
                context=context,
                event=processed_event,
            )
        return processed_event

    @override
    async def tool_call_before_execute(
        self,
        context: AgentInterceptorContext,
        tool_call: LLMCompletionToolCall,
    ) -> LLMCompletionToolCall:
        """按注册顺序处理待执行的工具调用。

        参数:
            context: 当前工具调用对应的动态运行上下文。
            tool_call: 当前待执行的完整工具调用。

        返回:
            所有工具调用拦截器顺序处理后的工具调用。
        """

        processed_tool_call = tool_call
        for interceptor in self._interceptors:
            processed_tool_call = await interceptor.tool_call_before_execute(
                context=context,
                tool_call=processed_tool_call,
            )
        return processed_tool_call

    @override
    async def tool_result_after_execute(
        self,
        context: AgentInterceptorContext,
        tool_result: ToolResult,
    ) -> ToolResult:
        """按注册顺序处理已执行工具的结果。

        参数:
            context: 当前工具结果对应的动态运行上下文。
            tool_result: 当前工具调用形成的标准结果。

        返回:
            所有工具结果拦截器顺序处理后的标准工具结果。
        """

        processed_tool_result = tool_result
        for interceptor in self._interceptors:
            processed_tool_result = await interceptor.tool_result_after_execute(
                context=context,
                tool_result=processed_tool_result,
            )
        return processed_tool_result

    @staticmethod
    def _validate_hook_overrides() -> None:
        """校验执行器显式重写了基础拦截器定义的全部公开 hook。

        返回:
            无返回值；缺少 override 时抛出异常。

        异常:
            OSRuntimeError: 基础拦截器新增 hook、执行器未重写或同步异步类型不匹配时抛出。

        说明:
            基础类为所有 hook 提供默认原样返回实现。执行器若意外继承新 hook 而未
            重写，会静默跳过已注册拦截器；此处通过反射在 Runtime 初始化阶段提前失败。
        """

        base_hook_name2method = {
            method_name: method
            for method_name, method in inspect.getmembers(
                BaseAgentInterceptor,
                predicate=inspect.isfunction,
            )
            if not method_name.startswith("_")
        }
        missing_hook_names = [
            method_name
            for method_name, base_method in base_hook_name2method.items()
            if not InterceptorExecutor._is_same_hook_type(
                base_method=base_method,
                executor_method=vars(InterceptorExecutor).get(method_name),
            )
        ]
        if missing_hook_names:
            raise OSRuntimeError(
                "InterceptorExecutor 未重写 BaseAgentInterceptor 的 hook："
                f"missing_hook_names={missing_hook_names}"
            )

    @staticmethod
    def _is_same_hook_type(
        base_method: object,
        executor_method: object,
    ) -> bool:
        """判断基础 hook 与执行器 override 的函数类型是否一致。

        参数:
            base_method: 从基础拦截器读取的 hook 定义。
            executor_method: 从执行器自身类定义读取的同名条目。

        返回:
            两者均为函数，且同步或异步类型一致时返回 `True`；否则返回 `False`。

        说明:
            普通函数与协程函数都属于可调度 hook，但同步 hook 不可由异步方法替代，
            异步 hook 也不可由普通函数替代。
        """

        if not inspect.isfunction(base_method) or not inspect.isfunction(
            executor_method
        ):
            return False
        return inspect.iscoroutinefunction(base_method) == inspect.iscoroutinefunction(
            executor_method
        )
