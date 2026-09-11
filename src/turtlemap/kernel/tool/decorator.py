#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 13:41
# @Author  : YaHaoo
# @File    : decorator.py

"""kernel 层工具装饰器与装配辅助定义。"""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast

from pydantic import BaseModel

from ..exceptions import KernelToolConfigurationError
from .models import (
    DECORATED_TOOL_DESCRIPTOR_ATTR,
    DECORATED_TOOL_INPUT_MODEL_ATTR,
    DECORATED_TOOL_METADATA_ATTR,
    ToolDescriptor,
    ToolMetadata,
    ToolResult,
)

InputModelT = TypeVar("InputModelT", bound=BaseModel)
ToolCallbackResult: TypeAlias = ToolResult | str | Awaitable[ToolResult | str]


def tool(
    *,
    descriptor: ToolDescriptor,
    input_model: type[InputModelT],
    tool_id: str | None = None,
    version: str | None = None,
    namespace: str | None = None,
    requires_confirmation: bool = False,
) -> Callable[
    [Callable[[InputModelT], ToolCallbackResult]],
    Callable[[InputModelT], ToolCallbackResult],
]:
    """把普通函数装配成可被 Runtime 注册的最小工具。

    参数:
        descriptor: 工具创建侧的结构化语义描述对象。
        input_model: 工具输入模型；无参工具应传入 `EmptyToolInputModel`。
        tool_id: 可选工具稳定 id；未提供时默认使用 `descriptor.name`。
        version: 可选工具版本。
        namespace: 可选工具命名空间。
        requires_confirmation: 是否必须在工具执行前获得用户确认。

    返回:
        一个装饰器。被装饰函数会保持接收 `BaseModel` 并返回 `ToolResult | str`
        或其可等待对象的调用形态，同时挂载 `__tool_metadata__` 供 `ToolService`
        装配。

    说明:
        当前装饰器只支持普通函数，不承载生命周期钩子或前置校验；
        一旦需要这些扩展能力，应直接使用 `ExecutableTool` 实例注册。
    """

    metadata = ToolMetadata.model(
        name=descriptor.name,
        input_model=input_model,
        tool_id=tool_id,
        version=version,
        namespace=namespace,
        requires_confirmation=requires_confirmation,
    )

    def decorator(
        func: Callable[[InputModelT], ToolCallbackResult],
    ) -> Callable[[InputModelT], ToolCallbackResult]:
        """执行普通函数到标准工具函数的装配。"""

        if not inspect.isfunction(func):
            raise KernelToolConfigurationError(
                "tool 装饰器注册失败：当前阶段只支持装饰普通函数"
            )

        @wraps(func)
        def wrapped(tool_input: InputModelT) -> ToolCallbackResult:
            """执行已装饰工具函数，保持原始模型入参与返回形态。"""

            return func(tool_input)

        wrapped_func = cast(Any, wrapped)
        setattr(wrapped_func, DECORATED_TOOL_METADATA_ATTR, metadata)
        setattr(wrapped_func, DECORATED_TOOL_DESCRIPTOR_ATTR, descriptor)
        setattr(wrapped_func, DECORATED_TOOL_INPUT_MODEL_ATTR, input_model)
        return wrapped

    return decorator
