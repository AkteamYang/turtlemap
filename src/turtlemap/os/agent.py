#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/04 12:10
# @Author  : YaHaoo
# @File    : agent.py

"""turtlemap os 层 Agent 实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from typing_extensions import override

from turtlemap.kernel.agent import BaseAgent
from turtlemap.kernel.tool import AgentDescriptor, ExecutableTool
from turtlemap.config import ContextTokenBudget, TurtleMapConfig
from turtlemap.os.context.models import SystemInstruction
from turtlemap.os.tool.build_in.handoff_return import HandoffReturnTool
from turtlemap.os.tool.build_in.resume_task import ResumeTaskTool
from turtlemap.shared.typing import ensure_instance

from .tool import ToolService

if TYPE_CHECKING:
    from turtlemap.os.service import OSService


class Agent(BaseAgent):
    """表示带有 os 层默认工具服务的默认业务 Agent。

    说明:
        `kernel.BaseAgent` 只约定 `build_tool_provider` 这个扩展点；
        当前 `Agent` 在此基础上接入 `os.ToolService`，让业务侧默认
        使用 os 层的工具管理与 ExecutionUnit 构造逻辑。
    """

    # os Agent 统一使用支持执行单元构造的 ToolService。
    tool_service_cls = ToolService

    __slots__ = ("config",)

    def __init__(
        self,
        agent_name: str,
        system: SystemInstruction,
        config: TurtleMapConfig | None = None,
        tools: list[ExecutableTool | Any] | None = None,
        handoffs: list[BaseAgent] | None = None,
        descriptor: AgentDescriptor | None = None,
    ) -> None:
        """初始化 os 层默认 Agent。

        参数:
            agent_name: Agent 的稳定业务名称。
            system: 当前 Agent 的结构化系统定义。
            config: 当前 Agent 使用的完整应用配置；为空时从环境变量读取。
            descriptor: 当前 Agent 作为 handoff 目标时对模型暴露的工具描述。
            tools: 当前 Agent 直接声明的工具对象列表。
            handoffs: 当前 Agent 可委派给下游 Agent 的目标 Agent 列表。
        """

        super().__init__(
            agent_name=agent_name,
            system=system,
            descriptor=descriptor,
            tools=tools,
            handoffs=handoffs,
        )

        # 当前 Agent 的完整应用配置，Runtime 会传给 OSService 统一分发。
        self.config = config or TurtleMapConfig.from_env()

    @property
    def name(self) -> str:
        """读取 Agent 的对外展示名称。

        返回:
            当前 `SystemInstruction.name` 中定义的展示名称。

        说明:
            `name` 用于用户与其他 Agent 可见的身份表达；运行期解析、持久化和工具 id
            仍使用稳定的 `agent_name`。
        """

        return ensure_instance(self.system, SystemInstruction).name or self.agent_name

    @property
    def context_token_budget(self) -> ContextTokenBudget:
        """读取当前 Agent 的上下文 token 预算配置。

        返回:
            当前完整应用配置中的上下文预算对象。
        """

        return self.config.context_token_budget

    def bind_service(self, os_service: "OSService") -> None:
        """绑定当前 Runtime 使用的 os 层统一服务。

        参数:
            os_service: 当前 Runtime 已创建并注入的 OSService 实例。

        说明:
            ToolService 在执行 tool loop continuation 时需要回调 OSService，
            因此 Agent 在 Runtime 装配阶段需要把服务对象继续传给工具服务。
        """
        tool_provider = ensure_instance(self.tool_provider, ToolService)
        tool_provider._os_service = os_service

    @override
    def build_tool_provider(self, tools: list[ExecutableTool | Any]) -> ToolService:
        """根据工具列表构建 os 层默认 ToolService。

        参数:
            tools: 当前 Agent 直接声明的工具对象列表。

        返回:
            默认使用 `os.ToolService` 的工具管理视图。
        """

        from turtlemap.os.tool.build_in import RecollectionTool

        # 系统级工具固定位于最前，业务 Agent 不需要显式声明。
        system_tools = [RecollectionTool(), ResumeTaskTool(), HandoffReturnTool()]

        # 复用 kernel 对系统工具、业务工具与 handoff 工具的统一装配。
        return ensure_instance(
            super().build_tool_provider(system_tools + tools),
            ToolService,
        )
