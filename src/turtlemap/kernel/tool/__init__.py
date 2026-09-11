#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 13:41
# @Author  : YaHaoo
# @File    : __init__.py

"""kernel 层工具定义与装配入口。"""

from .decorator import ToolCallbackResult, tool
from .models import (
    ExecutionUnit,
    ExecutionUnitResult,
    ExecutionResult,
    EmptyToolInputModel,
    JsonDict,
    RESERVED_UNIT_TYPE_LLM_CALL,
    RESERVED_UNIT_TYPE_TOOL_CALL,
    ToolCall,
    ToolCallFunction,
    ToolDescriptor,
    ToolExecutionContext,
    ToolFactSourceType,
    ToolMetadata,
    ToolResult,
    ValidateExecuteResult,
)
from .service import BaseToolService, ExecutableTool

__all__ = [
    "BaseToolService",
    "ExecutionUnit",
    "ExecutionUnitResult",
    "ExecutionResult",
    "EmptyToolInputModel",
    "ExecutableTool",
    "JsonDict",
    "RESERVED_UNIT_TYPE_LLM_CALL",
    "RESERVED_UNIT_TYPE_TOOL_CALL",
    "ToolCallbackResult",
    "ToolCall",
    "ToolCallFunction",
    "ToolDescriptor",
    "ToolExecutionContext",
    "ToolFactSourceType",
    "ToolMetadata",
    "ToolResult",
    "ValidateExecuteResult",
    "tool",
]
