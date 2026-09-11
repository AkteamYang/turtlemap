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
from turtlemap.kernel.tool import BaseToolService, ExecutableTool
from turtlemap.config import ContextTokenBudget, TurtleMapConfig
from turtlemap.os.context.models import SystemInstruction
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

    __slots__ = ("config", "event_bus_id")

    def __init__(
        self,
        agent_name: str,
        system: SystemInstruction,
        config: TurtleMapConfig | None = None,
        tools: list[ExecutableTool | Any] | None = None,
        handoffs: list[BaseAgent] | None = None,
        event_bus_id: str | None = None,
    ) -> None:
        """初始化 os 层默认 Agent。

        参数:
            agent_name: Agent 的稳定业务名称。
            system: 当前 Agent 的结构化系统定义。
            config: 当前 Agent 使用的完整应用配置；为空时从环境变量读取。
            tools: 当前 Agent 直接声明的工具对象列表。
            handoffs: 当前 Agent 可 handoff 到的下游 Agent 列表。
            event_bus_id: 当前 Agent 绑定的事件通道标识；为空时由 Runtime 创建。
        """

        super().__init__(
            agent_name=agent_name,
            system=system,
            tools=tools,
            handoffs=handoffs,
        )

        # 当前 Agent 的完整应用配置，Runtime 会传给 OSService 统一分发。
        self.config = config or TurtleMapConfig.from_env()

        # 当前 Agent 绑定的事件通道；未显式传入时由 Runtime 初始化阶段创建。
        self.event_bus_id: str = event_bus_id or ""

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
    def build_tool_provider(self, tools: list[ExecutableTool | Any]) -> BaseToolService:
        """根据工具列表构建 os 层默认 ToolService。

        参数:
            tools: 当前 Agent 直接声明的工具对象列表。

        返回:
            默认使用 `os.ToolService` 的工具管理视图。
        """

        from turtlemap.os.tool.build_in import RecollectionTool

        effective_tools = list(tools)
        
        # 系统级工具在构建工具服务时固定注入，业务 Agent 不需要声明。
        effective_tools.append(RecollectionTool())
        return ToolService.from_tools(effective_tools)
