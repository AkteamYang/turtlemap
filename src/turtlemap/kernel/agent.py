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

from typing import Any, Callable

from .exceptions import KernelUnsupportedOperationError
from .models import SystemDefinition
from .tool import (
    BaseToolService,
    AgentDescriptor,
    EmptyToolInputModel,
    ExecutableTool,
    K_HANDOFF_TOOL_ID_PREFIX,
    ToolMetadata,
    ToolResult,
)


class BaseAgent:
    """表示业务代码显式创建的 Agent 运行时定义基类。

    说明:
        BaseAgent 不是纯粹的数据存储结构，后续会逐步承接
        handoff 解析、工具装配和运行期辅助行为，因此使用普通类定义。
    """

    # 当前 Agent 默认构建的工具服务类型；派生 Agent 可通过类属性覆盖。
    tool_service_cls: type[BaseToolService] = BaseToolService

    __slots__ = (
        "agent_name",
        "system",
        "descriptor",
        "tool_provider",
        "handoffs",
    )

    def __init__(
        self,
        agent_name: str,
        system: SystemDefinition,
        tools: list[ExecutableTool | Callable[..., Any]] | None = None,
        handoffs: list[BaseAgent] | None = None,
        descriptor: AgentDescriptor | None = None,
    ) -> None:
        """初始化 BaseAgent。

        参数:
            agent_name: Agent 的稳定业务名称。
            system: 当前 Agent 的结构化系统定义。
            descriptor: 当前 Agent 作为 handoff 目标时对模型暴露的工具描述。
            tools: 当前 Agent 直接声明的工具对象列表。
            handoffs: 当前 Agent 可委派给下游 Agent 的目标 Agent 列表。
        """

        # Agent 的稳定业务名称，用于运行期解析与持久化关联。
        self.agent_name = agent_name

        # 当前 Agent 的结构化系统定义。
        self.system = system

        # 当前 Agent 被委派时对模型暴露的工具描述；为空时不能作为 handoff 目标。
        self.descriptor = descriptor

        # 当前 Agent 可 handoff 到的下游 Agent。
        self.handoffs = list(handoffs or [])

        # 当前 Agent 持有的工具管理视图；handoff 会在这里一并注册为工具。
        self.tool_provider = self.build_tool_provider(tools or [])

    def build_tool_provider(self, tools: list[ExecutableTool | Callable[..., Any]]) -> BaseToolService:
        """根据初始化传入的工具列表构建 BaseToolService。

        参数:
            tools: 当前 Agent 直接声明的工具对象列表。

        返回:
            适用于当前 Agent 的工具管理视图。

        说明:
            `os` 层子类可以重写该方法，返回自定义 `BaseToolService`
            或其子类实例，以实现工具选择逻辑注入。
        """

        return self.tool_service_cls.from_tools(
            agent_name=self.agent_name,
            tools=self._build_tools_with_handoffs(tools),
        )

    def _build_tools_with_handoffs(
        self,
        tools: list[ExecutableTool | Callable[..., Any]],
    ) -> list[ExecutableTool | Callable[..., Any]]:
        """将当前 Agent 的 handoff 定义转换为可注册工具。

        参数:
            tools: 当前 Agent 直接声明的原始工具列表。

        返回:
            追加 handoff 对应 ExecutableTool 后的工具列表。

        说明:
            handoff 当前仅完成模型可见工具的装配。调用后的任务创建、控制权切换
            和返回逻辑仍由后续 Runtime 调度链路实现。
        """

        handoff_tools = [
            self._build_handoff_executable_tool(handoff)
            for handoff in self.handoffs
        ]
        return list(tools) + handoff_tools

    @staticmethod
    def _build_handoff_executable_tool(handoff_agent: "BaseAgent") -> ExecutableTool:
        """构建单个 handoff 对应的无参 ExecutableTool。

        参数:
            handoff_agent: 当前待转换为 handoff 工具的目标 Agent。

        返回:
            工具名称为目标 Agent 名称、稳定 id 带 handoff 前缀的工具对象。
        """

        descriptor = handoff_agent.descriptor
        if descriptor is None:
            raise KernelUnsupportedOperationError(
                "handoff 目标 Agent 缺少 AgentDescriptor："
                f"agent_name={handoff_agent.agent_name}"
            )

        return ExecutableTool(
            tool_metadata=ToolMetadata.model(
                name=handoff_agent.agent_name,
                tool_id=f"{K_HANDOFF_TOOL_ID_PREFIX}{handoff_agent.agent_name}",
                input_model=EmptyToolInputModel,
            ),
            tool_descriptor=descriptor,
            input_model=EmptyToolInputModel,
            call=BaseAgent._unsupported_handoff_tool_call,
        )

    @staticmethod
    def _unsupported_handoff_tool_call(_: EmptyToolInputModel) -> ToolResult:
        """阻止未接入调度链路的 handoff 工具被当作普通工具执行。

        参数:
            _: handoff 工具固定无参数，当前值不参与处理。

        返回:
            无返回值。

        异常:
            KernelUnsupportedOperationError: handoff 调度尚未实现时抛出。
        """

        raise KernelUnsupportedOperationError("handoff 调度链路尚未实现")

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
