#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 18:45
# @Author  : YaHaoo
# @File    : __init__.py

"""turtlemap os 层 LLM 客户端实现。"""

from turtlemap.os.protocols import ModelClientProtocol

from .model import (
    LLMCompletion,
    LLMCompletionChoice,
    LLMCompletionChunk,
    LLMCompletionChunkChoice,
    LLMCompletionChunkChoiceDelta,
    LLMCompletionToolCall,
    LLMCompletionUsage,
    LLMDeltaToolCall,
    LLMDeltaToolCallFunction,
    LLMFunction,
    LLMMessage,
    LLMToolChoice,
)
from .openai_client import OpenAIClient
from .provider import ModelClientProvider

__all__ = [
    "LLMCompletion",
    "LLMCompletionChoice",
    "LLMCompletionChunk",
    "LLMCompletionChunkChoice",
    "LLMCompletionChunkChoiceDelta",
    "LLMCompletionToolCall",
    "LLMCompletionUsage",
    "LLMDeltaToolCall",
    "LLMDeltaToolCallFunction",
    "LLMFunction",
    "LLMMessage",
    "LLMToolChoice",
    "ModelClientProtocol",
    "ModelClientProvider",
    "OpenAIClient",
]
