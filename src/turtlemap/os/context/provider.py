#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 18:32
# @Author  : YaHaoo
# @File    : provider.py

"""turtlemap os 层默认上下文构建实现。"""

from __future__ import annotations

from turtlemap.kernel.agent import BaseAgent
from turtlemap.config import ContextTokenBudget
from turtlemap.kernel.models import (
    RuntimeArtifactType,
    TaskStateKind,
)
from turtlemap.kernel.models import (
    BaseAgentState,
    Input,
    MessageState,
    BaseProcessingTask,
    RuntimeArtifact,
    ToolState,
)
from turtlemap.kernel.models.enums import TaskStatus
from turtlemap.kernel.tool import (
    ExecutableTool,
    ToolDescriptor,
)
from turtlemap.kernel.tool.models import RESERVED_UNIT_TYPE_LLM_CALL
from turtlemap.os.agent import Agent
from turtlemap.os.context.models import (
    ContextBuildInput,
    ContextBuildResult,
    ContextCompressionLevel,
    ContextSource,
    SystemInstruction,
)
from turtlemap.os.exceptions import OSRuntimeError
from turtlemap.os.llm.model import LLMMessage
from turtlemap.os.protocols import (
    CompressionResultCallback,
    ContextBuildProtocol,
    ContextCompressionProtocol,
)
from turtlemap.shared.typing import ensure_instance

from ..tool import (
    LLMCallExecutionUnit,
    ToolCallExecutionUnit,
)
from .prompt import (
    build_bullet_list,
    build_markdown_section,
    build_tagged_examples_block,
    join_prompt_sections,
)
from .projector import ContextProjector
from .tokenizer import Tokenizer

