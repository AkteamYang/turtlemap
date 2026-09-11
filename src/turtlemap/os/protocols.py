#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/17 00:00
# @Author  : YaHaoo
# @File    : protocols.py

"""turtlemap os 层统一协议定义。"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, TypeAlias

from pydantic import BaseModel

from turtlemap.kernel.agent import BaseAgent
from turtlemap.kernel.models import (
    BaseAgentState,
    BaseSessionState,
    BaseProcessingTask,
    SessionStateSaveKind,
)
from turtlemap.kernel.models.models import RuntimeArtifact
from turtlemap.kernel.tool import ExecutableTool, ToolResult
from turtlemap.os.context.models import ContextBuildInput, ContextCompressionLevel

if TYPE_CHECKING:
    from turtlemap.os.llm.model import LLMCompletionChunk, LLMMessage, LLMToolChoice
    from turtlemap.os.context.models import (
        ContextBuildResult,
        ContextCompressionResult,
        ContextCompressionTaskRecord,
    )
    from turtlemap.os.models import SessionState

CompressionResultCallback: TypeAlias = Callable[
    ["ContextCompressionResult"], Awaitable[None] | None
]


class ModelClientProtocol(Protocol):
    """定义 os 层模型调用 provider 协议。

    说明:
        该协议只暴露 OSService 与上下文压缩链路需要依赖的模型调用能力，
        具体客户端创建、服务商适配和请求参数组织由实现类自行负责。
    """

    async def generate_message(
        self,
        messages: list[LLMMessage],
        tool_schemas: list[dict[str, object]] | None = None,
    ) -> LLMMessage:
        """执行一次普通或带工具的模型推理。

        参数:
            messages: 当前轮传给模型的 LLM 消息列表。
            tool_schemas: 当前轮可供模型选择的工具描述列表；为空时表示纯文本推理。

        返回:
            模型返回的 assistant 消息对象。
        """

        ...

    def stream_generate_message(
        self,
        messages: list[LLMMessage],
        tool_schemas: list[dict[str, object]] | None = None,
        tool_choice: LLMToolChoice | None = None,
    ) -> AsyncIterable[LLMCompletionChunk]:
        """执行一次普通或带工具的流式模型推理。

        参数:
            messages: 当前轮传给模型的 LLM 消息列表。
            tool_schemas: 当前轮可供模型选择的工具描述列表；为空时表示纯文本推理。
            tool_choice: 当前轮工具选择策略；为空时由实现按工具列表决定默认策略。

        返回:
            逐段产出模型流式响应 chunk 的异步可迭代对象。
        """

        ...


class StateStoreProtocol(Protocol):
    """定义 os 层 Runtime 状态存储协议。

    说明:
        该协议覆盖 OSService 主链路需要的会话状态、长期记忆
        以及上下文压缩任务相关数据库读写能力。AgentState 跟随
        SessionState 一起保存；会话展示历史由应用层根据
        Runtime 输出或 event bus 自行保存，不进入 SDK 状态存储协议。
    """

    async def save_session_state(
        self,
        session_state: BaseSessionState,
        save_kind: SessionStateSaveKind,
    ) -> None:
        """按指定保存语义保存当前会话状态。

        参数:
            session_state: 待保存的会话状态对象。
            save_kind: 当前保存动作对应的保存语义类型。
        """

        ...

    async def load_agent_long_term_memory(
        self,
        session_state: BaseSessionState,
        agent_name: str,
    ) -> str:
        """加载指定 Agent 当前生效的长期记忆文本。

        参数:
            session_state: 当前会话状态；实现可收窄为 os SessionState 读取用户维度。
            agent_name: 待加载长期记忆的 Agent 名称。

        返回:
            当前 Agent 在跨会话维度上的长期记忆文本。
        """

        ...

    async def try_create_context_compression_task(
        self,
        session_state: BaseSessionState,
        agent_name: str,
    ) -> ContextCompressionTaskRecord | None:
        """尝试为指定会话原子创建一个上下文压缩任务。

        参数:
            session_state: 当前会话状态，实现层可从中读取存储所需上下文。
            agent_name: 当前需要压缩治理的 Agent 名称。

        返回:
            若成功创建或抢占压缩任务则返回任务记录，否则返回 None。
        """

        ...

    async def finish_context_compression_task(
        self, compression_task: "ContextCompressionTaskRecord"
    ) -> None:
        """将上下文压缩任务标记为已完成。

        参数:
            compression_task: 当前需要结束的压缩任务记录；实现应校验 lease_owner。
        """

        ...

    async def save_context_compression_result(
        self,
        session_state: BaseSessionState,
        agent_name: str,
        compression_result: "ContextCompressionResult",
    ) -> None:
        """保存后台压缩结果。

        参数:
            session_state: 调度压缩时使用的会话状态；实现层可从中读取会话身份和 schema 信息。
            agent_name: 当前压缩结果所属 Agent 名称。
            compression_result: 当前后台压缩完成后的结果对象。

        说明:
            实现层应自行读取当前最新会话状态，并只在压缩前缀仍匹配时合并写入，
            避免后台压缩结果覆盖新的会话进展。
        """

        ...


class ExecutorProtocol(Protocol):
    """定义 os 层后台工具执行器协议。

    说明:
        该协议只描述 OSService 调度后台工具任务时依赖的提交与查询能力。
        是否使用进程内执行、远程队列或外部任务系统，由具体实现决定。
    """

    async def submit(
        self,
        tool_call_id: str,
        tool: ExecutableTool,
        tool_input: BaseModel,
    ) -> None:
        """提交后台工具任务。

        参数:
            tool_call_id: 当前工具调用的唯一标识。
            tool: 当前待执行的工具对象。
            tool_input: 当前工具调用已完成校验和上下文绑定的输入模型。
        """

        ...

    async def poll(self, tool_call_id: str) -> ToolResult | None:
        """查询后台工具任务稳定结果。

        参数:
            tool_call_id: 当前待查询的工具调用唯一标识。

        返回:
            若后台任务已形成稳定结果则返回工具结果，否则返回 None。
        """

        ...


class ContextCompressionProtocol(Protocol):
    """定义 os 层上下文压缩治理协议。

    说明:
        该协议覆盖上下文构建链路需要依赖的同步硬限制压缩、后台软限制调度，
        以及受控关闭或测试场景下等待后台压缩完成的能力。
    """

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
        """

        ...

    async def schedule_background_compression_if_needed(
        self,
        build_input: ContextBuildInput,
        callback: CompressionResultCallback | None = None,
    ) -> None:
        """在需要时调度一次后台压缩任务。

        参数:
            build_input: 当前轮上下文构建输入。
            callback: 后台压缩结束后的结果回调。
        """

        ...

    async def wait_background_compressions(self) -> None:
        """等待当前已经调度的后台压缩任务完成。"""

        ...


class ContextBuildProtocol(Protocol):
    """定义 os 层上下文构建协议。

    说明:
        该协议覆盖 OSService 需要依赖的 LLM 上下文构建、任务 history 收敛，
        以及 history 更新后的上下文治理入口。
    """

    async def build_llm_context(
        self,
        build_input: ContextBuildInput,
    ) -> ContextBuildResult:
        """根据当前标准输入构建一轮 LLM 上下文。

        参数:
            build_input: 当前轮上下文构建所需的标准输入对象。

        返回:
            可直接供模型消费的消息、工具 schema 与 token 统计结果。
        """

        ...

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
        """

        ...

    async def schedule_background_compression_if_needed(
        self,
        build_input: ContextBuildInput,
        current_result: ContextBuildResult,
        callback: CompressionResultCallback | None = None,
    ) -> None:
        """在需要时调度当前 Agent 的后台上下文压缩。

        参数:
            build_input: 当前轮上下文构建输入。
            current_result: 当前已经构建完成的上下文结果。
            callback: 后台压缩结束后的结果回调。
        """

        ...
