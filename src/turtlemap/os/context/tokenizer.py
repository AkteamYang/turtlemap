#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 18:32
# @Author  : YaHaoo
# @File    : tokenizer.py

"""turtlemap os 层 token 统计实现。"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field

import tiktoken

from turtlemap.config import ContextTokenBudget
from turtlemap.kernel.models.models import RuntimeArtifact
from turtlemap.os.llm.model import LLMMessage
from turtlemap.shared.ids import generate_content_hash
from turtlemap.shared.json_parser import dump_to_static_json

encoding_name2encoding: dict[str, tiktoken.Encoding] = {}
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_CALL_OVERHEAD_TOKENS = 4
TOOL_RESULT_OVERHEAD_TOKENS = 4


class TokenCache:
    """表示基于 `OrderedDict` 的最小 LRU token 缓存。

    说明:
        当前缓存只按“最大缓存条目数”做容量控制，key 由
        `tokenizer_identity + content_hash` 组成，value 为 token 数。
        后续若需要更细粒度的内存治理，再扩展为按字节数淘汰。
    """

    __slots__ = ("max_entries", "_cache")

    def __init__(self, max_entries: int = 1024) -> None:
        """初始化 TokenCache。

        参数:
            max_entries: 缓存最多允许保留的条目数。
        """

        if max_entries <= 0:
            raise ValueError("max_entries 必须为正整数")

        # 当前缓存允许保留的最大条目数。
        self.max_entries = max_entries

        # 当前 LRU 缓存主体，尾部表示最近访问。
        self._cache: OrderedDict[tuple[str, str], int] = OrderedDict()

    def get(self, cache_key: tuple[str, str]) -> int | None:
        """读取缓存中的 token 结果。

        参数:
            cache_key: 当前待查询的缓存键。

        返回:
            命中的 token 数；若未命中则返回 `None`。
        """

        token_count = self._cache.get(cache_key)
        if token_count is None:
            return None

        # 命中后移动到尾部，保持 LRU 顺序。
        self._cache.move_to_end(cache_key)
        return token_count

    def set(self, cache_key: tuple[str, str], token_count: int) -> None:
        """写入一条 token 统计缓存。

        参数:
            cache_key: 当前待写入的缓存键。
            token_count: 当前内容对应的 token 数。
        """

        self._cache[cache_key] = token_count
        self._cache.move_to_end(cache_key)

        # 超出容量时淘汰最久未使用项。
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)


@dataclass(slots=True)
class Tokenizer:
    """表示 os 层默认 token 统计器。

    说明:
        当前实现基于 `tiktoken` 做近似真实模型规则的 token 统计，
        并通过 `TokenCache` 缓存重复内容的统计结果，降低上下文治理中
        的重复 CPU 开销。
    """

    # 当前 tokenizer 的稳定标识，用于区分不同编码规则。
    tokenizer_identity: str

    # 当前 tokenizer 内部使用的编码名称。
    encoding_name: str

    # 当前 tokenizer 持有的 token 缓存。
    cache: TokenCache = field(default_factory=TokenCache)

    def __init__(
        self,
        model_name: str = "gpt-4o-mini",
        cache: TokenCache | None = None,
    ) -> None:
        """初始化 Tokenizer。

        参数:
            model_name: 当前默认参考的模型名称。
            cache: 可选外部注入的 token 缓存对象。
        """

        try:
            encoding = tiktoken.encoding_for_model(model_name)
            encoding_name = encoding.name
        except KeyError:
            encoding_name = "cl100k_base"

        # 当前 tokenizer 的稳定标识。
        self.tokenizer_identity = model_name

        # 当前 tokenizer 内部实际采用的编码名称。
        self.encoding_name = encoding_name

        # 当前 tokenizer 持有的 token 缓存。
        self.cache = cache or TokenCache()

    def count_text(self, content: str) -> int:
        """统计一段文本的 token 数。

        参数:
            content: 当前待统计的文本内容。

        返回:
            该文本在当前 tokenizer 规则下的 token 数。
        """

        return self._count_serialized_content(content)

    def count_message(self, message: LLMMessage) -> int:
        """统计单条 LLM 消息的 token 数。

        参数:
            message: 当前待统计的 LLM 消息。

        返回:
            该消息序列化后的 token 数。
        """

        token_count = MESSAGE_OVERHEAD_TOKENS
        token_count += self._count_serialized_content(message.role.value)
        token_count += self._count_serialized_content(message.to_token_budget_text())

        # 工具调用和工具结果会在 chat template 中引入额外协议结构，用固定开销补足。
        if message.tool_calls:
            token_count += TOOL_CALL_OVERHEAD_TOKENS
        if message.tool_call_id:
            token_count += TOOL_RESULT_OVERHEAD_TOKENS
        return token_count

    def count_tool_schema(self, tool_schema: dict[str, object]) -> int:
        """统计单个工具 schema 的 token 数。

        参数:
            tool_schema: 当前待统计的工具 schema。

        返回:
            该工具 schema 序列化后的 token 数。
        """

        serialized_tool_schema = dump_to_static_json(tool_schema)
        return self._count_serialized_content(serialized_tool_schema)

    def count_context(
        self,
        messages: list[LLMMessage],
        tool_schemas: list[dict[str, object]],
    ) -> int:
        """统计一轮上下文总 token 数。

        参数:
            messages: 当前轮标准消息列表。
            tool_schemas: 当前轮暴露给模型的工具 schema 列表。

        返回:
            当前上下文材料的总 token 数。
        """

        total_tokens = 0
        for message in messages:
            total_tokens += self.count_message(message)

        for tool_schema in tool_schemas:
            total_tokens += self.count_tool_schema(tool_schema)

        return total_tokens

    @staticmethod
    def count_history_rounds(history: list[RuntimeArtifact]) -> int:
        """统计 history 中已经稳定落库的对话轮次数。

        参数:
            history: 当前 Agent 已稳定落库的 Runtime 产物列表。

        返回:
            以 user 消息作为起点统计得到的历史轮次数。
        """

        round_count = 0
        for artifact in history:
            if artifact.is_user_role_message():
                round_count += 1
        return round_count

    def _count_serialized_content(self, content: str) -> int:
        """统计一段已序列化内容的 token 数并复用缓存。

        参数:
            content: 当前待统计的已序列化字符串内容。

        返回:
            该内容的 token 数。
        """

        content_hash = generate_content_hash(content)
        cache_key = (self.tokenizer_identity, content_hash)
        cached_token_count = self.cache.get(cache_key)
        if cached_token_count is not None:
            return cached_token_count

        encoding = get_encoding(self.encoding_name)
        token_count = len(encoding.encode(content))
        self.cache.set(cache_key, token_count)
        return token_count


def get_encoding(encoding_name: str) -> tiktoken.Encoding:
    """按编码名称读取并缓存 tiktoken 编码对象。

    参数:
        encoding_name: tiktoken 编码名称。

    返回:
        可用于 encode 的 tiktoken 编码对象。
    """

    encoding = encoding_name2encoding.get(encoding_name)
    if encoding is None:
        # tiktoken 编码对象加载成本较高，按名称全局缓存后复用。
        encoding = tiktoken.get_encoding(encoding_name)
        encoding_name2encoding[encoding_name] = encoding
    return encoding
