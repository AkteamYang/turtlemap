#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2026 - 2026 YaHaoo, Inc. All Rights Reserved 
#
# @Time    : 2026/04/24 08:53
# @Author  : YaHaoo
# @File    : client.py
import time
import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional

from .model import (
    LLMCompletion,
    LLMCompletionChunk,
    LLMMessage,
    LLMToolChoice,
    StructuredOutputT,
)


class BaseLLMClient(ABC):
    """定义不同 LLM 服务客户端的统一调用接口。

    该基类负责保存模型调用所需的通用配置，并约束同步调用、
    流式调用、异步流式调用和工具调用的统一方法签名。

    Attributes:
        api_key: LLM 服务访问密钥。
        base_url: LLM 服务基础地址；为空时由具体客户端使用默认地址。
        timeout: 请求超时时间。
        model: 当前调用的模型名称。
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str],
        model: str,
        timeout: float = 60,
        temperature: float | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool | None = None,
    ):
        """初始化 LLM 客户端基础配置。

        Args:
            api_key: LLM 服务访问密钥。
            base_url: LLM 服务基础地址。
            model: 当前调用的模型名称。
            timeout: 请求超时时间。
            temperature: 默认采样温度；为空时不传递该参数。
            max_tokens: 默认最大输出 token 数；为空时不传递该参数。
            enable_thinking: 默认是否启用 thinking；为空时不传递该参数。
        """
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self._client = None
        self._async_client = None

    @abstractmethod
    def create_client(self) -> Any:
        """创建同步客户端实例。"""
        pass

    def create_async_client(self) -> Any:
        """创建异步客户端实例。

        Returns:
            异步客户端实例；若子类未实现则返回 ``None``。
        """
        return None

    @abstractmethod
    def invoke(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
        tool_choice: LLMToolChoice | None = None,
        output_model_cls: type[StructuredOutputT] | None = None,
        **kwargs: object,
    ) -> LLMCompletion[StructuredOutputT]:
        """执行一次非流式调用。

        参数:
            messages: 当前轮传给模型的标准 LLM 输入消息列表。
            tools: 当前轮可供模型调用的工具 schema 列表。
            tool_choice: 当前轮工具选择策略。
            output_model_cls: 响应 content 期望解析成的结构化 Pydantic 模型类。
            **kwargs: 透传给具体 LLM 服务的额外参数。

        返回:
            标准 LLM 非流式响应对象。
        """
        

    async def ainvoke(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
        tool_choice: LLMToolChoice | None = None,
        output_model_cls: type[StructuredOutputT] | None = None,
        **kwargs: object,
    ) -> LLMCompletion[StructuredOutputT]:
        """执行一次异步非流式调用。

        参数:
            messages: 当前轮传给模型的标准 LLM 输入消息列表。
            tools: 当前轮可供模型调用的工具 schema 列表。
            tool_choice: 当前轮工具选择策略。
            output_model_cls: 响应 content 期望解析成的结构化 Pydantic 模型类。
            **kwargs: 透传给同步 ``invoke`` 的扩展参数。

        返回:
            标准 LLM 响应对象。

        说明:
            默认实现使用线程池包装同步 ``invoke``，子类可覆写为原生异步实现。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.invoke(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                output_model_cls=output_model_cls,
                **kwargs,
            )
        )

    @abstractmethod
    def astream_invoke(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
        tool_choice: LLMToolChoice | None = None,
        **kwargs: object,
    ) -> AsyncIterator[LLMCompletionChunk]:
        """异步流式调用（子类可选实现真正的异步）

        参数:
            messages: 当前轮传给模型的标准 LLM 输入消息列表。
            tools: 当前轮可供模型调用的工具 schema 列表。
            tool_choice: 当前轮工具选择策略。
            **kwargs: 透传给具体 LLM 服务的额外参数。

        返回:
            逐段产出标准 LLM 流式响应 chunk 的异步迭代器。
        """
        ...


def create_adapter(
    api_key: str,
    base_url: Optional[str],
    timeout: float,
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    enable_thinking: bool | None = None,
) -> BaseLLMClient:
    """根据 ``base_url`` 自动选择适配器。

    当前检测逻辑：
    - ``googleapis.com`` 或 ``generativelanguage`` -> ``GeminiClient``
    - 其他 -> ``OpenAIClient``（默认）

    Args:
        api_key: LLM 服务访问密钥。
        base_url: LLM 服务基础地址。
        timeout: 请求超时时间。
        model: 当前调用的模型名称。
        temperature: 默认采样温度；为空时不传递该参数。
        max_tokens: 默认最大输出 token 数；为空时不传递该参数。
        enable_thinking: 默认是否启用 thinking；为空时不传递该参数。

    Returns:
        适配当前服务地址的客户端实例。
    """
    if base_url:
        base_url_lower = base_url.lower()

        if "googleapis.com" in base_url_lower or "generativelanguage" in base_url_lower:
            from .gemini_client import GeminiClient
            return GeminiClient(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout=timeout,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
            )

    # 默认使用OpenAI适配器（兼容所有OpenAI格式接口）
    from .openai_client import OpenAIClient
    return OpenAIClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
    )
