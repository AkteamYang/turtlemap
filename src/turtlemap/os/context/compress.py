#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 22:25
# @Author  : YaHaoo
# @File    : compress.py

"""turtlemap os 层上下文压缩治理实现。"""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy

from turtlemap.kernel.models import (
    BaseAgentState,
    MessageRole,
    RuntimeArtifact,
    RuntimeArtifactType,
)
from turtlemap.kernel.models.models import BaseProcessingTask
from turtlemap.os.llm.model import LLMMessage
from turtlemap.os.context.models import (
    ContextBuildInput,
    ContextBuildResult,
    ContextCompressionLevel,
    ContextCompressionResult,
    ContextCompressionTaskRecord,
)
from turtlemap.os.exceptions import (
    NoCompressibleHistoryError,
)
from turtlemap.os.event_bus.event_bus import EventBus
from turtlemap.os.event_bus.models import (
    ContextCompressionEvent,
    ContextCompressionMode,
    RuntimeEventPhase,
)
from turtlemap.os.models import ProcessingTask
from turtlemap.os.protocols import (
    CompressionResultCallback,
    ContextCompressionProtocol,
    ModelClientProtocol,
    StateStoreProtocol,
)
from turtlemap.shared import exponential_backoff_retry
from turtlemap.shared.ids import PREFIX_EVENT_ID, generate_prefixed_id
from turtlemap.shared.logger import Logger

from .provider import ContextBuildProvider
from .prompt import (
    K_SUMMARY_HISTORY,
    build_memory_section,
    build_mid_term_memory_summary_prompt,
)
from .tokenizer import Tokenizer

MAX_COMPLETED_SUMMARY_ITEMS = 20


