#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/30 17:26
# @Author  : YaHaoo
# @File    : message_publisher.py

"""turtlemap os 层 LLM 消息事件发布与截流实现。"""

from __future__ import annotations

from dataclasses import replace

from turtlemap.os.interceptor.context import (
    AgentInterceptorContext,
    MessageContent,
    MessageInterceptionContext,
    MessageModified,
)
from turtlemap.os.interceptor.executor import InterceptorExecutor
from turtlemap.os.llm.model import (
    LLMCompletionChunk,
    LLMCompletionChunkChoice,
    LLMCompletionChunkChoiceDelta,
)
from turtlemap.os.models import ProcessingTask
from turtlemap.shared.typing import ensure_instance

from .models import MessageEvent, RuntimeEventPhase


class MessagePublisher:
    """发布 LLM 消息事件，并按候选回复维度维护截流状态。

    说明:
        publisher 对外统一接收 `MessageEvent`，内部在每个 `IN_PROGRESS` chunk 发布前
        调用 interceptor。截流状态以 `(event_id, choice_index)` 隔离，保证多 choice
        响应不会混入同一缓冲区。截流期间仍转发空文本事件，释放时将累计文本回填到
        当前事件；若直到 `END` 仍有待发布文本，则自动补发同一消息流的 `IN_PROGRESS`。
    """

    def __init__(
        self,
        event_bus_id: str,
        interceptor_executor: InterceptorExecutor,
    ) -> None:
        """初始化消息发布器。

        参数:
            event_bus_id: 当前 Runtime 绑定的事件总线通道标识。
            interceptor_executor: 当前 Runtime 使用的有序 interceptor 调度器。
        """

        # 当前发布器绑定的事件通道，所有转发事件均进入该通道。
        self._event_bus_id = event_bus_id

        # 负责按顺序调用消息截流相关 hook 的执行器。
        self._interceptor_executor = interceptor_executor

        # 同一消息事件下的多 choice 必须使用独立截流缓冲。
        self._event_id2choice_index2interception_context: dict[
            str,
            dict[int, MessageInterceptionContext],
        ] = {}

    async def publish(
        self,
        context: AgentInterceptorContext,
        event: MessageEvent,
    ) -> None:
        """处理截流后发布一条 LLM 消息事件。

        参数:
            context: 当前消息流对应的动态 Agent、会话与任务上下文。
            event: 当前待发布的消息事件。

        返回:
            无返回值。

        说明:
            `IN_PROGRESS` 与 `END` 均由统一处理链路完成截流、释放与内容覆盖；`END`
            前会先补发仍被截流的文本快照，再清理该消息事件的截流状态。调用方无需
            区分普通消息发布和截流消息发布。
        """

        events_before, events_after = await self._process_message_event(
            context=context,
            event=event,
        )

        for process_event in events_before:
            await ProcessingTask.publish(
                context.task,
                self._event_bus_id,
                process_event,
            )

        await ProcessingTask.publish(
            context.task,
            self._event_bus_id,
            event,
        )
        for process_event in events_after:
            await ProcessingTask.publish(
                context.task,
                self._event_bus_id,
                process_event,
            )

        if event.event_phase == RuntimeEventPhase.END:

            # `END` 已经完成所有 choice 的释放，后续不会再有同一消息流的增量。
            self._event_id2choice_index2interception_context.pop(event.event_id, None)

    async def _process_message_event(
        self,
        context: AgentInterceptorContext,
        event: MessageEvent,
    ) -> tuple[list[MessageEvent], list[MessageEvent]]:
        """统一处理消息过程事件中的截流、释放与内容覆盖。

        参数:
            context: 当前消息流对应的动态运行上下文。
            event: 当前待发布的消息事件。

        返回:
            当前事件前后需要额外发布的消息事件列表。`END` 前补发位于前置列表，
            普通过程事件完成后的内容覆盖位于后置列表。

        说明:
            `IN_PROGRESS` 从当前 chunk 读取新文本，`END` 从既有截流缓冲读取待释放文本。
            两类事件共用释放、快照更新与 replay 链路，避免维护两套相似状态机。
        """

        is_in_progress = event.event_phase == RuntimeEventPhase.IN_PROGRESS
        is_end = event.event_phase == RuntimeEventPhase.END
        if not is_in_progress and not is_end:
            return [], []

        chunk: LLMCompletionChunk | None = ensure_instance(
                event.chunk,
                LLMCompletionChunk,
                "截流消息事件的 chunk",
            ) if event.chunk else None
        choice_index2choice: dict[int, LLMCompletionChunkChoice] = {}
        interception_contexts: list[MessageInterceptionContext] = []
        has_usage = True if chunk and chunk.usage else False
        is_message_end = is_end or has_usage

        # 有 choices 时处理当前 choices 对应的截流数据
        # 没有时，不做任何处理，除非当前流已结束
        if chunk and chunk.choices:
            for choice in chunk.choices:
                choice_index2choice[choice.index] = choice
                interception_contexts.append(
                    self._build_interception_context(
                        event_id=event.event_id,
                        chunk=chunk,
                        choice=choice,
                    )
                )
        elif is_end:
            interception_contexts = list(
                self._event_id2choice_index2interception_context.get(
                    event.event_id,
                    {},
                ).values()
            )

        # 没有待释放文本的上下文无需触发 flush、快照合并或内容覆盖。
        interception_contexts = [
            interception_context
            for interception_context in interception_contexts
            if self._has_message_content(
                interception_context.intercepted_display_content
            )
            or self._has_message_content(
                interception_context.intercepted_context_content
            )
        ]
        if not interception_contexts:
            return [], []

        # 已有内容请求覆盖展示
        replay_choice_contents: list[tuple[MessageInterceptionContext, MessageModified]] = []

        # 待发送的截流内容
        flush_interception_contents: list[tuple[MessageInterceptionContext, MessageModified]] = []
        for interception_context in interception_contexts:

            # 已结束时，不截流缓冲。
            if is_message_end:
                should_continue_intercept = False
            else:
                should_continue_intercept = (
                    await self._interceptor_executor.message_should_continue_intercept(
                        context=context,
                        interception_context=interception_context,
                    )
                )

            # 需要截流时，保留原始 provider delta，仅清空两个对外发布通道。
            if should_continue_intercept:
                choice = choice_index2choice.get(interception_context.choice_index)
                if choice is not None:
                    self._clear_choice_channel_content(choice)
                self._save_interception_context(
                    event_id=event.event_id,
                    interception_context=interception_context,
                )
                continue

            # 无需截流时，修订两条待释放通道并更新各自已发布快照。
            flushed_message_modified = (
                await self._interceptor_executor.message_before_flush_intercepted(
                    context=context,
                    interception_context=interception_context,
                    is_message_end=is_message_end,
                )
            )
            display_content = (
                flushed_message_modified.display_content
                if flushed_message_modified.display_content is not None
                else interception_context.intercepted_display_content
            )
            context_content = (
                flushed_message_modified.context_content
                if flushed_message_modified.context_content is not None
                else interception_context.intercepted_context_content
            )

            # 仅把 interceptor 显式改写且与原始 delta 不同的内容写入派生通道。
            # choice 读取规则是 content/reasoning_content 会作为 display/context content 的兜底
            # 因此当内容相同时避免给 display/context content 赋值以减少冗余数据存储
            choice = choice_index2choice.get(interception_context.choice_index)
            if choice is not None:
                if choice.delta.reasoning_content != display_content.reasoning_content:
                    choice.delta.display_reasoning_content = (
                        display_content.reasoning_content
                    )
                if choice.delta.content != display_content.content:
                    choice.delta.display_content = display_content.content
                if choice.delta.reasoning_content != context_content.reasoning_content:
                    choice.delta.context_reasoning_content = (
                        context_content.reasoning_content
                    )
                if choice.delta.content != context_content.content:
                    choice.delta.context_content = context_content.content

                # 控制参数随 delta 一并聚合到最终 LLMMessage，不停留在 choice 壳层。
                choice.delta.runtime_params.update(flushed_message_modified.runtime_params)

            # 截流内容只有通过 hook 释放后，才会进入对应通道的已发布快照。
            interception_context = replace(
                interception_context,
                intercepted_display_content=MessageContent(),
                intercepted_context_content=MessageContent(),
                received_display_content=self._merge_message_content(
                    interception_context.received_display_content,
                    display_content,
                ),
                received_context_content=self._merge_message_content(
                    interception_context.received_context_content,
                    context_content,
                ),
            )

            # 如果是 end 事件需要补发截流内容。
            if is_end:
                flushed_message_modified = MessageModified(
                    display_content=display_content,
                    context_content=context_content,
                    runtime_params=flushed_message_modified.runtime_params,
                )
                flush_interception_contents.append(
                    (interception_context, flushed_message_modified)
                )

            # interceptor 基于最新接收内容请求覆盖重发。
            replay_message_modified = (
                await self._interceptor_executor.message_build_replay_content(
                    context=context,
                    received_display_content=interception_context.received_display_content,
                    received_context_content=interception_context.received_context_content,
                    is_message_end=is_message_end,
                )
            )
            if replay_message_modified is not None:
                if replay_message_modified.display_content is not None:
                    interception_context = replace(
                        interception_context,
                        received_display_content=replay_message_modified.display_content,
                    )
                if replay_message_modified.context_content is not None:
                    interception_context = replace(
                        interception_context,
                        received_context_content=replay_message_modified.context_content,
                    )
                replay_choice_contents.append(
                    (
                        interception_context,
                        replay_message_modified,
                    )
                )

            self._save_interception_context(
                event_id=event.event_id,
                interception_context=interception_context,
            )

        events_before: list[MessageEvent] = []
        events_after: list[MessageEvent] = []
        if flush_interception_contents:
            flush_event = self._build_content_event(
                source_event=event,
                event_phase=RuntimeEventPhase.IN_PROGRESS,
                choice_contents=flush_interception_contents,
            )
            events_before.append(flush_event)
        if replay_choice_contents:
            replay_event = self._build_content_event(
                source_event=event,
                event_phase=RuntimeEventPhase.IN_PROGRESS_PARTIAL,
                choice_contents=replay_choice_contents,
            )
            if is_end:
                events_before.append(replay_event)
            else:
                events_after.append(replay_event)
        return events_before, events_after

    def _build_interception_context(
        self,
        event_id: str,
        chunk: LLMCompletionChunk,
        choice: LLMCompletionChunkChoice,
    ) -> MessageInterceptionContext:
        """基于当前 choice 与既有缓冲构建最新截流上下文。

        参数:
            event_id: 当前消息事件流标识。
            chunk: 当前 LLM 流式响应 chunk。
            choice: 当前待处理的候选回复。

        返回:
            当前增量仅合并到截流内容中的上下文；已发布内容保持不变，直到截流内容
            经过释放 hook 修订并真正发布。
        """

        previous_context = self._event_id2choice_index2interception_context.get(
            event_id,
            {},
        ).get(choice.index)
        current_delta_content = self._message_content_from_delta(choice.delta)
        if previous_context is None:

            # 首个 delta 固化补发所需的 completion 元信息，后续仅追加文本快照。
            previous_context = MessageInterceptionContext(
                chunk_id=chunk.id,
                choice_index=choice.index,
                chunk_created=chunk.created,
                chunk_model=chunk.model,
            )
        return MessageInterceptionContext(
            chunk_id=chunk.id,
            choice_index=choice.index,
            chunk_created=chunk.created,
            chunk_model=chunk.model,
            intercepted_display_content=self._merge_message_content(
                previous_context.intercepted_display_content,
                current_delta_content,
            ),
            intercepted_context_content=self._merge_message_content(
                previous_context.intercepted_context_content,
                current_delta_content,
            ),
            received_display_content=previous_context.received_display_content,
            received_context_content=previous_context.received_context_content,
        )

    def _save_interception_context(
        self,
        event_id: str,
        interception_context: MessageInterceptionContext,
    ) -> None:
        """保存指定消息事件与 choice 的最新截流上下文。

        参数:
            event_id: 当前消息事件流标识。
            interception_context: 当前 choice 对应的最新截流上下文。

        返回:
            无返回值。
        """

        choice_index2interception_context = (
            self._event_id2choice_index2interception_context.setdefault(event_id, {})
        )
        choice_index2interception_context[interception_context.choice_index] = (
            interception_context
        )

    @staticmethod
    def _has_message_content(message_content: MessageContent) -> bool:
        """判断消息内容是否包含已接收的任一文本通道。

        参数:
            message_content: 当前待判断的消息内容快照。

        返回:
            推理文本或正文已被设置时返回 `True`；空字符串也是有效内容。
        """

        return (
            message_content.reasoning_content is not None
            or message_content.content is not None
        )

    @staticmethod
    def _message_content_from_delta(
        delta: LLMCompletionChunkChoiceDelta,
    ) -> MessageContent:
        """将单个 LLM 增量中的展示文本转换为内容快照。

        参数:
            delta: 当前 choice 携带的 LLM 增量。

        返回:
            不包含 tool call 等协议字段的推理与正文内容快照。
        """

        return MessageContent(
            reasoning_content=delta.reasoning_content,
            content=delta.content,
        )

    @staticmethod
    def _merge_message_content(
        previous_content: MessageContent,
        current_content: MessageContent,
    ) -> MessageContent:
        """将当前增量内容追加到已有内容快照。

        参数:
            previous_content: 已累计的内容快照。
            current_content: 当前新接收的内容快照。

        返回:
            按推理与正文维度分别拼接后的完整内容快照。
        """

        return MessageContent(
            reasoning_content=MessagePublisher._append_message_text(
                previous_text=previous_content.reasoning_content,
                current_text=current_content.reasoning_content,
            ),
            content=MessagePublisher._append_message_text(
                previous_text=previous_content.content,
                current_text=current_content.content,
            ),
        )

    @staticmethod
    def _append_message_text(
        previous_text: str | None,
        current_text: str | None,
    ) -> str | None:
        """将可选文本增量合并到已有快照。

        参数:
            previous_text: 当前已累计的文本；未收到过该通道时为 `None`。
            current_text: 当前新接收的文本增量；本 chunk 不含该通道时为 `None`。

        返回:
            保留 `None` 表示尚未收到该通道；其他情况按增量顺序拼接文本。
        """

        if current_text is None:
            return previous_text
        if previous_text is None:
            return current_text
        return previous_text + current_text

    @staticmethod
    def _clear_choice_channel_content(choice: LLMCompletionChunkChoice) -> None:
        """清空 choice 中待发布的两个通道文本，保留原始 provider delta。

        参数:
            choice: 当前需要转发为空通道事件的流式候选回复。

        返回:
            无返回值。
        """
        if choice.delta.reasoning_content is not None:
            choice.delta.display_reasoning_content = ""
            choice.delta.context_reasoning_content = ""
        if choice.delta.content is not None:
            choice.delta.display_content = ""
            choice.delta.context_content = ""

    @staticmethod
    def _build_content_event(
        source_event: MessageEvent,
        event_phase: RuntimeEventPhase,
        choice_contents: list[tuple[MessageInterceptionContext, MessageModified]],
    ) -> MessageEvent:
        """构建同一消息流中的截流补发或内容覆盖事件。

        参数:
            source_event: 当前消息流中的原始消息事件。
            event_phase: 新事件的过程阶段，区分补发增量和完整内容覆盖。
            choice_contents: 各 choice 的截流上下文与经 interceptor 修订后的发布内容。

        返回:
            与原消息流使用相同 event_id 的补发或覆盖消息事件。
        """

        first_interception_context = choice_contents[0][0]
        content_event = source_event.model_copy(deep=True)
        content_event.event_phase = event_phase

        choice_contents_to_publish: list[LLMCompletionChunkChoice] = []
        for interception_context, message_modified in choice_contents:
            display_content = message_modified.display_content
            context_content = message_modified.context_content
            choice_contents_to_publish.append(
                LLMCompletionChunkChoice(
                    index=interception_context.choice_index,
                    delta=LLMCompletionChunkChoiceDelta(
                        display_reasoning_content=(
                            display_content.reasoning_content
                            if display_content is not None
                            else None
                        ),
                        display_content=(
                            display_content.content
                            if display_content is not None
                            else None
                        ),
                        context_reasoning_content=(
                            context_content.reasoning_content
                            if context_content is not None
                            else None
                        ),
                        context_content=(
                            context_content.content
                            if context_content is not None
                            else None
                        ),
                        runtime_params=message_modified.runtime_params,
                    ),
                )
            )

        # 新事件沿用原 event_id，并将多个 choice 聚合到同一个 completion chunk。
        content_event.chunk = LLMCompletionChunk(
            id=first_interception_context.chunk_id,
            created=first_interception_context.chunk_created,
            model=first_interception_context.chunk_model,
            choices=choice_contents_to_publish,
        )
        return content_event
