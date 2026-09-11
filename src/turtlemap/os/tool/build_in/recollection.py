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

from typing import ClassVar

from pydantic import BaseModel, Field

from turtlemap.kernel.tool import ToolDescriptor, ToolMetadata, ToolResult
from turtlemap.kernel.tool.models import ToolFactSourceType

from .base import BuildinTool

K_TOOL_NAME_RECOLLECTION = "recollection"


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
            name=self.TOOL_NAME,
            capability="回忆可能相关的历史会话内容，用于补充当前对话历史和 Memory Context 不足的事实。",
            use_cases=[
                "用户明确询问过去会话中讨论过、决定过或实现过的内容",
                "用户使用“之前”“上次”“我们当时”等表达，且当前对话历史与 Memory Context 不足以回答",
                "用户的提问有明显的回忆意图，如：“你想一想”，“回忆一下”，且涉及的内容不在当前上下文中",
            ],
            anti_use_cases=[
                "当前对话历史或 Memory Context 已经包含回答用户问题所需的信息时，禁止调用",
                "用户提出的是全新任务、当前事实判断、实时信息查询或外部数据查询时，禁止调用",
                "用户只是要求解释、总结、修改当前对话中已经出现的内容时，禁止调用",
                "用户问题没有明确指向过去会话或历史记忆时，禁止调用",
            ],
            tags=["回忆", "recollection"],
        )
        super().__init__(
            tool_metadata=ToolMetadata.model(
                name=descriptor.name,
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
        """

        return ToolResult(
            content=f"没有找到与“{input_model.query}”相关的可用历史信息。",
            fact_source_type=ToolFactSourceType.RECOLLECTION,
            purpose="retrieve relevant information from past conversations",
            raw_data={
                "query": input_model.query,
                "retrieval_queries": input_model.retrieval_queries,
                "mock": True,
                "matched": False,
            },
        )