class ContextCompressionProvider(ContextCompressionProtocol):
    """表示 os 层上下文压缩提供器。

    说明:
        当前实现先落一版最小可跑的同步压缩能力：
        当上下文超过硬限制时，优先把较早的 `history` 与已有
        `memory.mid_term_memory` 一起重写成新的中期工作记忆，
        同时只保留最近若干条原始历史消息，供外层重新构建上下文。
    """

    __slots__ = (
        "model_client_provider",
        "state_store",
        "keep_recent_history_rounds",
        "_background_tasks",
    )

    def __init__(
        self,
        model_client_provider: ModelClientProtocol,
        state_store: StateStoreProtocol,
        keep_recent_history_rounds: int = 5,
    ) -> None:
        """初始化 ContextCompressionProvider。

        参数:
            model_client_provider: 当前压缩器复用的模型客户端提供器。
            state_store: 当前压缩器用于创建和结束压缩任务记录的 os/store 存储。
            keep_recent_history_rounds: 压缩时保留原始形态的最近历史轮次数。
        """

        if keep_recent_history_rounds < 0:
            raise ValueError("keep_recent_history_rounds 不能小于 0")

        # 当前压缩器复用的模型客户端提供器。
        self.model_client_provider = model_client_provider

        # 当前压缩器使用的状态存储，用于创建和结束跨进程可见的后台压缩任务记录。
        self.state_store = state_store

        # 每轮压缩后仍保留原始形态的最近历史轮次数。
        self.keep_recent_history_rounds = keep_recent_history_rounds

        # 当前进程内尚未完成的后台压缩协程任务。
        self._background_tasks: set[asyncio.Task[ContextCompressionResult | None]] = set()

    async def compress_for_hard_limit(
        self,
        build_input: ContextBuildInput,
        current_result: ContextBuildResult,
        level: ContextCompressionLevel,
    ) -> bool:
        """尝试对超过硬限制的上下文执行一次同步压缩。

        参数:
            build_input: 当前轮上下文构建输入。
            current_result: 当前已经构建完成、但 token 超过硬限制的上下文结果。
            level: 当前同步硬限制兜底压缩等级。

        返回:
            若当前等级完成了一次有效压缩或裁剪，则返回 True。

        说明:
            hard limit 压缩采用明确的分级兜底策略：
            - NORMAL: 正常摘要压缩
            - DISCARD_MID_TERM_MEMORY: 丢弃已有摘要后重新压缩
            - DROP_HISTORY: 丢弃最近会话
        """

        _ = current_result
        compression_level = ContextCompressionLevel(level)
        event_id = generate_prefixed_id(PREFIX_EVENT_ID)
        event_bus_id = build_input.owner_agent.event_bus_id
        session_id = build_input.session_state.session_id
        agent_name = build_input.owner_agent.agent_name

        # 同步压缩发生在当前 run 内，需要通过事件让调用方感知硬限制兜底动作。
        await self._publish_context_compression_event(
            event_id=event_id,
            compression_mode=ContextCompressionMode.SYNC,
            event_phase=RuntimeEventPhase.STARTED,
            task=build_input.task,
            event_bus_id=event_bus_id,
            session_id=session_id,
            agent_name=agent_name,
        )

        # 分级压缩时中间级别允许失败，但是末级失败时需要抛出异常
        compression_owner_agent_state = deepcopy(build_input.owner_agent_state)
        try:
            compression_result = await self._compact_history_into_mid_term_memory(
                owner_agent_state=compression_owner_agent_state,
                level=compression_level,
            )
        except Exception as e:
            Logger.logger.warning(
                "同步压缩中发生异常："
                f"event_id={event_id}, "
                f"event_bus_id={event_bus_id}, "
                f"session_id={session_id}, "
                f"task_id={build_input.task.task_id}, "
                f"agent_name={agent_name}, ",
                exc_info=True
            )
            compression_result = ContextCompressionResult(merged=False, success=False, error=repr(e))
        compression_result.merged = self.merge_compacted_result(
            owner_agent_state=build_input.owner_agent_state,
            compression_result=compression_result,
        )

        # END 事件携带最终合并结果，collector 只会把同步压缩输出返回给本轮 run。
        await self._publish_context_compression_event(
            event_id=event_id,
            compression_mode=ContextCompressionMode.SYNC,
            event_phase=RuntimeEventPhase.END,
            task=build_input.task,
            event_bus_id=event_bus_id,
            session_id=session_id,
            agent_name=agent_name,
            compression_result=compression_result,
        )
        return compression_result.merged

    async def schedule_background_compression_if_needed(
        self,
        build_input: ContextBuildInput,
        callback: CompressionResultCallback | None = None,
    ) -> None:
        """在需要时调度一次后台压缩任务。

        参数:
            build_input: 当前轮上下文构建输入。
            callback: 后台压缩结束后的结果回调，无需压缩时不会执行。

        返回:
            无返回值。

        说明:
            该入口会先尝试通过 os/store 压缩任务存储创建一个跨进程可见的
            压缩任务记录；若已有同一会话、同一 Agent 的压缩任务正在执行，
            则直接跳过本次调度。当前后台执行体仍是进程内协程原型，后续可
            替换为真正的 worker 队列与结果合并写回。
        """

        # 先创建任务记录获取跨进程互斥权；拿不到记录表示已有压缩任务在运行。
        compression_task = await self._try_create_background_compression_task(
            build_input=build_input
        )
        if compression_task is None:
            return

        event_id = generate_prefixed_id(PREFIX_EVENT_ID)

        # 后台任务不能持有完整 ContextBuildInput，只冻结压缩需要的 BaseAgentState 快照。
        background_owner_agent_state = deepcopy(build_input.owner_agent_state)
        session_state = build_input.session_state
        background_task = asyncio.create_task(
            self._run_background_compression(
                compression_owner_agent_state=background_owner_agent_state,
                compression_task=compression_task,
                event_bus_id=build_input.owner_agent.event_bus_id,
                event_id=event_id,
                task=build_input.task,
                session_id=session_state.session_id,
                agent_name=build_input.owner_agent.agent_name,
                callback=callback,
            )
        )
        self._background_tasks.add(background_task)
        background_task.add_done_callback(self._handle_background_task_done)

    async def wait_background_compressions(self) -> None:
        """等待当前已经调度的后台压缩任务完成。

        返回:
            无返回值。

        说明:
            该方法主要服务测试、调试和后续受控关闭场景。正常请求链路不应
            依赖该方法阻塞等待，否则后台压缩就会退化成同步压缩。
        """

        if not self._background_tasks:
            return

        await asyncio.gather(*self._background_tasks, return_exceptions=True)

    @classmethod
    def merge_compacted_result(
        cls,
        owner_agent_state: BaseAgentState,
        compression_result: ContextCompressionResult,
    ) -> bool:
        """将压缩结果安全合并回目标状态。

        参数:
            owner_agent_state: 等待接收压缩结果的目标 BaseAgentState。
            compression_result: 后台或隔离压缩生成的结果对象。

        返回:
            若目标状态仍与快照匹配并完成合并，则返回 `True`；若目标状态已
            被主流程推进或改写，为避免覆盖新状态返回 `False`。若结果已经
            在压缩阶段完成合并，则直接返回 `True`。
        """
        # 压缩失败，退出
        if not compression_result.success:
            return False

        # 压缩成功，合并数据
        if compression_result.level != ContextCompressionLevel.NORMAL:
            owner_agent_state.history = list(compression_result.compressed_history)
            owner_agent_state.memory.mid_term_memory = compression_result.compressed_mid_term_memory
            return True

        # level 1 的合并操作
        # RuntimeArtifact.id 是稳定历史产物的身份边界；只要快照仍是当前 history 前缀，
        # 就可以把已压缩前缀替换成摘要结果，并保留压缩期间新增的尾部消息。
        if not cls._is_history_artifact_id_prefix_matched(
            current_history=owner_agent_state.history,
            origin_history=compression_result.origin_history,
        ):
            return False

        if (
            owner_agent_state.memory.mid_term_memory
            != compression_result.origin_mid_term_memory
        ):
            return False

        # 压缩期间新增的 history 尾部必须保留，避免后台结果覆盖新对话进展。
        appended_history = owner_agent_state.history[
            len(compression_result.origin_history) :
        ]
        owner_agent_state.memory.mid_term_memory = (
            compression_result.compressed_mid_term_memory
        )
        owner_agent_state.history = (
            deepcopy(compression_result.compressed_history) + appended_history
        )
        return True

    def _find_retained_history_start_index(
        self,
        history: list[RuntimeArtifact],
        keep_recent_history_rounds: int,
    ) -> int:
        """查找压缩后应保留的最近轮次起始位置。

        参数:
            history: 当前 Agent 已稳定落库的完整历史消息列表。
            keep_recent_history_rounds: 本次策略要求保留的最近历史轮次数。

        返回:
            应保留历史片段的起始下标；返回 0 表示全部保留，
            返回 `len(history)` 表示不保留任何历史。
        """

        if keep_recent_history_rounds < 0:
            raise ValueError("keep_recent_history_rounds 不能小于 0")
        if keep_recent_history_rounds == 0:
            return len(history)

        # 以用户消息作为会话轮次起点，保留最近 N 轮完整原始 history。
        round_start_indices: list[int] = []
        for index, artifact in enumerate(history):
            if artifact.is_user_role_message():
                round_start_indices.append(index)

        # 纯工具函数只表达“按传入轮次保留”的结果，不承载具体压缩策略。
        if len(round_start_indices) <= keep_recent_history_rounds:
            return 0

        return round_start_indices[-keep_recent_history_rounds]

    async def _try_create_background_compression_task(
        self,
        build_input: ContextBuildInput,
    ) -> ContextCompressionTaskRecord | None:
        """尝试创建当前 checkpoint 对应的后台压缩任务记录。

        参数:
            build_input: 当前轮上下文构建输入，必须已经对应一次稳定 checkpoint。

        返回:
            若可以继续调度后台压缩，则返回压缩任务记录；若已有压缩任务运行中，
            则返回 `None`。

        说明:
            正式运行环境应通过持久化存储获得跨进程互斥；本地开发可以传入
            InMemoryStateStore 保持同一套任务生命周期语义。
        """

        session_state = build_input.session_state
        return await self.state_store.try_create_context_compression_task(
            session_state=session_state,
            agent_name=build_input.owner_agent.agent_name,
        )

    @exponential_backoff_retry(retry_exceptions=(Exception,))
    async def _rewrite_mid_term_memory(
        self,
        owner_agent_state: BaseAgentState,
        history_messages: list[LLMMessage],
    ) -> str:
        """重写当前会话的中期工作记忆。

        参数:
            owner_agent_state: 当前压缩任务使用的 BaseAgentState 快照。
            history_messages: 本轮待吸收入中期记忆的较早历史消息列表。

        返回:
            新的中期工作记忆文本；若模型未返回有效文本，则返回空字符串。
        """
        summary_messages: list[LLMMessage] = [
            LLMMessage(
                role=MessageRole.SYSTEM,
                content=build_mid_term_memory_summary_prompt(),
            )
        ]

        existing_mid_term_memory = (
            owner_agent_state.memory.mid_term_memory.strip()
        )
        if existing_mid_term_memory:
            summary_messages.append(
                LLMMessage(
                    role=MessageRole.USER,
                    content=build_memory_section(
                        title=K_SUMMARY_HISTORY,
                        description="以下是会话历史摘要，请在重写时继承其中仍然有效的信息。",
                        tag_name="mid_term_memory",
                        content=existing_mid_term_memory,
                    ),
                )
            )

        summary_messages.extend(history_messages)
        summary_messages.append(
            LLMMessage(
                role=MessageRole.USER,
                content=(
                    "请输出一份融合后的完整会话历史摘要，不要追加在原摘要后面。"
                    "请严格遵守 system 消息中的 Output Format 直接输出结果"
                ),
            )
        )
        summary_message = await self.model_client_provider.generate_message(
            messages=summary_messages
        )
        summary_content = (summary_message.content or "").strip()
        return self.trim_completed_summary_items(summary_content, MAX_COMPLETED_SUMMARY_ITEMS)

    def trim_completed_summary_items(self, summary_content: str, max_items: int) -> str:
        """裁剪摘要中 Completed 栏目的较早条目。

        参数:
            summary_content: 模型生成的完整中期记忆 markdown 文本。

        返回:
            已裁剪 `Completed` 栏目的摘要文本；未找到栏目或条目未超限时返回原文本。

        说明:
            模型对“最多保留 N 条”的约束不稳定，这里用确定性后处理兜底。
            只裁剪 `Completed` 条目，不调整编号，避免破坏摘要中的稳定 id。
        """

        lines = summary_content.splitlines()
        completed_start_index = self._find_completed_section_start(lines)
        if completed_start_index is None:
            return summary_content

        completed_end_index = self._find_next_summary_section_start(
            lines, completed_start_index + 1
        )
        completed_section = lines[completed_start_index:completed_end_index]
        trimmed_section = self._trim_markdown_list_items(
            completed_section,
            max_items=max_items,
        )

        return "\n".join(
            lines[:completed_start_index]
            + trimmed_section
            + lines[completed_end_index:]
        ).strip()

    @staticmethod
    def _find_completed_section_start(lines: list[str]) -> int | None:
        """查找 `Completed` markdown 栏目的起始行。

        参数:
            lines: 摘要 markdown 文本按行切分后的内容。

        返回:
            若找到 `### Completed` 栏目则返回其行号，否则返回 `None`。
        """

        for index, line in enumerate(lines):
            if line.strip() == "### Completed":
                return index
        return None

    @staticmethod
    def _find_next_summary_section_start(lines: list[str], start_index: int) -> int:
        """查找当前 markdown 二级栏目之后的下一个栏目起点。

        参数:
            lines: 摘要 markdown 文本按行切分后的内容。
            start_index: 从该行开始向后查找。

        返回:
            下一个 `### ` 栏目的行号；若不存在则返回文本末尾行号。
        """

        for index in range(start_index, len(lines)):
            if lines[index].startswith("### "):
                return index
        return len(lines)

    @staticmethod
    def _trim_markdown_list_items(lines: list[str], max_items: int) -> list[str]:
        """保留 markdown 段落中的最近若干条列表项。

        参数:
            lines: 单个 markdown 栏目的全部行，包含栏目标题和说明文本。
            max_items: 最多保留的列表条目数。

        返回:
            裁剪后的栏目行列表。

        说明:
            条目可能存在后续说明行，因此这里按列表项块裁剪，而不是按单行裁剪。
        """

        head_lines: list[str] = []
        item_blocks: list[list[str]] = []
        current_block: list[str] | None = None

        for line in lines:
            if line.startswith("- "):
                current_block = [line]
                item_blocks.append(current_block)
                continue

            if current_block is None:
                head_lines.append(line)
            else:
                current_block.append(line)

        if len(item_blocks) <= max_items:
            return lines

        # 只保留最近条目，旧条目删除后不重排编号，保持摘要 id 的稳定含义。
        trimmed_blocks = item_blocks[-max_items:]
        trimmed_lines = head_lines[:]
        for block in trimmed_blocks:
            trimmed_lines.extend(block)
        return trimmed_lines

    def _ensure_compressible_history(
        self,
        history: list[RuntimeArtifact],
        keep_recent_history_rounds: int,
    ) -> list[RuntimeArtifact]:
        """校验并返回本轮可压缩的较早 history 消息。

        参数:
            history: 当前 Agent 已稳定落库的完整历史消息列表。
            keep_recent_history_rounds: 本次策略要求保留的最近历史轮次数。

        返回:
            本轮允许被压缩的较早历史消息列表。

        异常:
            NoCompressibleHistoryError: 当前可压缩消息数不足，不满足压缩前提。
        """

        current_round_count = Tokenizer.count_history_rounds(history)
        if current_round_count <= keep_recent_history_rounds:
            # 已触发压缩但历史轮次不足时，保留轮次降级为 0，避免压缩无效果。
            keep_recent_history_rounds = 0

        retained_start_index = self._find_retained_history_start_index(
            history=history,
            keep_recent_history_rounds=keep_recent_history_rounds,
        )
        compressible_history = history[:retained_start_index]
        if not compressible_history:
            raise NoCompressibleHistoryError(
                "当前没有足够的较早 history 可供压缩，"
                f"compressible_count={len(compressible_history)}, "
                f"keep_recent_history_rounds={keep_recent_history_rounds}"
            )
        return compressible_history

    async def _compact_history_into_mid_term_memory(
        self,
        owner_agent_state: BaseAgentState,
        level: ContextCompressionLevel = ContextCompressionLevel.NORMAL,
    ) -> ContextCompressionResult:
        """把较早 history 收敛进 `mid_term_memory`。

        参数:
            owner_agent_state: 当前压缩任务使用的 BaseAgentState；调用方可传入快照或真实对象。
            level: 当前压缩强度等级；后台压缩默认只使用 NORMAL。

        返回:
            当前压缩生成的摘要、保留 history 与被压缩 history 数量。

        异常:
            NoCompressibleHistoryError: 当前没有足够的较早 history 可供压缩。
        """
        # 记录压缩前快照，后续回调合并时用它判断目标状态是否仍可安全更新。
        origin_mid_term_memory = owner_agent_state.memory.mid_term_memory
        origin_history = deepcopy(owner_agent_state.history)

        # level 2 丢弃最近历史
        if level == ContextCompressionLevel.DROP_HISTORY:
            retained_start_index = len(owner_agent_state.history)
            compressed_history = owner_agent_state.history[retained_start_index:]
            success = len(owner_agent_state.history) > 0
            error = ""
            if not success:
                error = f"原始历史为空，无法裁剪"
            return ContextCompressionResult(
                merged=False,
                level=level,
                success=success,
                origin_mid_term_memory=origin_mid_term_memory,
                origin_history=origin_history,
                compressed_mid_term_memory="",
                compressed_history=compressed_history,
                compressed_history_count=retained_start_index,
                error=error
            )

        # level 1 直接丢弃摘要
        if level == ContextCompressionLevel.DISCARD_MID_TERM_MEMORY:

            # 二级兜底只处理“摘要过长”问题，直接丢弃摘要，保留原始 history。
            success = origin_mid_term_memory != ""
            error = ""
            if not success:
                error = f"原始中期记忆为空，无法裁剪"
            return ContextCompressionResult(
                merged=False,
                level=level,
                success=success,
                origin_mid_term_memory=origin_mid_term_memory,
                origin_history=origin_history,
                compressed_mid_term_memory="",
                compressed_history=owner_agent_state.history,
                compressed_history_count=0,
                error=error
            )

        # level 0 合并摘要优先采取的策略。
        compressible_history = self._ensure_compressible_history(
            history=owner_agent_state.history,
            keep_recent_history_rounds=self.keep_recent_history_rounds,
        )
        compressible_messages: list[LLMMessage] = []
        for history_round in ContextBuildProvider._split_history_rounds(
            compressible_history
        ):
            # 暂停任务尚未形成稳定会话结论，不应沉淀进中期记忆。
            if any(
                artifact.type == RuntimeArtifactType.INTERRUPTION_REQUEST
                for artifact in history_round
            ):
                continue

            for artifact in history_round:
                compressible_messages.extend(
                    ContextBuildProvider._artifact_to_llm_messages(artifact)
                )

        # 只把较早 history 送入摘要，尾部保留轮次继续以原始消息进入上下文。
        summary_content = await self._rewrite_mid_term_memory(
            owner_agent_state=owner_agent_state,
            history_messages=compressible_messages,
        )
        compressed_history = owner_agent_state.history[len(compressible_history) :]

        # 压缩结果进入 mid-term memory，history 只保留未被摘要化的原始尾部消息。
        owner_agent_state.memory.mid_term_memory = summary_content
        owner_agent_state.history = compressed_history
        return ContextCompressionResult(
            merged=False,
            level=level,
            origin_mid_term_memory=origin_mid_term_memory,
            origin_history=origin_history,
            compressed_mid_term_memory=summary_content,
            compressed_history=deepcopy(compressed_history),
            compressed_history_count=len(compressible_history),
        )

    @classmethod
    def _is_history_artifact_id_prefix_matched(
        cls,
        current_history: list[RuntimeArtifact],
        origin_history: list[RuntimeArtifact],
    ) -> bool:
        """判断压缩前 history 的 RuntimeArtifact.id 顺序是否仍是当前短期历史前缀。

        参数:
            current_history: 当前运行态里的短期历史产物列表。
            origin_history: 压缩启动时捕获的短期历史产物列表。

        返回:
            若快照产物 id 序列仍是当前 history 产物 id 序列的前缀，则返回 `True`。
        """

        if len(current_history) < len(origin_history):
            return False

        current_artifact_ids = [artifact.id for artifact in current_history]
        origin_artifact_ids = [artifact.id for artifact in origin_history]
        return current_artifact_ids[: len(origin_artifact_ids)] == origin_artifact_ids

    async def _run_background_compression(
        self,
        compression_owner_agent_state: BaseAgentState,
        compression_task: ContextCompressionTaskRecord,
        event_bus_id: str,
        event_id: str,
        session_id: str,
        task: BaseProcessingTask,
        agent_name: str,
        callback: CompressionResultCallback | None = None,
    ) -> ContextCompressionResult | None:
        """执行一次后台 history 收敛任务。

        参数:
            compression_owner_agent_state: 当前后台压缩任务捕获的 BaseAgentState 快照。
            compression_task: 当前后台压缩任务存储记录。
            event_bus_id: 当前 Runtime 绑定的事件通道标识。
            event_id: 当前压缩事件链路 id。
            session_id: 当前会话 id。
            agent_name: 当前压缩所属 Agent 名称。
            callback: 后台压缩结束后的结果回调。

        返回:
            后台压缩生成的结果对象。
        """
        # 后台压缩仍发送事件，但 collector 会忽略异步压缩输出，仅供外部 listener 观测。
        await self._publish_context_compression_event(
            event_id=event_id,
            compression_mode=ContextCompressionMode.ASYNC,
            event_phase=RuntimeEventPhase.STARTED,
            task=task,
            event_bus_id=event_bus_id,
            session_id=session_id,
            agent_name=agent_name,
        )

        # 后台压缩属于可选项，失败不影响主链路
        try:
            compression_result = await self._compact_history_into_mid_term_memory(
                owner_agent_state=compression_owner_agent_state,
            )
        except Exception as e:
            Logger.logger.warning(
                "后台压缩中发生异常："
                f"event_id={event_id}, "
                f"event_bus_id={event_bus_id}, "
                f"session_id={session_id}, "
                f"task_id={task.task_id}, "
                f"agent_name={agent_name}, "
                f"compression_task_id={compression_task.id}, "
                f"lease_owner={compression_task.lease_owner}", 
                exc_info=True
            )
            compression_result = ContextCompressionResult(merged=False, success=False, error=repr(e))

        # 回调负责合并和 checkpoint；成功回调后再完成任务，避免任务提前释放。
        await self._notify_compression_result(callback, compression_result)

        # 完成任务时会匹配创建时的 lease_owner，避免旧 worker 完成被抢占的新任务。
        await self.state_store.finish_context_compression_task(
            compression_task=compression_task
        )
        await self._publish_context_compression_event(
            event_id=event_id,
            compression_mode=ContextCompressionMode.ASYNC,
            event_phase=RuntimeEventPhase.END,
            task=task,
            event_bus_id=event_bus_id,
            session_id=session_id,
            agent_name=agent_name,
            compression_result=compression_result,
        )
        return compression_result

    async def _publish_context_compression_event(
        self,
        event_id: str,
        compression_mode: ContextCompressionMode,
        event_phase: RuntimeEventPhase,
        task: BaseProcessingTask,
        event_bus_id: str,
        session_id: str,
        agent_name: str,
        compression_result: ContextCompressionResult | None = None,
    ) -> None:
        """发布上下文压缩生命周期事件。

        参数:
            event_id: 当前压缩事件链路 id。
            compression_mode: 当前压缩执行模式。
            event_phase: 当前压缩事件阶段。
            compression_result: 压缩结束后的结果；END 阶段携带。
            event_bus_id: 可选事件通道标识。
            session_id: 可选会话 id。
            agent_name: 可选 Agent 名称。

        返回:
            无返回值；没有事件通道时跳过发送。
        """

        if event_bus_id is None or session_id is None:
            # 事件缺失基础字段说明 Runtime/Agent 的事件链路没有绑定成功，需要显式暴露。
            Logger.logger.warning(
                "跳过上下文压缩事件发送，缺少事件基础字段："
                f"event_id={event_id}, "
                f"compression_mode={compression_mode}, "
                f"event_phase={event_phase}, "
                f"event_bus_id={event_bus_id}, "
                f"session_id={session_id}"
                f"task_id={task_id}"
            )
            return

        await ProcessingTask.publish(
            task,
            event_bus_id,
            ContextCompressionEvent(
                event_id=event_id,
                session_id=session_id,
                agent_name=agent_name,
                task_id=task.task_id,
                compression_mode=compression_mode,
                compression_result=compression_result,
                event_phase=event_phase,
            )
        )

    async def _notify_compression_result(
        self,
        callback: CompressionResultCallback | None,
        compression_result: ContextCompressionResult,
    ) -> None:
        """通知后台压缩结果回调。

        参数:
            callback: 可选压缩结果回调。
            compression_result: 当前后台压缩结果对象。

        返回:
            无返回值；同步和异步回调都支持。
        """

        if callback is None:
            return

        callback_result = callback(compression_result)
        if inspect.isawaitable(callback_result):
            await callback_result

    def _handle_background_task_done(
        self, background_task: asyncio.Task[ContextCompressionResult | None]
    ) -> None:
        """处理后台压缩任务结束后的本地清理。

        参数:
            background_task: 当前已经结束的后台压缩协程任务。

        返回:
            无返回值。

        说明:
            这里会主动读取任务异常，避免异步任务异常变成未观察异常。
            当前阶段暂不引入分散 logger，后续接入统一观测后再在这里记录失败详情。
        """

        self._background_tasks.discard(background_task)
        try:
            task_exception = background_task.exception()
        except asyncio.CancelledError:
            return
        
        if not task_exception or isinstance(task_exception, NoCompressibleHistoryError):
            return

        Logger.logger.exception(f"压缩任务发生异常，{repr(task_exception)}", exc_info=task_exception)
        
