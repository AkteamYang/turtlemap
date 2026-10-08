#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/30 15:35
# @Author  : YaHaoo
# @File    : context.py

"""turtlemap os 层 Agent 运行期拦截上下文定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from turtlemap.os.agent import Agent
from turtlemap.os.models import ProcessingTask, SessionState


K_HAS_CONTINUE_TAG = "has_continue_tag"


@dataclass(frozen=True, slots=True)
class AgentInterceptorContext:
    """承载一次 Agent 运行期拦截所需的动态上下文。

    说明:
        上下文仅引用本次 Runtime 运行中的 Agent、会话状态和任务现场，不由
        `AgentInterceptor` 长期持有，避免同一拦截器在不同会话或并发运行间串用状态。
        输入处理等任务尚未创建的阶段，`task` 允许为空。
    """

    # 当前正在执行或消费事件的 Agent。
    agent: Agent

    # 当前 Runtime 持有的会话级状态。
    session_state: SessionState

    # 当前关联的任务现场；输入进入 Runtime 但尚未建任务时为空。
    task: ProcessingTask | None = None


@dataclass(frozen=True, slots=True)
class MessageContent:
    """表示 LLM 消息中可展示的推理与正文内容。

    说明:
        该模型只承载流式消息的文本快照，不包含 role、tool call 或 LLM 厂商协议字段。
        截流、重发与展示层替换都以完整快照为单位处理，避免将增量片段误作稳定内容。
    """

    # 当前消息累计的推理内容；尚未收到该通道时为空。
    reasoning_content: str | None = None

    # 当前消息累计的正文内容；尚未收到该通道时为空。
    content: str | None = None


@dataclass(frozen=True, slots=True)
class MessageModified:
    """表示 interceptor 修订后的消息内容及其 Runtime 控制参数。

    说明:
        `display_content` 面向用户展示，`context_content` 面向后续 LLM 上下文；应用必须
        显式返回两个通道，避免展示治理意外篡改模型历史。`runtime_params` 承载需要随
        choice 传递至 Runtime final 结果的结构化控制信号。相同参数名按后续 interceptor
        或后续流式事件的值覆盖。
    """

    # 修订后应发布给用户的消息文本
    # None 表示不修改
    display_content: Optional[MessageContent] = None

    # 修订后应沉淀到后续 LLM 上下文的消息文本
    # None 表示不修改
    context_content: Optional[MessageContent] = None

    # 当前消息修改携带的 Runtime 控制参数。
    runtime_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MessageInterceptionContext:
    """表示单个 LLM 消息流在截流过程中的文本状态。

    说明:
        每条通道各自维护已接收但尚未发布的截流内容，以及已经通过释放流程的累计内容。
        chunk 与 choice 元信息用于 Runtime 在 `END` 前构建同一消息流的补发事件。Runtime
        应按 `(event_id, choice_index)` 维护该对象，不由 interceptor 长期保存。
    """

    # 当前截流内容所属 LLM completion chunk 的稳定标识。
    chunk_id: str

    # 当前截流内容所属候选回复的序号。
    choice_index: int

    # 当前 completion 的创建时间，用于构建补发 chunk。
    chunk_created: int

    # 当前 completion 使用的模型名称，用于构建补发 chunk。
    chunk_model: str

    # 判断是否继续截流时统一读取 intercepted_display_content；此阶段它与 context 通道内容一致。

    # 已截流、尚未发布至展示层的内容。
    intercepted_display_content: MessageContent = field(default_factory=MessageContent)

    # 已截流、尚未沉淀到 LLM 上下文的内容。
    intercepted_context_content: MessageContent = field(default_factory=MessageContent)

    # 当前消息流已经发布至展示层的累计内容，不包含尚未释放的截流文本。
    received_display_content: MessageContent = field(default_factory=MessageContent)

    # 当前消息流已经沉淀到 LLM 上下文的累计内容，不包含尚未释放的截流文本。
    received_context_content: MessageContent = field(default_factory=MessageContent)