class ContextBuildProvider(ContextBuildProtocol):
    """提供 os 层默认上下文构建与任务历史收敛实现。"""

    __slots__ = (
        "tokenizer",
        "compression_provider",
        "projector",
    )

    def __init__(
        self,
        compression_provider: ContextCompressionProtocol,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        """初始化 ContextBuildProvider。

        参数:
            tokenizer: 当前上下文构建器使用的 token 统计器；为空时使用默认实现。
            compression_provider: 当前上下文构建器使用的压缩提供器。
        """

        # 当前上下文构建器持有的 token 统计器。
        self.tokenizer = tokenizer or Tokenizer()

        # 当前上下文构建器持有的压缩提供器。
        self.compression_provider = compression_provider

        # 当前上下文投影消息构建器。
        self.projector = ContextProjector()
    
    async def build_llm_context(
        self,
        build_input: ContextBuildInput,
    ) -> ContextBuildResult:
        """根据当前标准输入构建一轮最小上下文。

        参数:
            build_input: 当前轮上下文构建所需的标准输入对象。

        返回:
            可直接供模型消费的标准消息列表与工具 schema 列表。

        说明:
            当前 build 只关注硬限制收敛：
            - 每轮先按固定顺序构建 `system -> memory -> history -> current input`
            - 若未超过 `hard_limit`，则直接返回
            - 若超过 `hard_limit`，则立即执行一次同步压缩并重建
            - 若同步压缩达到最大轮数后仍未收敛，则抛出明确异常

            `soft_limit` 相关的后台治理不在这里处理，而是交给固定触发点
            `schedule_background_compression_if_needed(...)` 承接。
        """

        from turtlemap.os.agent import Agent

        owner_agent = ensure_instance(
            build_input.owner_agent, Agent, "当前 Owner Agent"
        )
        current_result = self._build_llm_context_result(
            build_input=build_input,
        )
        compression_levels = (
            ContextCompressionLevel.NORMAL,
            ContextCompressionLevel.DISCARD_MID_TERM_MEMORY,
            ContextCompressionLevel.DROP_HISTORY,
        )
        for level in compression_levels:
            trigger_hard_limit = self._should_schedule_sync_compression(
                build_input.session_state.history,
                build_input.owner_agent.context_token_budget,
                current_result.total_tokens
            )
            if not trigger_hard_limit:
                return current_result

            # 硬限制压缩按等级递进：先尝试摘要，再逐步采取更明确的裁剪动作。
            success = await self.compression_provider.compress_for_hard_limit(
                build_input=build_input,
                current_result=current_result,
                level=level
            )
            if success:
                current_result = self._build_llm_context_result(
                    build_input=build_input,
                )

        trigger_hard_limit = self._should_schedule_sync_compression(
            build_input.session_state.history,
            build_input.owner_agent.context_token_budget,
            current_result.total_tokens
        )
        if not trigger_hard_limit:
            return current_result

        raise OSRuntimeError(
                "当前上下文 token 数超过硬限制，"
                "多级压缩处理失败："
                f"agent={owner_agent.agent_name}, "
                f"total_tokens={current_result.total_tokens}, "
                f"hard_limit_tokens={owner_agent.context_token_budget.hard_limit_tokens}, "
                f"history_count={len(build_input.session_state.history)}, "
                f"has_mid_term_memory={bool(build_input.session_state.memory.mid_term_memory.strip())}"
            )

    def build_task_artifacts(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
        exclude_start_input: bool = False,
    ) -> list[RuntimeArtifact]:
        """根据任务现场构建 Runtime 产物列表。

        参数:
            owner_agent: 当前推进该任务的 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 当前需要展开的任务现场。
            exclude_start_input: 是否跳过 start_input 产物；当前输入单独构建 LLM 提示时使用。

        返回:
            可进入 LLM 上下文或在任务闭合后写入 history 的 Runtime 产物列表。

        说明:
            MessageState 会展开起始输入和已生成的 assistant 产物；
            ToolState 会展开起始输入、assistant tool_call 产物以及已完成的执行单元。
        """

        _ = owner_state

        start_input_artifact = (
            None
            if exclude_start_input
            else self._build_start_input_history_artifact(
                task=task,
                owner_agent_name=owner_agent.agent_name,
            )
        )
        start_input_artifacts = [start_input_artifact] if start_input_artifact else []

        if task.state.status == TaskStatus.PAUSED and task.interruption_request is not None:
            # 暂停任务只沉淀结构化中断请求；面向 LLM 的说明在上下文构建阶段生成。
            return [
                *start_input_artifacts,
                RuntimeArtifact(
                    type=RuntimeArtifactType.INTERRUPTION_REQUEST,
                    owner_agent_name=owner_agent.agent_name,
                    payload=task.interruption_request,
                ),
            ]

        if task.state.kind == TaskStateKind.MESSAGE:
            message_state = ensure_instance(task.state, MessageState, "消息任务状态")
            if message_state.status != TaskStatus.COMPLETED:
                # 生成前构建上下文时 message 尚未产生，此时只需要把起始输入放入上下文。
                return start_input_artifacts

            if not message_state.message:
                raise OSRuntimeError(
                    f"已完成 MessageState 缺少 assistant 产物，task_id={task.task_id}"
                )

            return [
                *start_input_artifacts,
                message_state.message,
            ]

        if task.state.kind == TaskStateKind.TOOL:
            tool_state = ensure_instance(task.state, ToolState, "工具任务状态")
            return [
                *start_input_artifacts,
                *self._collect_tool_loop_artifacts(
                    tool_state=tool_state,
                    owner_agent_name=owner_agent.agent_name,
                ),
            ]

        raise OSRuntimeError(
            f"当前 ContextBuildProvider 暂不支持构建该任务状态产物：{task.state.kind}"
        )


    async def schedule_background_compression_if_needed(
        self,
        build_input: ContextBuildInput,
        current_result: ContextBuildResult,
        callback: CompressionResultCallback | None = None,
    ) -> None:
        """在需要时调度当前 Agent 的后台上下文压缩。

        参数:
            build_input: 当前轮上下文构建输入，保留了同一次 LLM 调用的工具集合。
            current_result: 当前已经构建完成的上下文结果。
            callback: 后台压缩结束后的结果回调。

        返回:
            无返回值。

        说明:
            该入口默认偏“后台维护”而不是同步收敛：
            - 直接复用本轮 LLM 调用已经构建出的上下文结果
            - 若未达到 token 软限制和 history 轮次软限制，则不做额外动作
            - 若达到任一软限制，则尝试创建跨进程可见的后台压缩任务记录
            真正阻塞当前请求的同步压缩仍然只在 `build(...)` 的硬限制分支触发。
        """

        from turtlemap.os.agent import Agent

        effective_owner_agent = ensure_instance(
            build_input.owner_agent, Agent, "当前 Owner Agent"
        )
        context_token_budget = effective_owner_agent.context_token_budget
        if not self._should_schedule_background_compression(
            history=build_input.session_state.history,
            context_token_budget=context_token_budget,
            total_tokens=current_result.total_tokens,
        ):
            return

        await self.compression_provider.schedule_background_compression_if_needed(
            build_input=build_input,
            callback=callback,
        )

    def _build_llm_context_result(
        self,
        build_input: ContextBuildInput,
    ) -> ContextBuildResult:
        """构建一轮面向 LLMMessage 的上下文草稿结果。

        参数:
            build_input: 当前轮上下文构建所需的标准输入对象。

        返回:
            已完成消息拼装、工具 schema 拼装和 token 统计的上下文草稿。
        """

        context_source = self._build_context_source(build_input)
        messages = self.projector.build_messages_from_source(context_source)
        tool_schemas = self._build_tool_schemas(context_source.tools)
        total_tokens = self.tokenizer.count_context(
            messages=messages,
            tool_schemas=tool_schemas,
        )
        return ContextBuildResult(
            messages=messages,
            tool_schemas=tool_schemas,
            total_tokens=total_tokens,
        )

    def _build_context_source(
        self,
        build_input: ContextBuildInput,
    ) -> ContextSource:
        """汇集一次上下文构建所需的完整原材料。

        参数:
            build_input: 当前轮上下文构建所需的标准输入对象。

        返回:
            尚未展开为最终 LLM messages 的上下文原材料。
        """        
        # history
        history = list(build_input.session_state.history)

        # system
        is_subagent = len(build_input.session_state.agent_frames) > 1
        agent_name2display_name = {
            agent_name: ensure_instance(agent, Agent).name
            for agent_name, agent in build_input.agent_name2agent.items()
        }
        group_chat_agent_names = {
            artifact.owner_agent_name
            for artifact in history
            if artifact.owner_agent_name
        }
        if build_input.owner_agent.agent_name:
            group_chat_agent_names.add(build_input.owner_agent.agent_name)
        is_group_chat = len(group_chat_agent_names) > 1

        # 可接管 Agent 必须是本轮实际暴露给模型的 handoff 工具，不能依据静态配置判断。
        has_handoff = any(
            tool.tool_metadata.is_handoff
            for tool in build_input.available_tools
        )
        system_message = self.projector._build_system_llm_message(
            system=ensure_instance(
                build_input.owner_agent_state.system,
                SystemInstruction,
            ),
            is_subagent=is_subagent,
            is_group_chat=is_group_chat,
            has_handoff=has_handoff,
        )

        # memory
        memory_message = self.projector._build_memory_llm_message(
            build_input.session_state
        )

        # current task
        current_input: Input | None = None
        task_artifacts: list[RuntimeArtifact] = []
        if build_input.task.start_input is None:
            raise OSRuntimeError(
                f"构建上下文原材料时缺少 start_input, task_id={build_input.task.task_id}"
            )

        # current_input 保留原始 Input，后续消息展开时再选择 LLM 可读模板。
        current_input = build_input.task.start_input
        task_artifacts = self.build_task_artifacts(
            owner_agent=build_input.owner_agent,
            owner_state=build_input.owner_agent_state,
            task=build_input.task,
            exclude_start_input=True,  # 排除start_input，由current_input专门构建
        )

        context_source = ContextSource(
            system_message=system_message,
            is_subagent=is_subagent,
            owner_agent_name=build_input.owner_agent.agent_name,
            agent_name2display_name=agent_name2display_name,
            is_group_chat=is_group_chat,
            memory_message=memory_message,
            history=history,
            current_input=current_input,
            task_artifacts=task_artifacts,
            paused_tasks=[
                task
                for task in build_input.owner_agent_state.processing_tasks
                if task.state.status == TaskStatus.PAUSED
            ],
            tools=list(build_input.available_tools),
        )
        return self._modify_context_source(context_source)

    def _modify_context_source(
        self, context_source: ContextSource
    ) -> ContextSource:
        """按当前可用工具集合修正上下文原材料。

        参数:
            context_source: 初始上下文原材料。

        返回:
            已过滤不可用工具历史轮次后的上下文原材料。

        说明:
            history 中的 tool call 必须能被当前轮可用工具集合解释；否则该轮
            旧 history 在当前上下文中无法稳定复现，整轮过滤掉。
        """
        return context_source


    def _should_schedule_sync_compression(
        self,
        history: list[RuntimeArtifact],
        context_token_budget: ContextTokenBudget,
        total_tokens: int,
    ) -> bool:
        if total_tokens >= context_token_budget.hard_limit_tokens:
            return True

        hard_limit_rounds = context_token_budget.hard_limit_rounds
        if hard_limit_rounds is None:
            return False

        current_rounds = Tokenizer.count_history_rounds(history)
        return current_rounds >= hard_limit_rounds

    def _should_schedule_background_compression(
        self,
        history: list[RuntimeArtifact],
        context_token_budget: ContextTokenBudget,
        total_tokens: int,
    ) -> bool:
        """判断当前 history 是否需要触发后台压缩。

        参数:
            history: 当前历史。
            context_token_budget: 当前 Agent 的上下文预算配置。
            total_tokens: 当前上下文草稿的 token 估算值。

        返回:
            当 token 超过软限制，或 history 轮次超过软限制时返回 `True`。
        """

        if total_tokens >= context_token_budget.soft_limit_tokens:
            return True

        soft_limit_rounds = context_token_budget.soft_limit_rounds
        if soft_limit_rounds is None:
            return False

        current_rounds = Tokenizer.count_history_rounds(history)
        return current_rounds >= soft_limit_rounds


    def _build_start_input_history_artifact(
        self,
        task: BaseProcessingTask,
        owner_agent_name: str,
    ) -> RuntimeArtifact:
        """把任务起点输入转换成需要写入 history 的输入产物。

        参数:
            task: 当前已经闭合、等待写入历史的任务现场。
            owner_agent_name: 当前任务所属 Agent 的名称。

        返回:
            payload 为原始 `Input` 的 RuntimeArtifact。
        """

        start_input = task.start_input
        if start_input is None:
            raise OSRuntimeError(f"构建任务输入 history 时缺少 start_input, task_id={task.task_id}")

        return RuntimeArtifact(
            type=RuntimeArtifactType.INPUT,
            owner_agent_name=owner_agent_name,
            payload=start_input,
        )


    def _build_tool_schemas(
        self, available_tools: list[ExecutableTool]
    ) -> list[dict[str, object]]:
        """把当前可见工具对象列表转换为模型可消费的工具 schema。

        参数:
            available_tools: 当前轮允许暴露给模型的工具对象列表。

        返回:
            符合模型函数调用协议的工具 schema 列表。

        说明:
            当前实现只负责结构化拼装，不在这里裁剪工具；工具是否可见应当在
            `tool_provider.get_tools_with_inputs` 阶段决定。
        """

        tool_schemas: list[dict[str, object]] = []
        for executable_tool in available_tools:
            tool_metadata = executable_tool.tool_metadata
            tool_schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_metadata.id,
                        "description": self._build_tool_description(executable_tool),
                        "parameters": tool_metadata.input_schema,
                    },
                }
            )
        return tool_schemas

    def _build_tool_description(self, executable_tool: ExecutableTool) -> str:
        """根据工具结构化描述构建模型可消费的工具说明文本。

        参数:
            executable_tool: 当前待暴露给模型的工具对象。

        返回:
            面向模型消费的工具说明文本；若工具没有结构化描述，则返回空字符串。

        说明:
            当前实现会把 capability、use cases、anti use cases、
            examples 和 tags 统一收敛成稳定 Markdown 文本，避免描述拼装
            逻辑散落在 tool 定义侧。
        """

        descriptor = executable_tool.tool_descriptor
        if descriptor is None:
            return ""

        description_parts = [
            build_markdown_section("Capability", descriptor.capability.strip()),
        ]

        if descriptor.use_cases:
            description_parts.append(
                build_markdown_section(
                    "Use Cases",
                    "Use this tool when one or more of the following scenarios apply.\n\n"
                    f"{build_bullet_list(descriptor.use_cases)}",
                )
            )

        if descriptor.anti_use_cases:
            description_parts.append(
                build_markdown_section(
                    "Anti Use Cases",
                    "Do not use this tool when one or more of the following scenarios apply.\n\n"
                    f"{build_bullet_list(descriptor.anti_use_cases)}",
                )
            )

        if descriptor.examples:
            examples_block = build_tagged_examples_block(
                descriptor.examples,
                id_prefix="示例",
                id_separator="",
            )
            if examples_block:
                description_parts.append("Examples:\n" f"{examples_block}")

        return join_prompt_sections(description_parts)

    def _collect_tool_loop_artifacts(
        self,
        tool_state: ToolState,
        owner_agent_name: str,
    ) -> list[RuntimeArtifact]:
        """根据 ToolState 与执行结果重建当前 tool loop 的完整产物链。

        参数:
            tool_state: 当前已经闭合或待写历史的工具任务状态。
            owner_agent_name: 当前工具任务所属 Agent 的名称。

        返回:
            按执行顺序重建出的完整 Runtime 产物链。

        说明:
            当前实现会先写入 assistant 的原始 tool call 产物，再按 execution
            unit 顺序追加各单元对应的结果产物，保证 history 与 tool loop
            的真实推进顺序一致。
        """

        artifacts: list[RuntimeArtifact] = [tool_state.tool_call_message]
        for execution_unit in tool_state.execution_units:
            if not execution_unit.is_finished:
                continue

            if isinstance(execution_unit, ToolCallExecutionUnit):

                # 排除掉 runtime 插入的 tool 如 hitl
                if not execution_unit.from_llm:
                    continue

                artifacts.append(
                    RuntimeArtifact(
                        type=RuntimeArtifactType.TOOL_CALL_EXE,
                        owner_agent_name=owner_agent_name,
                        payload=ensure_instance(
                            execution_unit,
                            ToolCallExecutionUnit,
                            "工具调用执行单元",
                        ),
                    )
                )
                continue

            if execution_unit.unit_type == RESERVED_UNIT_TYPE_LLM_CALL:
                artifacts.append(
                    RuntimeArtifact(
                        type=RuntimeArtifactType.TOOL_LLM_RESPONSE,
                        owner_agent_name=owner_agent_name,
                        payload=ensure_instance(
                            execution_unit,
                            LLMCallExecutionUnit,
                            "LLM 调用执行单元",
                        ),
                    )
                )
                continue

            raise OSRuntimeError(
                f"当前 ContextBuildProvider 暂不支持收集该执行单元产物：{execution_unit.unit_type}"
            )
        return artifacts

    def _message_to_artifact(
        self,
        message: LLMMessage,
        owner_agent_name: str,
        artifact_type: RuntimeArtifactType | None = None,
    ) -> RuntimeArtifact:
        """把 os 侧 LLMMessage 包装为 RuntimeArtifact。

        参数:
            message: 工具链路中已有的 LLM 消息对象。
            owner_agent_name: 生成当前消息产物的 Agent 名称。
            artifact_type: 外部已确定的产物类型；为空时根据消息内容判断 assistant 类型。

        返回:
            payload 为 `LLMMessage` 的 Runtime 产物。
        """

        return RuntimeArtifact(
            type=artifact_type
            or (
                RuntimeArtifactType.TOOL_CALL
                if message.tool_calls
                else RuntimeArtifactType.ASSISTANT_MESSAGE
            ),
            owner_agent_name=owner_agent_name,
            payload=message,
        )
