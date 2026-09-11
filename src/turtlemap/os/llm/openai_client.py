#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 18:45
# @Author  : YaHaoo
# @File    : openai_client.py

"""turtlemap os 层 OpenAI 兼容 LLM 客户端实现。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Optional, cast

from openai import AsyncOpenAI, AsyncStream, OpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
)
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
from pydantic import BaseModel, ValidationError
from typing_extensions import override

from turtlemap.kernel.models import MessageRole
from turtlemap.shared.json_parser import sanitize_json_string
from turtlemap.shared.json_schema import pydantic_model_to_json_schema
from turtlemap.shared.logger import Logger

from .client import BaseLLMClient
from .model import (
    LLMCompletion,
    LLMCompletionChoice,
    LLMCompletionChunk,
    LLMCompletionChunkChoice,
    LLMCompletionChunkChoiceDelta,
    LLMMessage,
    LLMCompletionToolCall,
    LLMCompletionUsage,
    LLMDeltaToolCall,
    LLMDeltaToolCallFunction,
    LLMFunction,
    LLMMessage,
    LLMToolChoice,
    StructuredOutputT,
)


class OpenAIClient(BaseLLMClient):
    """表示基于 OpenAI 兼容接口的 LLM 客户端适配器。

    说明:
        该类只负责把 os 层 LLM 模型与 OpenAI SDK 数据结构互相转换，不直接实现
        kernel 协议。Runtime 产物到 LLM 消息的解释由 os 层服务负责。
    """

    @override
    def create_client(self) -> OpenAI:
        """创建同步 OpenAI 客户端实例。

        返回:
            当前配置对应的同步 OpenAI SDK 客户端。
        """

        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    @override
    def create_async_client(self) -> AsyncOpenAI:
        """创建异步 OpenAI 客户端实例。

        返回:
            当前配置对应的异步 OpenAI SDK 客户端。
        """

        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    @override
    def invoke(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
        tool_choice: LLMToolChoice | None = None,
        output_model_cls: type[StructuredOutputT] | None = None,
        **kwargs: object,
    ) -> LLMCompletion[StructuredOutputT]:
        """执行一次 OpenAI 兼容非流式调用。

        参数:
            messages: 当前轮传给模型的标准 LLM 输入消息列表。
            tools: 当前轮可供模型调用的工具 schema 列表。
            tool_choice: 当前轮工具选择策略。
            output_model_cls: 响应 content 期望解析成的结构化 Pydantic 模型类。
            **kwargs: 透传给 OpenAI Chat Completions 的额外参数。

        返回:
            归一化后的 LLM 非流式完成结果。
        """

        client = self._get_client()
        request_kwargs = self._build_request_kwargs(
            kwargs=kwargs,
            stream=False,
            tools=tools,
            tool_choice=tool_choice,
            output_model_cls=output_model_cls,
        )
        response: ChatCompletion = client.chat.completions.create(
            model=self.model,
            messages=self._normalize_messages(messages),
            **request_kwargs,
        )
        return self._normalize_completion(
            response=response,
            output_model_cls=output_model_cls,
        )

    @override
    async def ainvoke(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
        tool_choice: LLMToolChoice | None = None,
        output_model_cls: type[StructuredOutputT] | None = None,
        **kwargs: object,
    ) -> LLMCompletion[StructuredOutputT]:
        """执行一次 OpenAI 兼容异步非流式调用。

        参数:
            messages: 当前轮传给模型的标准 LLM 输入消息列表。
            tools: 当前轮可供模型调用的工具 schema 列表。
            tool_choice: 当前轮工具选择策略。
            output_model_cls: 响应 content 期望解析成的结构化 Pydantic 模型类。
            **kwargs: 透传给 OpenAI Chat Completions 的额外参数。

        返回:
            归一化后的 LLM 非流式完成结果。
        """

        client = self._get_async_client()
        request_kwargs = self._build_request_kwargs(
            kwargs=kwargs,
            stream=False,
            tools=tools,
            tool_choice=tool_choice,
            output_model_cls=output_model_cls,
        )
        response = await client.chat.completions.create(
            model=self.model,
            messages=self._normalize_messages(messages),
            **request_kwargs,
        )
        return self._normalize_completion(
            response=response,
            output_model_cls=output_model_cls,
        )

    @override
    async def astream_invoke(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
        tool_choice: LLMToolChoice | None = None,
        **kwargs: object,
    ) -> AsyncIterator[LLMCompletionChunk]:
        """执行一次 OpenAI 兼容异步流式调用。

        参数:
            messages: 当前轮传给模型的标准 LLM 输入消息列表。
            tools: 当前轮可供模型调用的工具 schema 列表。
            tool_choice: 当前轮工具选择策略。
            **kwargs: 透传给 OpenAI Chat Completions 的额外参数。

        返回:
            逐段产出归一化后的 LLM 流式 chunk。
        """

        client = self._get_async_client()
        request_kwargs = self._build_request_kwargs(
            kwargs=kwargs,
            stream=True,
            tools=tools,
            tool_choice=tool_choice,
            output_model_cls=None,
        )
        response: AsyncStream[ChatCompletionChunk] = await client.chat.completions.create(
            model=self.model,
            messages=self._normalize_messages(messages),
            **request_kwargs,
        )
        async for chunk in response:
            yield self._normalize_completion_chunk(chunk)

    def _build_request_kwargs(
        self,
        kwargs: dict[str, object],
        stream: bool,
        tools: list[dict[str, object]] | None,
        tool_choice: LLMToolChoice | None,
        output_model_cls: type[BaseModel] | None,
    ) -> dict[str, Any]:
        """构建 OpenAI Chat Completions 请求参数。

        参数:
            kwargs: 调用方传入的原始扩展参数。
            stream: 当前调用是否启用流式响应。
            tools: 当前轮可供模型调用的工具 schema 列表。
            tool_choice: 当前轮工具选择策略。
            output_model_cls: 响应 content 期望解析成的结构化 Pydantic 模型类。

        返回:
            可直接透传给 OpenAI SDK `create` 方法的参数字典。
        """

        if tools and output_model_cls is not None:
            raise ValueError("tools 与 output_model_cls 不能同时使用")
        if not tools and tool_choice not in (None, "none"):
            raise ValueError("tools 为空时只能不传 tool_choice，或显式传 tool_choice='none'")

        # 初始化参数只作为默认值，调用方传入的 kwargs 拥有更高优先级。
        request_kwargs: dict[str, Any] = {}
        if self.temperature is not None:
            request_kwargs["temperature"] = self.temperature
        if self.max_tokens is not None:
            request_kwargs["max_tokens"] = self.max_tokens
        request_kwargs.update(kwargs)

        # enable_thinking 位于 extra_body 内部，只在调用方未显式配置时补充默认值。
        if self.enable_thinking is not None:
            extra_body = request_kwargs.setdefault("extra_body", {})
            if isinstance(extra_body, dict) and "enable_thinking" not in extra_body:
                extra_body["enable_thinking"] = self.enable_thinking

        # stream 由具体调用方法决定，避免外部 kwargs 造成返回类型不一致。
        if request_kwargs.get("stream") is not None and request_kwargs.get("stream") != stream:
            Logger.logger.warning(
                f"LLM 调用收到 stream={request_kwargs.get('stream')}，已强制覆盖为 stream={stream}"
            )
        request_kwargs["stream"] = stream
        if tools is not None:
            request_kwargs["tools"] = tools
        if tool_choice is not None:
            request_kwargs["tool_choice"] = tool_choice
        if output_model_cls is not None:
            # 结构化输出 schema 同样挂在 extra_body 下，保留调用方已有扩展参数。
            extra_body = dict(request_kwargs.get("extra_body", {}))
            structured_outputs = dict(extra_body.get("structured_outputs", {}))
            structured_outputs["json"] = pydantic_model_to_json_schema(output_model_cls)
            extra_body["structured_outputs"] = structured_outputs
            request_kwargs["extra_body"] = extra_body
        return request_kwargs

    def _get_client(self) -> OpenAI:
        """获取当前复用的同步 OpenAI 客户端。

        返回:
            当前复用的同步 OpenAI SDK 客户端。
        """

        if self._client is None:
            self._client = self.create_client()
        return cast(OpenAI, self._client)

    def _get_async_client(self) -> AsyncOpenAI:
        """获取当前复用的异步 OpenAI 客户端。

        返回:
            当前复用的异步 OpenAI SDK 客户端。
        """

        if self._async_client is None:
            self._async_client = self.create_async_client()
        return cast(AsyncOpenAI, self._async_client)

    def _normalize_messages(
        self,
        messages: list[LLMMessage],
    ) -> list[ChatCompletionMessageParam]:
        """把 os 层 LLM 输入消息转换为 OpenAI SDK 消息结构。

        参数:
            messages: 当前待发送给模型的标准 LLM 输入消息列表。

        返回:
            供 OpenAI SDK 调用使用的消息列表。
        """

        normalized_messages: list[dict[str, object]] = []
        for message in messages:
            normalized_message: dict[str, object] = {
                "role": message.role.value,
                "content": message.content,
            }
            if message.tool_call_id is not None:
                normalized_message["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                normalized_message["tool_calls"] = [
                    tool_call.model_dump(mode="json", exclude_none=True)
                    for tool_call in message.tool_calls
                ]
            normalized_messages.append(normalized_message)
        return cast(list[ChatCompletionMessageParam], normalized_messages)

    def _normalize_completion(
        self,
        response: ChatCompletion,
        output_model_cls: type[StructuredOutputT] | None,
    ) -> LLMCompletion[StructuredOutputT]:
        """把 OpenAI 非流式响应转换为 os 层 LLM completion。

        参数:
            response: OpenAI SDK 返回的非流式响应对象。
            output_model_cls: 响应 content 期望解析成的结构化 Pydantic 模型类。

        返回:
            归一化后的 LLM 非流式完成结果。
        """

        return LLMCompletion(
            id=response.id,
            choices=[
                LLMCompletionChoice(
                    index=choice.index,
                    message=self._normalize_completion_message(choice.message),
                    structured_output=self._parse_structured_output(
                        content=choice.message.content,
                        output_model_cls=output_model_cls,
                    ),
                    finish_reason=choice.finish_reason,
                )
                for choice in response.choices
            ],
            created=response.created,
            model=response.model,
            usage=self._normalize_usage(response.usage),
        )

    def _normalize_completion_message(
        self,
        raw_message: ChatCompletionMessage,
    ) -> LLMMessage:
        """把 OpenAI 返回消息转换为 os 层 LLM 完成消息。

        参数:
            raw_message: OpenAI SDK 返回的 message 对象。

        返回:
            归一化后的 LLM 完成消息。
        """

        raw_role = raw_message.role
        role = MessageRole(raw_role)
        reasoning_content = getattr(raw_message, "reasoning_content", None)
        return LLMMessage(
            role=role,
            content=raw_message.content,
            reasoning_content=reasoning_content,
            tool_calls=self._normalize_tool_calls(cast(list[ChatCompletionMessageFunctionToolCall], raw_message.tool_calls)),
        )

    def _parse_structured_output(
        self,
        content: str | None,
        output_model_cls: type[StructuredOutputT] | None,
    ) -> StructuredOutputT | None:
        """解析并校验模型返回的结构化 content。

        参数:
            content: 模型返回的原始文本内容。
            output_model_cls: 目标结构化 Pydantic 模型类；为空时不解析。

        返回:
            解析后的结构化对象；未启用结构化输出时返回 None。
        """

        if output_model_cls is None:
            return None
        if not content or not content.strip():
            raise RuntimeError(
                f"结构化输出解析失败：模型未返回内容，model={self.model}"
            )

        try:
            sanitized_content = sanitize_json_string(content)
            return output_model_cls.model_validate_json(sanitized_content)
        except ValidationError as exc:
            raise RuntimeError(
                "结构化输出校验失败："
                f"output_model_cls={output_model_cls.__name__}, "
                f"content={content}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                "结构化输出解析失败："
                f"output_model_cls={output_model_cls.__name__}, "
                f"content={content}"
            ) from exc

    def _normalize_completion_chunk(
        self,
        raw_chunk: ChatCompletionChunk,
    ) -> LLMCompletionChunk:
        """把 OpenAI 流式 chunk 转换为 os 层 LLM chunk。

        参数:
            raw_chunk: OpenAI SDK 返回的原始流式 chunk。

        返回:
            归一化后的 LLM 流式响应 chunk。
        """

        return LLMCompletionChunk(
            id=raw_chunk.id,
            choices=[
                LLMCompletionChunkChoice(
                    index=choice.index,
                    delta=LLMCompletionChunkChoiceDelta(
                        role=(
                            MessageRole(choice.delta.role)
                            if choice.delta.role is not None
                            else None
                        ),
                        content=choice.delta.content,
                        reasoning_content=getattr(choice.delta, "reasoning_content", None),
                        tool_calls=self._normalize_delta_tool_calls(choice.delta.tool_calls),
                    ),
                    finish_reason=choice.finish_reason,
                )
                for choice in raw_chunk.choices
            ],
            created=raw_chunk.created,
            model=raw_chunk.model,
            usage=self._normalize_usage(raw_chunk.usage),
        )

    def _normalize_usage(self, raw_usage: Any) -> LLMCompletionUsage | None:
        """把 OpenAI usage 转换为 os 层 LLM 用量模型。

        参数:
            raw_usage: OpenAI SDK 返回的 usage 对象。

        返回:
            归一化后的 token 用量；原始字段为空时返回 None。
        """

        if raw_usage is None:
            return None
        
        return LLMCompletionUsage(
            completion_tokens=raw_usage.completion_tokens,
            prompt_tokens=raw_usage.prompt_tokens,
            total_tokens=raw_usage.total_tokens,
        )

    def _normalize_delta_tool_calls(
        self,
        raw_tool_calls: Optional[list[ChoiceDeltaToolCall]],
    ) -> list[LLMDeltaToolCall] | None:
        """把 OpenAI 流式 tool calls 转换为 os 层 LLM 增量工具调用结构。

        参数:
            raw_tool_calls: OpenAI delta 中的原始 tool calls 字段。

        返回:
            归一化后的增量工具调用列表；原始字段为空时返回 None。
        """

        if raw_tool_calls is None:
            return None

        return [
            LLMDeltaToolCall(
                index=raw_tool_call.index,
                id=raw_tool_call.id,
                function=(
                    LLMDeltaToolCallFunction(
                        arguments=raw_tool_call.function.arguments,
                        name=raw_tool_call.function.name,
                    )
                    if raw_tool_call.function is not None
                    else None
                ),
                type=raw_tool_call.type,
            )
            for raw_tool_call in raw_tool_calls
        ]

    def _normalize_tool_calls(
        self,
        raw_tool_calls: list[ChatCompletionMessageFunctionToolCall] | None,
    ) -> list[LLMCompletionToolCall]:
        """把 OpenAI 返回的 tool calls 转换为 os 层 LLM 工具调用结构。

        参数:
            raw_tool_calls: OpenAI 响应中的原始 tool calls 字段。

        返回:
            归一化后的工具调用列表；原始字段为空时返回 None。
        """

        if raw_tool_calls is None:
            return []

        return [
            LLMCompletionToolCall(
                id=raw_tool_call.id,
                function=LLMFunction(
                    arguments=raw_tool_call.function.arguments,
                    name=raw_tool_call.function.name,
                ),
                type=raw_tool_call.type,
            )
            for raw_tool_call in raw_tool_calls
        ]
