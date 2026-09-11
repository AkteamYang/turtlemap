#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/02 15:35
# @Author  : YaHaoo
# @File    : agent.py

"""kernel 层 BaseAgent 定义。"""

from __future__ import annotations

from typing import Any

from .models import SystemDefinition
from .tool import BaseToolService, ExecutableTool


class BaseAgent:
    """表示业务代码显式创建的 Agent 运行时定义基类。

    说明:
        BaseAgent 不是纯粹的数据存储结构，后续会逐步承接
        handoff 解析、工具装配和运行期辅助行为，因此使用普通类定义。
    """

    __slots__ = (
        "agent_name",
        "system",
        "tool_provider",
        "handoffs",
    )

    def __init__(
        self,
        agent_name: str,
        system: SystemDefinition,
        tools: list[ExecutableTool | Any] | None = None,
        handoffs: list[BaseAgent] | None = None,
    ) -> None:
        """初始化 BaseAgent。

        参数:
            agent_name: Agent 的稳定业务名称。
            system: 当前 Agent 的结构化系统定义。
            tools: 当前 Agent 直接声明的工具对象列表。
            handoffs: 当前 Agent 可 handoff 到的下游 Agent 列表。
        """

        # Agent 的稳定业务名称，用于运行期解析与持久化关联。
        self.agent_name = agent_name

        # 当前 Agent 的结构化系统定义。
        self.system = system

        # 当前 Agent 持有的工具管理视图。
        self.tool_provider = self.build_tool_provider(tools or [])

        # 当前 Agent 可 handoff 到的下游 Agent。
        self.handoffs = list(handoffs or [])

    def build_tool_provider(self, tools: list[ExecutableTool | Any]) -> BaseToolService:
        """根据初始化传入的工具列表构建 BaseToolService。

        参数:
            tools: 当前 Agent 直接声明的工具对象列表。

        返回:
            适用于当前 Agent 的工具管理视图。

        说明:
            `os` 层子类可以重写该方法，返回自定义 `BaseToolService`
            或其子类实例，以实现工具选择逻辑注入。
        """

        return BaseToolService.from_tools(tools)

    def resolve_reachable_agents(self) -> dict[str, BaseAgent]:
        """递归解析当前 Agent 可达的全部 Agent。

        返回:
            以 `agent_name` 为键的 Agent 映射。
        """

        agent_name2agent: dict[str, BaseAgent] = {}
        pending_agents: list[BaseAgent] = [self]
        while pending_agents:
            agent = pending_agents.pop()
            if agent.agent_name in agent_name2agent:
                continue

            # 同名 Agent 只保留首次遇到的对象，避免 handoff 图重复展开。
            agent_name2agent[agent.agent_name] = agent
            pending_agents.extend(agent.handoffs)
        return agent_name2agent
