#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/28 13:39
# @Author  : YaHaoo
# @File    : recollection.py

"""os 层内置回忆工具定义。"""

from __future__ import annotations

import re
from typing import ClassVar

from pydantic import BaseModel, Field

from turtlemap.kernel.models.enums import TaskStatus
from turtlemap.kernel.tool import ToolDescriptor, ToolMetadata, ToolResult
from turtlemap.kernel.tool.models import ToolFactSourceType
from turtlemap.os.tool.enums import BuiltinToolCapabilityCategory
from turtlemap.os.tool.model import ToolInputContext
from turtlemap.shared.typing import ensure_instance

from .base import BuildinTool

K_TOOL_NAME_RECOLLECTION = "_recollection"


class RecollectionQuery(BaseModel):
    """表示系统级历史会话回忆工具的输入参数。

    说明:
        当前模型负责把用户的指代问题改写成适合检索的独立语义描述；
        真实回忆能力后续会在该内置工具内部继续接入 memory / observation。
    """

    # 用于回忆历史信息的独立查询陈述句。
    query: str = Field(
        description=(
            "用于历史回忆的独立查询，必须是消除指代、引用和省略表达后可单独理解的明确描述。"
        )
    )

    # 辅助回忆的多角度语义查询，不使用零碎关键词。
    retrieval_queries: list[str] = Field(
        description=(
            "用于辅助历史回忆的多角度语义查询列表。每一项都必须是完整、"
            "可独立理解、适合语义检索的短句或短描述，不要只给零碎关键词；"
            "可以包含相关人物、地点、时间、主题和任务目标。"
        )
    )


class RecollectionTool(BuildinTool[RecollectionQuery]):
    """表示 os 层内置历史回忆工具。

    说明:
        该工具作为系统级工具由 `Agent.build_tool_provider` 自动注入，
        不需要业务 Agent 显式注册；后续真实回忆能力也保持在工具内部演进。
    """

    # 内置工具对模型暴露的稳定名称。
    TOOL_NAME: ClassVar[str] = K_TOOL_NAME_RECOLLECTION

    def __init__(self) -> None:
        """初始化系统级历史回忆工具。"""

        descriptor = ToolDescriptor(
            summary="检索历史会话与中断任务信息。",
            capability_category=BuiltinToolCapabilityCategory.MEMORY.value,
            capability="回忆可能相关的历史会话、任务信息等相关信息，用于补充当前对话历史和 Memory Context 不足的事实。",
            use_cases=[
                "用户明确询问过去会话中讨论过、决定过或实现过的内容",
                "用户使用“之前”、“上次”、“我们当时”等表达，且当前对话历史与 Memory Context 不足以回答",
                "用户的提问有明显的回忆意图，如：“你想一想”，“回忆一下”，且涉及的内容不在当前上下文中",
            ],
            anti_use_cases=[
                "当前对话历史或 Memory Context 已经包含回答用户问题所需的信息时，禁止调用",
                "用户提出的是全新任务、当前事实判断、实时信息查询或外部数据查询时，禁止调用",
                "用户只是要求解释、总结、修改当前对话中已经出现的内容时，禁止调用",
                "用户问题没有明确指向过去会话或历史记忆时，禁止调用",
            ],
        )
        super().__init__(
            tool_metadata=ToolMetadata.model(
                name=self.TOOL_NAME,
                input_model=RecollectionQuery,
            ),
            tool_descriptor=descriptor,
            input_model=RecollectionQuery,
            call=self.call,
        )

    async def call(self, input_model: RecollectionQuery) -> ToolResult:
        """执行系统级历史回忆操作。

        参数:
            input_model: 已由 ToolService 校验并绑定上下文的回忆查询模型。

        返回:
            可回传给 LLM 的标准工具结果。

        说明:
            查询文本同时包含“任务”与“中断”或“暂停”时，会读取当前 Agent 下
            仍处于暂停状态的任务；其他查询不会访问运行期任务现场。
        """

        from turtlemap.os.context.prompt import build_markdown_section, join_prompt_sections

        sections: list[str] = []

        # 中断任务信息
        interruption_tasks_section = self._build_interruption_tasks_section(input_model)
        if interruption_tasks_section:
            sections.append(interruption_tasks_section)

        # 检索结果
        retrieval_result = f"没有找到与“{input_model.query}”相关的可用历史信息。"
        if retrieval_result:
            sections.append(
                build_markdown_section(
                    "检索结果",
                    retrieval_result,
                    heading_level=1,
                )
            )

        return ToolResult(
            content=join_prompt_sections(sections),
            fact_source_type=ToolFactSourceType.RECOLLECTION,
            purpose="retrieve relevant information from past conversations",
            raw_data={
                "query": input_model.query,
                "retrieval_queries": input_model.retrieval_queries,
            },
        )

    @staticmethod
    def _build_interruption_tasks_section(input_model: RecollectionQuery) -> str:
        """按回忆查询构建当前 Agent 的中断任务区块。

        参数:
            input_model: 已绑定工具运行期上下文的回忆查询参数。

        返回:
            查询明确指向暂停或中断任务时，返回包含当前未响应暂停任务的一级
            Markdown 区块；未命中查询意图或没有可展示任务时返回空字符串。
        """

        # 当前仅在检索意图明确指向暂停或中断任务时读取运行现场，避免普通回忆查询暴露任务状态。
        if "任务" not in input_model.query:
            return ""

        from turtlemap.os.context.prompt import (
            build_interruption_request_description,
            build_markdown_section,
            join_prompt_sections,
        )
        from turtlemap.os.tool.build_in.resume_task import K_TOOL_NAME_RESUME_TASK
        from turtlemap.os.tool.service import ToolService

        context = ensure_instance(
            ToolInputContext.get_input_context(input_model),
            ToolInputContext,
            "回忆工具输入上下文",
        )
        tool_service = ensure_instance(
            context.tool_service,
            ToolService,
            "回忆工具服务",
        )
        agent_state = tool_service.os_service.real_session_state.top_agent_state()
        interruption_task_descriptions: list[str] = []
        for task in agent_state.processing_tasks:
            request = task.interruption_request
            if task.state.status != TaskStatus.PAUSED or request is None:
                continue
            if request.response is not None:
                continue

            interruption_request_description = build_interruption_request_description(
                request=request,
                resume_tool_name=K_TOOL_NAME_RESUME_TASK,
            )
            interruption_task_descriptions.append(
                f"【任务 {len(interruption_task_descriptions) + 1}】：\n"
                f"{interruption_request_description}"
            )

        interruption_tasks = join_prompt_sections(interruption_task_descriptions)
        return build_markdown_section(
            "中断的任务",
            interruption_tasks,
            heading_level=1,
        )
