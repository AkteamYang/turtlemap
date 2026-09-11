#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/08/28 15:26
# @Author  : YaHaoo
# @File    : models.py

"""turtlemap os 层上下文构建与压缩模型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from turtlemap.kernel.models import (
    BaseAgentState,
    BaseStateModel,
    Input,
    BaseProcessingTask,
    RuntimeArtifact,
    SystemDefinition,
)
from turtlemap.kernel.tool.models import JsonDict

if TYPE_CHECKING:
    from turtlemap.kernel.tool import ExecutableTool
    from turtlemap.os.agent import Agent
    from turtlemap.os.llm.model import LLMMessage
    from turtlemap.os.models import SessionState


K_FRAMEWORK_INSTRUCTION = "Framework Instructions"
K_AGENT_DEFINITION = "Agent Definition"


@SystemDefinition.register_type
class SystemInstruction(SystemDefinition):
    """表示 os 层可渲染为 system prompt 的 Agent 系统提示词。

    说明:
        kernel `SystemDefinition` 只表达 Agent 的业务定义，不绑定 prompt
        展示结构。os 层在构建 LLM 上下文时使用该模型补充固定标题与说明。
    """

    # 当前 os 层系统提示词多态类型名，避免与 kernel SystemDefinition 注册冲突。
    type_name: str = "system_instruction"

    # 系统定义在 prompt 中使用的顶层区块标题。
    title: str = K_AGENT_DEFINITION

    # 系统定义的整体说明文本。
    description: str = "本次任务对应提示词"


@dataclass(slots=True)
class FrameworkInstruction:
    """表示 os 层框架级提示词规则。

    说明:
        框架级提示词不是 Agent 角色定义，不应复用 `SystemDefinition` 的
        Role / Objective 结构；它只承载所有 Agent 都必须遵守的前置规则。
    """

    # 框架级提示词区块标题。
    title: str = K_FRAMEWORK_INSTRUCTION

    # 框架级提示词的整体说明。
    description: str = "以下为 Runtime 注入的框架级规则，最高优先级，严禁覆盖。"

    # 框架规则中关于提示词优先级的统一说明。
    priority: str = ""

    # 框架规则中关于记忆上下文的统一说明。
    memory: str = ""

    # 框架规则中关于工具调用行为的统一说明。
    tools: str = ""

    # 框架规则中关于事实来源采用策略的统一说明。
    fact_sources: str = ""


class ContextCompressionTaskStatus(str, Enum):
    """上下文压缩任务生命周期状态。"""

    # 压缩任务已被创建，尚未完成结果写回。
    RUNNING = "running"

    # 压缩任务已经完成，并且结果已完成写回或被判定无需写回。
    COMPLETED = "completed"

    # 压缩任务执行失败，后续可由治理任务决定是否重试。
    FAILED = "failed"


class ContextCompressionLevel(int, Enum):
    """上下文压缩强度等级。

    说明:
        level 只描述同步硬限制兜底时的递进策略。后台压缩始终使用
        `NORMAL`，避免后台任务主动丢弃上下文信息。
    """

    # 默认压缩：融合较早 history 和已有 mid-term memory，并保留最近配置轮次。
    NORMAL = 0

    # 丢弃已有历史摘要：只基于可压缩 history 生成新摘要，规避旧摘要过长。
    DISCARD_MID_TERM_MEMORY = 1

    # 强裁剪：丢弃最近历史。
    DROP_HISTORY = 2


class ContextCompressionTaskRecord(BaseStateModel):
    """表示上下文压缩任务的通用运行记录。

    说明:
        该模型服务 OS 层后台压缩调度协议。具体存储实现可以直接复用该结构，
        也可以在业务层转换为更细的 persistence model。
    """

    # 压缩任务持久化记录 id。
    id: int

    # 所属会话唯一标识。
    session_id: str

    # Agent 稳定业务名称。
    agent_name: str

    # 压缩任务状态，例如 running / completed / failed。
    status: ContextCompressionTaskStatus

    # 当前租约持有者。
    lease_owner: str | None = None

    # 当前租约过期时间。
    lease_expires_at: datetime | None = None

    # 失败原因。
    error: str | None = None

    # 创建时间。
    created_at: datetime | None = None

    # 更新时间。
    updated_at: datetime | None = None


@dataclass(slots=True)
class ContextBuildInput:
    """表示 os 层一次上下文构建的最小输入。

    说明:
        该对象只服务 os 层上下文构建、压缩和模型调用准备流程。kernel 不应
        理解具体的会话类型、Agent 类型或工具对象集合。
    """

    # 当前 os 会话状态。
    session_state: "SessionState"

    # 当前拥有控制权的 os Agent 对象。
    owner_agent: "Agent"

    # 当前 Owner Agent 对应的主观状态。
    owner_agent_state: BaseAgentState

    # 当前正在构建上下文的任务现场
    task: BaseProcessingTask

    # 当前轮候选可用工具对象列表。
    available_tools: list["ExecutableTool"] = field(default_factory=list)


@dataclass(slots=True)
class ContextProjection:
    """表示上下文构建过程中的原始数据投影。

    说明:
        投影对象保存进入上下文构建的直接数据，而不是最终 LLM messages。
        这样后续插件层可以在消息展开前检查、改写或观测上下文组成。
    """

    # 当前 Agent 的 system 消息；系统定义为空时允许不存在。
    system_message: LLMMessage | None = None

    # 当前 Agent 的 memory 消息；长期和中期记忆都为空时允许不存在。
    memory_message: LLMMessage | None = None

    # 已稳定进入历史的 Runtime 产物列表。
    history: list[RuntimeArtifact] = field(default_factory=list)

    # 当前任务起点输入；后台治理类构建允许不存在。
    current_input: Input | None = None

    # 当前任务过程中已经产生但尚未写入稳定 history 的 Runtime 产物。
    task_artifacts: list[RuntimeArtifact] = field(default_factory=list)

    # 当前轮候选可用工具对象列表，最终展开上下文结果时再转换为模型 schema。
    tools: list["ExecutableTool"] = field(default_factory=list)


@dataclass(slots=True)
class ContextBuildResult:
    """表示一次上下文构建的最小输出。

    说明:
        当前结果除了保留模型调用所需的 `messages` 和 `tool_schemas`，
        也显式记录本轮构建完成后的 `total_tokens`，便于后续观测、
        预算判断和压缩入口直接复用，而不必重复统计。
    """

    # 提供给模型的标准消息列表。
    messages: list[LLMMessage]

    # 提供给模型的工具 schema 列表。
    tool_schemas: list[JsonDict] = field(default_factory=list)

    # 当前构建结果对应的总 token 数。
    total_tokens: int = 0


class ContextCompressionResult(BaseModel):
    """表示一次上下文压缩完成后的结果。

    说明:
        该结果既承载压缩生成的摘要内容，也记录压缩结果是否成功合并回原始
        运行状态，便于后台任务完成后通过回调继续做 checkpoint 或观测。
    """

    # 压缩结果是否已经成功合并回原始状态。
    merged: bool

    # 压缩等级，用于观测同步硬限制兜底采取了哪一级策略。
    level: ContextCompressionLevel = ContextCompressionLevel.NORMAL

    # 是否压缩成功
    success: bool = True

    # 压缩前的原始中期记忆文本，用于回调侧安全合并。
    origin_mid_term_memory: str = ""

    # 压缩前的原始短期 history 快照，用于回调侧校验合并边界。
    origin_history: list[RuntimeArtifact] = Field(default_factory=list, exclude=True)

    # 本次压缩生成的中期记忆摘要文本；未实际压缩时为空字符串。
    compressed_mid_term_memory: str = ""

    # 压缩后仍保留在短期 history 中的 Runtime 产物列表。
    compressed_history: list[RuntimeArtifact] = Field(default_factory=list, exclude=True)

    # 本次被吸收到摘要中的 history 产物数量。
    compressed_history_count: int = 0

    error: str = ""
