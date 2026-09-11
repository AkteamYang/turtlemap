#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 18:45
# @Author  : YaHaoo
# @File    : provider.py

"""turtlemap os 层模型调用 provider 实现。"""

from __future__ import annotations

from typing import AsyncIterable

from turtlemap.config import LLMConfig
from turtlemap.os.exceptions import OSErrorBase, OSRuntimeError
from turtlemap.os.protocols import ModelClientProtocol

from .client import BaseLLMClient, create_adapter
from .model import (
    LLMCompletion,
    LLMCompletionChunk,
    LLMMessage,
    LLMToolChoice,
)


class ModelClientProvider(ModelClientProtocol):
    """表示 os 层默认模型调用 provider。

    说明:
        provider 负责持有具体 LLM 客户端适配器，并在 kernel 消息结构与 os 层
        LLM 协议结构之间转换。具体服务商协议由 `BaseLLMClient` 实现承担。
    """

    __slots__ = ("config", "llm_client")

    def __init__(self, config: LLMConfig) -> None:
        """初始化 ModelClientProvider。

        参数:
            config: 当前 provider 使用的 LLM 配置对象。
        """

        # 当前 provider 使用的 LLM 配置对象。
        self.config = config

        # 当前 provider 内部持有的具体 LLM 客户端适配器。
        self.llm_client: BaseLLMClient = create_adapter(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            model=config.model,
            enable_thinking=False
        )

    async def generate_message(
        self,
        messages: list[LLMMessage],
        tool_schemas: list[dict[str, object]] | None = None,
    ) -> LLMMessage:
        """执行一次普通或带工具的模型推理。

        参数:
            messages: 当前轮传给模型的标准消息字典列表。
            tool_schemas: 当前轮可供模型选择的工具描述列表；为空时表示纯文本推理。

        返回:
            模型返回的 LLM assistant 消息对象。
        """

        completion = await self.llm_client.ainvoke(
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto" if tool_schemas else None,
        )
        if not completion.choices:
            raise OSRuntimeError(f'LLM 未返回有效内容')
        
        return completion.choices[0].message
    
    def stream_generate_message(
        self,
        messages: list[LLMMessage],
        tool_schemas: list[dict[str, object]] | None = None,
        tool_choice: LLMToolChoice | None = None,
    ) -> AsyncIterable[LLMCompletionChunk]:
        """执行一次流式模型推理。

        参数:
            messages: 当前轮传给模型的标准 LLM 输入消息列表。
            tool_schemas: 当前轮可供模型选择的工具描述列表；为空时表示纯文本推理。
            tool_choice: 当前轮工具选择策略；为空时在有工具时默认使用 `auto`。

        返回:
            逐段产出标准 LLM 流式响应 chunk 的异步可迭代对象。
        """

        return self.llm_client.astream_invoke(
            messages=messages, 
            tools=tool_schemas, 
            tool_choice=tool_choice if tool_choice else ("auto" if tool_schemas else None)
        )
