#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/10/09 15:47
# @Author  : YaHaoo
# @File    : test_handoff_return.py

"""handoff 控制权归还工具测试。"""

from __future__ import annotations

import asyncio

from turtlemap.kernel.models import (
    AgentFrame,
    BaseAgentState,
    BaseProcessingTask,
    MessageRole,
    MessageState,
    RuntimeArtifact,
    RuntimeArtifactType,
)
from turtlemap.kernel.models.enums import (
    AgentFrameChangeReason,
    AgentFrameChangeType,
)
from turtlemap.kernel.tool import EmptyToolInputModel, ToolExecutionContext
from turtlemap.kernel.tool.models import K_RAW_DATA_HANDOFF
from turtlemap.os import Agent
from turtlemap.os.context.models import SystemInstruction
from turtlemap.os.context.projector import ContextProjector
from turtlemap.os.llm.model import LLMCompletionToolCall, LLMFunction, LLMMessage
from turtlemap.os.models import ProcessingTask, SessionState
from turtlemap.os.service import OSService
from turtlemap.os.tool.build_in.handoff_return import (
    HandoffReturnTool,
    K_TOOL_NAME_HANDOFF_RETURN,
)
from turtlemap.os.tool.model import AgentFrameChange, ToolInputContext
from turtlemap.os.tool.tool import LLMCallExecutionUnit, ToolCallExecutionUnit
from turtlemap.shared.typing import ensure_instance


def test_handoff_return_builds_parent_pop_frame_change(monkeypatch) -> None:
    """验证子 Agent 归还工具生成父 Agent 的 POP 控制权变更。"""

    root_agent = Agent(
        agent_name="root_agent",
        system=SystemInstruction(
            name="主客服",
            role="协调客服",
            objective="处理用户问题。",
        ),
    )
    subagent = Agent(
        agent_name="after_sales_agent",
        system=SystemInstruction(
            name="售后客服",
            role="售后客服",
            objective="处理售后问题。",
        ),
    )
    session_state = SessionState(
        agent_frames=[
            AgentFrame(agent_name=root_agent.agent_name),
            AgentFrame(agent_name=subagent.agent_name),
        ],
    )
    tool_service = subagent.tool_provider
    tool_service._os_service = OSService(session_state=session_state)

    async def fake_publish(*args: object, **kwargs: object) -> None:
        """跳过工具调用事件发布，保持测试聚焦于控制权归还单元构建。"""

        _ = (args, kwargs)

    monkeypatch.setattr(ProcessingTask, "publish", staticmethod(fake_publish))
    tool_execution_context = ToolExecutionContext(
        agent=subagent,
        agent_name2agent={
            root_agent.agent_name: root_agent,
            subagent.agent_name: subagent,
        },
        owner_state=BaseAgentState(
            agent_name=subagent.agent_name,
            system=subagent.system,
        ),
        session_state=session_state,
        task=BaseProcessingTask(state=MessageState()),
    )
    input_model = EmptyToolInputModel()
    ToolInputContext(
        tool_execution_context=tool_execution_context,
    ).bind_to_input_model(input_model)

    tool_result = asyncio.run(HandoffReturnTool().call(input_model))
    agent_frame_change = ensure_instance(
        tool_result.raw_data[K_RAW_DATA_HANDOFF],
        AgentFrameChange,
        "handoff return 测试控制权变更",
    )

    assert agent_frame_change.source == subagent.agent_name
    assert agent_frame_change.target == root_agent.agent_name
    assert agent_frame_change.source_display_name == subagent.name
    assert agent_frame_change.target_display_name == root_agent.name
    assert agent_frame_change.type == AgentFrameChangeType.POP
    assert agent_frame_change.reason == AgentFrameChangeReason.HANDOFF_RETURN_TOOL

    execution_unit = ToolCallExecutionUnit(
        tool_meta_id=K_TOOL_NAME_HANDOFF_RETURN,
        tool_call=LLMCompletionToolCall(
            id="call:handoff-return",
            function=LLMFunction(
                name=K_TOOL_NAME_HANDOFF_RETURN,
                arguments="{}",
            ),
        ),
    )
    assert execution_unit.is_handoff_return

    task = ProcessingTask(state=MessageState())
    units = asyncio.run(
        tool_service.build_tool_execution_units(
            agent=subagent,
            task=task,
            message=RuntimeArtifact(
                type=RuntimeArtifactType.TOOL_CALL,
                payload=LLMMessage(
                    role=MessageRole.ASSISTANT,
                    available_funtion_names=[K_TOOL_NAME_HANDOFF_RETURN],
                    tool_calls=[execution_unit.tool_call],
                ),
            ),
        )
    )

    assert len(units) == 1
    assert isinstance(units[0], ToolCallExecutionUnit)
    assert not any(isinstance(unit, LLMCallExecutionUnit) for unit in units)


def test_context_projector_keeps_handoff_return_run_message() -> None:
    """验证显式归还控制权会补充 assistant 消息闭合工具调用链。"""

    projector = ContextProjector()
    auto_return_artifact = RuntimeArtifact(
        type=RuntimeArtifactType.AGENT_FRAME_CHANGE,
        payload=AgentFrameChange(
            source="after_sales_agent",
            target="root_agent",
            type=AgentFrameChangeType.POP,
            reason=AgentFrameChangeReason.HANDOFF_RETURN,
            target_display_name="主客服",
        ),
    )
    explicit_return_artifact = RuntimeArtifact(
        type=RuntimeArtifactType.AGENT_FRAME_CHANGE,
        payload=AgentFrameChange(
            source="after_sales_agent",
            target="root_agent",
            type=AgentFrameChangeType.POP,
            reason=AgentFrameChangeReason.HANDOFF_RETURN_TOOL,
            target_display_name="主客服",
        ),
    )

    assert projector._artifact_to_llm_messages(auto_return_artifact) == []

    messages = projector._artifact_to_llm_messages(explicit_return_artifact)

    assert len(messages) == 1
    assert messages[0].role == MessageRole.ASSISTANT
    assert messages[0].content == "接下来将由 `主客服` 为您处理。"
