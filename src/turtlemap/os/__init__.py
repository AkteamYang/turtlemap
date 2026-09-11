#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/06/17 13:49
# @Author  : YaHaoo
# @File    : __init__.py

"""turtlemap 外围 OS 实现层。"""

from turtlemap.kernel import ToolResult, ToolResultStatus

from .agent import Agent
from .context import (
    ContextBuildProvider,
    ContextCompressionProvider,
    ContextTokenBudget,
    TokenCache,
    Tokenizer,
)
from .executor import NoopExecutor
from .exceptions import (
    ContextCompressionError,
    NoCompressibleHistoryError,
)
from .llm import ModelClientProvider, OpenAIClient
from .context.models import (
    ContextBuildInput,
    ContextBuildResult,
    ContextCompressionResult,
    ContextCompressionTaskRecord,
    ContextCompressionTaskStatus,
)
from .models import (
    AgentState,
    SessionState,
)
from .protocols import (
    ContextBuildProtocol,
    ContextCompressionProtocol,
    ExecutorProtocol,
    ModelClientProtocol,
    StateStoreProtocol,
)
from .runtime import Runtime
from .service import OSService
from .store import InMemoryStateStore
from .tool import (
    LLMCallExecutionUnit,
    LLMCallExecutionResult,
    ToolService,
    ToolCallExecutionResult,
    ToolCallExecutionUnit,
)

__all__ = [
    "Agent",
    "AgentState",
    "ContextBuildProtocol",
    "ContextBuildInput",
    "ContextBuildProvider",
    "ContextBuildResult",
    "ContextCompressionError",
    "ContextCompressionProtocol",
    "ContextCompressionProvider",
    "ContextCompressionResult",
    "ContextCompressionTaskRecord",
    "ContextCompressionTaskStatus",
    "ContextTokenBudget",
    "ExecutorProtocol",
    "InMemoryStateStore",
    "LLMCallExecutionUnit",
    "LLMCallExecutionResult",
    "NoCompressibleHistoryError",
    "NoopExecutor",
    "ModelClientProtocol",
    "ModelClientProvider",
    "OpenAIClient",
    "OSService",
    "Runtime",
    "SessionState",
    "StateStoreProtocol",
    "TokenCache",
    "Tokenizer",
    "ToolService",
    "ToolCallExecutionResult",
    "ToolCallExecutionUnit",
    "ToolResult",
    "ToolResultStatus",
]
