#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 18:32
# @Author  : YaHaoo
# @File    : __init__.py

"""turtlemap os 层上下文治理实现。"""

from turtlemap.config import ContextTokenBudget

from .models import (
    ContextBuildInput,
    ContextBuildResult,
    ContextCompressionLevel,
    ContextCompressionResult,
    ContextCompressionTaskRecord,
    ContextCompressionTaskStatus,
    ContextProjection,
    FrameworkInstruction,
    K_AGENT_DEFINITION,
    K_FRAMEWORK_INSTRUCTION,
    SystemInstruction,
)
from .tokenizer import TokenCache, Tokenizer


def __getattr__(name: str) -> object:
    """按需加载上下文 provider，避免 context.models 导入时触发运行链循环依赖。

    参数:
        name: 当前外部访问的导出名称。

    返回:
        与导出名称对应的 provider 类对象。
    """

    if name == "ContextBuildProvider":
        from .provider import ContextBuildProvider

        return ContextBuildProvider
    if name == "ContextCompressionProvider":
        from .compress import ContextCompressionProvider

        return ContextCompressionProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ContextBuildInput",
    "ContextBuildProvider",
    "ContextBuildResult",
    "ContextCompressionLevel",
    "ContextCompressionProvider",
    "ContextCompressionResult",
    "ContextCompressionTaskRecord",
    "ContextCompressionTaskStatus",
    "ContextProjection",
    "ContextTokenBudget",
    "FrameworkInstruction",
    "K_AGENT_DEFINITION",
    "K_FRAMEWORK_INSTRUCTION",
    "SystemInstruction",
    "TokenCache",
    "Tokenizer",
]
