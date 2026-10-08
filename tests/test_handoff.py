#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/23 21:30
# @Author  : YaHaoo
# @File    : test_handoff.py

"""handoff 工具装配测试。"""

import pytest

from turtlemap.kernel.agent import BaseAgent
from turtlemap.kernel.exceptions import KernelRuntimeError
from turtlemap.kernel.models import SystemDefinition
from turtlemap.kernel.tool import (
    BaseToolService,
    AgentDescriptor,
    EmptyToolInputModel,
    K_HANDOFF_TOOL_ID_PREFIX,
    ToolDescriptor,
    tool,
)


def test_base_agent_registers_handoff_agent_as_executable_tool() -> None:
    """验证 BaseAgent 会将带 AgentDescriptor 的目标 Agent 注册为无参工具。"""

    target_agent = BaseAgent(
        agent_name="researcher",
        system=SystemDefinition(role="researcher", objective="研究问题"),
        descriptor=AgentDescriptor(
            summary="研究复杂问题。",
            capability_category="research",
            capability="分析复杂问题并给出研究结论。",
            use_cases=["当前任务需要独立研究时"],
            anti_use_cases=["可由当前 Agent 直接回答时"],
            examples=["把市场调研任务交给 researcher"],
        ),
    )
    root_agent = BaseAgent(
        agent_name="root",
        system=SystemDefinition(role="assistant", objective="协助用户"),
        handoffs=[target_agent],
    )

    tool = root_agent.tool_provider.get_tool_by_id(
        f"{K_HANDOFF_TOOL_ID_PREFIX}researcher"
    )

    assert tool is not None
    assert tool.tool_metadata.id == f"{K_HANDOFF_TOOL_ID_PREFIX}researcher"
    assert tool.tool_metadata.name == "researcher"
    assert tool.tool_metadata.is_handoff
    assert tool.input_model is EmptyToolInputModel
    assert tool.tool_descriptor is not None
    assert tool.tool_descriptor == target_agent.descriptor
    assert root_agent.resolve_reachable_agents() == {
        "root": root_agent,
        "researcher": target_agent,
    }


def test_tool_decorator_uses_explicit_name_for_metadata() -> None:
    """验证工具展示名由装饰器参数提供，而非 ToolDescriptor。"""

    @tool(
        name="describe_project",
        descriptor=ToolDescriptor(
            summary="描述当前项目。",
            capability_category="project",
            capability="描述当前项目。",
        ),
        input_model=EmptyToolInputModel,
    )
    def describe_project(_: EmptyToolInputModel) -> str:
        """返回项目描述。"""

        return "TurtleMap"

    executable_tool = BaseToolService.from_tools(
        agent_name="test_agent",
        tools=[describe_project],
    ).get_tool_by_id("describe_project")

    assert executable_tool is not None
    assert executable_tool.tool_metadata.id == "describe_project"
    assert executable_tool.tool_metadata.name == "describe_project"
    assert not executable_tool.tool_metadata.is_handoff
    assert executable_tool.tool_descriptor is not None
    assert executable_tool.tool_descriptor.capability == "描述当前项目。"


def test_tool_service_preserves_order_and_rejects_duplicate_ids() -> None:
    """验证工具服务按注册顺序保存工具，并通过 id 索引拒绝重复注册。"""

    first_agent = BaseAgent(
        agent_name="first",
        system=SystemDefinition(role="assistant", objective="处理第一类任务"),
        descriptor=AgentDescriptor(
            summary="处理第一类任务。",
            capability_category="first",
            capability="处理第一类任务。",
        ),
    )
    second_agent = BaseAgent(
        agent_name="second",
        system=SystemDefinition(role="assistant", objective="处理第二类任务"),
        descriptor=AgentDescriptor(
            summary="处理第二类任务。",
            capability_category="second",
            capability="处理第二类任务。",
        ),
    )
    root_agent = BaseAgent(
        agent_name="root",
        system=SystemDefinition(role="assistant", objective="协调任务"),
        handoffs=[first_agent, second_agent],
    )
    tools = root_agent.tool_provider.list_tools()

    assert [tool.tool_metadata.id for tool in tools] == [
        f"{K_HANDOFF_TOOL_ID_PREFIX}first",
        f"{K_HANDOFF_TOOL_ID_PREFIX}second",
    ]

    with pytest.raises(KernelRuntimeError, match="id 重复"):
        root_agent.tool_provider.register_tool(tools[0])
