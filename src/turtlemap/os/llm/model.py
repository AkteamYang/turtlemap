#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/16 09:00
# @Author  : YaHaoo
# @File    : model.py

"""turtlemap os 层 LLM 流式响应模型。"""

from __future__ import annotations

import json
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, TypeAdapter

from turtlemap.kernel.models.enums import MessageRole, RuntimeArtifactType
from turtlemap.kernel.models.models import RuntimeArtifact

StructuredOutputT = TypeVar("StructuredOutputT", bound=BaseModel)
LLMToolChoice = Literal["none", "auto", "required"] | dict[str, object]

@RuntimeArtifact.register_field_type("payload", RuntimeArtifactType.ASSISTANT_MESSAGE)
@RuntimeArtifact.register_field_type("payload", RuntimeArtifactType.TOOL_CALL)
class LLMMessage(BaseModel):
    """表示发送给 LLM 的最小输入消息。

    说明:
        该模型用于 os 层适配具体 LLM 服务时承载请求消息。纯文本对话使用
        `role` 与 `content`；工具结果消息通过 `tool_call_id` 关联前置调用；
        assistant 工具调用消息通过 `tool_calls` 传回模型上下文。
    """

    # 消息持久化后的自增唯一标识；未落库前允许为空。
    message_id: int | None = None

    # 当前输入消息的角色。
    role: MessageRole

    # 当前输入消息的文本内容。
    content: str | None = None

    # 当前推理完整内容，用于兼容 reasoning 模型输出。
    reasoning_content: str | None = None

    # 工具结果消息对应的工具调用标识；普通文本消息为空。
    tool_call_id: str | None = None

    # assistant 消息中携带的工具调用列表；非工具调用消息为空列表。
    tool_calls: list[LLMCompletionToolCall] = Field(default_factory=list)

    def to_token_budget_text(self) -> str:
        """生成用于估算上下文 token 预算的代表性字符串。

        返回:
            用于 token 预算统计的字符串。

        说明:
            该字符串不是 LLM 请求序列化格式，只用于更接近 chat template 的
            预算估算：普通消息只统计会进入 LLM 上下文的内容文本；工具调用和
            工具结果额外统计必要协议结构，固定消息开销由 Tokenizer 单独通过
            常量补足。`reasoning_content` 不作为历史回填给 LLM，因此不进入预算。
        """

        if self.role == MessageRole.TOOL or self.tool_call_id:
            return self._build_tool_result_token_budget_text()

        parts: list[str] = []
        if self.content:
            parts.append(self.content)
        if self.tool_calls:
            parts.append(self._build_tool_calls_token_budget_text())
        return "\n".join(parts)

    def _build_tool_result_token_budget_text(self) -> str:
        """生成工具结果消息的 token 预算字符串。

        返回:
            包含工具调用 id 和工具结果文本的紧凑 JSON 字符串。
        """

        return json.dumps(
            {
                "tool_call_id": self.tool_call_id,
                "content": self.content or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _build_tool_calls_token_budget_text(self) -> str:
        """生成 assistant tool calls 的 token 预算字符串。

        返回:
            包含必要工具调用协议字段的稳定紧凑 JSON 字符串。
        """

        # 先经 Pydantic 转成 JSON-compatible 对象，再统一排序输出，保证预算文本稳定。
        tool_calls_data = _LLM_COMPLETION_TOOL_CALLS_ADAPTER.dump_python(
            self.tool_calls,
            mode="json",
            exclude_none=True,
        )
        return json.dumps(
            tool_calls_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class LLMDeltaToolCallFunction(BaseModel):
    """表示 LLM 流式工具调用中的函数片段。

    说明:
        该模型参考 OpenAI `ChoiceDeltaToolCallFunction`，用于承载流式返回中
        function call 的名称和参数片段。`arguments` 通常是增量 JSON 字符串。
    """

    # 工具调用参数片段，通常是尚未完整闭合的 JSON 字符串。
    arguments: str | None = None

    # 被调用的函数或工具名称。
    name: str | None = None


class LLMDeltaToolCall(BaseModel):
    """表示 LLM 流式响应中的工具调用片段。

    说明:
        该模型参考 OpenAI `ChoiceDeltaToolCall`，用于在流式过程中逐步承载
        tool call id、类型以及函数调用片段。
    """

    # 当前工具调用在本轮响应中的全局序号，用于跨 chunk 合并增量片段。
    index: int

    # 当前工具调用的唯一标识，部分流式片段中可能为空。
    id: str | None = None

    # 当前工具调用携带的函数调用片段。
    function: LLMDeltaToolCallFunction | None = None

    # 工具调用类型，当前仅支持 function。
    type: Literal["function"] | None = None


class LLMCompletionChunkChoiceDelta(BaseModel):
    """表示 LLM 流式响应中的单个增量片段。

    说明:
        该模型参考 OpenAI `ChoiceDelta`，并补充 `reasoning_content` 字段，
        用于兼容支持推理内容流式输出的模型。
    """

    # 当前片段所属角色，通常只在首个片段中返回。
    role: MessageRole | None = None

    # 当前普通文本内容片段。
    content: str | None = None

    # 当前推理内容片段，用于兼容 reasoning 模型输出。
    reasoning_content: str | None = None

    # 当前片段携带的工具调用增量列表。
    tool_calls: list[LLMDeltaToolCall] | None = None


class LLMCompletionChunkChoice(BaseModel):
    """表示 LLM 流式响应 chunk 中的单个候选回复。

    说明:
        该模型参考 OpenAI `Choice` 在流式响应中的结构，用于承载候选回复序号、
        增量内容和结束原因。`finish_reason` 通常只在最后一个片段中出现。
    """

    # 当前候选回复的全局序号，对应 OpenAI Choice.index。
    index: int

    # 当前候选回复在本 chunk 中携带的增量内容。
    delta: LLMCompletionChunkChoiceDelta

    # 当前候选回复的结束原因，通常只在最后一个增量片段中出现。
    finish_reason: str | None = None


class LLMCompletionChunk(BaseModel):
    """表示一次 LLM 流式响应 chunk。

    说明:
        该模型参考 OpenAI `ChatCompletionChunk`，但将 `choices[].delta`
        归一化为直接可消费的 `choices` 列表，避免上层依赖 OpenAI SDK
        的响应结构。
    """

    # 当前 completion 的唯一标识，同一轮流式响应中通常保持一致。
    id: str

    # 当前 chunk 中包含的候选回复列表。
    choices: list[LLMCompletionChunkChoice]

    # 当前 completion 创建时间，使用 Unix 秒级时间戳。
    created: int

    # 当前 chunk 对应的模型名称。
    model: str

    # 当前响应的 token 用量；通常只有最终 chunk 才会携带。
    usage: LLMCompletionUsage | None = None


class LLMFunction(BaseModel):
    """表示 LLM 完整工具调用中的函数信息。

    说明:
        该模型参考 OpenAI tool call 中的 function 结构，用于承载模型
        生成的函数名称和完整参数字符串。
    """

    # 工具调用完整参数字符串，通常是 JSON 字符串。
    arguments: str | None = None

    # 被调用的函数或工具名称。
    name: str | None = None


class LLMCompletionToolCall(BaseModel):
    """表示 LLM 完整响应中的工具调用信息。

    说明:
        该模型参考 OpenAI `ChatCompletionMessageToolCall`，用于承载
        非流式完成结果中模型生成的完整工具调用。
    """

    # 当前工具调用的唯一标识。
    id: str | None = None

    # 当前工具调用携带的完整函数调用信息。
    function: LLMFunction | None = None

    # 工具调用类型，当前仅支持 function。
    type: Literal["function"] | None = None


_LLM_COMPLETION_TOOL_CALLS_ADAPTER = TypeAdapter(list[LLMCompletionToolCall])


class LLMCompletionChoice(BaseModel, Generic[StructuredOutputT]):
    """表示 LLM 非流式完成结果中的单个候选回复。

    说明:
        该模型参考 OpenAI `Choice` 在非流式响应中的结构，用于承载候选回复序号、
        稳定消息和结束原因。
    """

    # 当前候选回复的全局序号，对应 OpenAI Choice.index。
    index: int

    # 当前候选回复携带的稳定消息内容。
    message: LLMMessage

    # 当前消息 content 按 output_model_cls 解析后的结构化结果。
    structured_output: StructuredOutputT | None = None

    # 当前候选回复的结束原因。
    finish_reason: str | None = None


class LLMCompletionUsage(BaseModel):
    """表示一次 LLM completion 的 token 用量。

    说明:
        该模型参考 OpenAI `CompletionUsage`，当前只保留业务常用的顶层 token
        用量字段；provider 特有的细分 details 后续有明确需求时再扩展。
    """

    # 模型输出消耗的 token 数。
    completion_tokens: int

    # 模型输入提示消耗的 token 数。
    prompt_tokens: int

    # 本次请求总 token 数。
    total_tokens: int


class LLMCompletion(BaseModel, Generic[StructuredOutputT]):
    """表示一次 LLM 非流式完成结果。

    说明:
        该模型参考 OpenAI `ChatCompletion`，但将 `choices[].message`
        归一化为直接可消费的 `choices` 列表，避免上层依赖 OpenAI SDK
        的响应结构。
    """

    # 当前 completion 的唯一标识。
    id: str

    # 当前 completion 中包含的稳定候选回复列表。
    choices: list[LLMCompletionChoice[StructuredOutputT]]

    # 当前 completion 创建时间，使用 Unix 秒级时间戳。
    created: int

    # 当前 completion 对应的模型名称。
    model: str

    # 当前响应的 token 用量。
    usage: LLMCompletionUsage | None = None
