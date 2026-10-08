#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/30 15:42
# @Author  : YaHaoo
# @File    : agent_interceptor.py

"""turtlemap os 层默认 Agent 运行期拦截器。"""

from __future__ import annotations
from dataclasses import replace

from turtlemap.os.context.prompt import K_CONTINUE_MARKER

from .context import (
    AgentInterceptorContext,
    K_HAS_CONTINUE_TAG,
    MessageContent,
    MessageInterceptionContext,
    MessageModified,
)
from .interceptor import BaseAgentInterceptor


class AgentInterceptor(BaseAgentInterceptor):
    """提供 os 层默认的 Agent 运行期拦截器实现。

    说明:
        消息相关 hook 在此显式定义，作为 os 层统一的流式内容治理入口。当前默认实现
        负责隐藏 subagent 的 `<#CONTINUE#>` 控制标记；后续可在此集中实现内容规范化等
        通用策略。
    """

    async def message_should_continue_intercept(
        self,
        context: AgentInterceptorContext,
        interception_context: MessageInterceptionContext,
    ) -> bool:
        """判断 os 层是否继续截流当前消息增量。

        参数:
            context: 当前消息流对应的动态 Agent、会话与任务上下文。
            interception_context: 当前 choice 的截流与已发布内容快照。

        返回:
            `True` 表示继续截流，`False` 表示释放内容。
        """

        if not self._is_subagent(context):
            return await super().message_should_continue_intercept(
                context=context,
                interception_context=interception_context
            )

        # 只要最后一个 `<` 后的尾段仍可能组成控制标记，就先不将其展示给用户。
        content = interception_context.intercepted_display_content.content or ""
        marker_start = content.rfind("<")
        return marker_start >= 0 and content[marker_start:] in K_CONTINUE_MARKER

    async def message_before_flush_intercepted(
        self,
        context: AgentInterceptorContext,
        interception_context: MessageInterceptionContext,
        is_message_end: bool,
    ) -> MessageModified:
        """在释放截流内容前执行 os 层统一文本处理。

        参数:
            context: 当前消息流对应的动态 Agent、会话与任务上下文。
            interception_context: 当前 choice 的待释放截流内容快照。
            is_message_end: 当前 SSE 消息流是否已结束。

        返回:
            应发布的内容及 Runtime 控制参数；subagent 中出现的继续标记会被移除。
        """

        intercepted_display_content = interception_context.intercepted_display_content
        has_continue_tag = (
            K_CONTINUE_MARKER in (intercepted_display_content.reasoning_content or "")
            or K_CONTINUE_MARKER in (intercepted_display_content.content or "")
        )
        if not self._is_subagent(context) or not has_continue_tag:
            return await super().message_before_flush_intercepted(
                context=context,
                interception_context=interception_context,
                is_message_end=is_message_end,
            )

        return MessageModified(
            display_content=MessageContent(
                reasoning_content=(
                    (intercepted_display_content.reasoning_content or "").replace(
                        K_CONTINUE_MARKER,
                        "",
                    )
                ),
                content=(intercepted_display_content.content or "").replace(
                    K_CONTINUE_MARKER,
                    "",
                ),
            ),
            runtime_params={K_HAS_CONTINUE_TAG: True},
        )

    async def message_build_replay_content(
        self,
        context: AgentInterceptorContext,
        received_display_content: MessageContent,
        received_context_content: MessageContent,
        is_message_end: bool,
    ) -> MessageModified | None:
        """根据已发布消息构造 os 层内容覆盖快照。

        参数:
            context: 当前消息流对应的动态 Agent、会话与任务上下文。
            received_display_content: 当前 choice 已实际发布给用户的完整内容。
            received_context_content: 当前 choice 已沉淀到 LLM 上下文的完整内容。
            is_message_end: 当前 SSE 消息流是否已结束。

        返回:
            需要覆盖展示时返回完整文本快照；默认不重发。
        """
        if "您" in (received_display_content.content or ""):
            return MessageModified(
                display_content=replace(
                    received_display_content,
                    content=(received_display_content.content or "").replace("您", "Ning"),
                ),
                runtime_params={"您": 1},
            )
        
        return await super().message_build_replay_content(
            context,
            received_display_content,
            received_context_content,
            is_message_end,
        )

    @staticmethod
    def _is_subagent(context: AgentInterceptorContext) -> bool:
        """判断当前消息是否由嵌套 Agent frame 中的 subagent 产生。

        参数:
            context: 当前消息流对应的动态 Agent、会话与任务上下文。

        返回:
            Agent frame 深度大于一时返回 `True`。
        """

        return len(context.session_state.agent_frames) > 1
