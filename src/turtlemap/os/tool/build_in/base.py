#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/28 13:39
# @Author  : YaHaoo
# @File    : base.py

"""os 层内置工具基类定义。"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from turtlemap.kernel.tool import ExecutableTool

InputModelT = TypeVar("InputModelT", bound=BaseModel)


class BuildinTool(ExecutableTool[InputModelT]):
    """表示 os 层内置工具基类。

    说明:
        内置工具由框架在 `Agent.build_tool_provider` 阶段自动注入，
        与业务侧自定义工具区分开；当前类只承载身份语义，后续可继续扩展
        内置工具专属权限、观测或过滤策略。
    """

    ...
