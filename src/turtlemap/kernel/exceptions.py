#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/08 10:26
# @Author  : YaHaoo
# @File    : exceptions.py

"""kernel 层异常定义。"""

from __future__ import annotations

from turtlemap.shared.exceptions import ErrorBase


class KernelError(ErrorBase):
    """表示 kernel 层统一异常基类。

    说明:
        `kernel` 内部抛出的结构化异常应尽量继承该基类，
        便于上层 `os` 统一识别、兜底和转换。
    """


class KernelRuntimeError(KernelError):
    """表示 Runtime 恢复或推进时发现的状态不一致异常。

    说明:
        该异常用于表达 checkpoint、任务现场和输入队列之间的
        结构性不一致，属于 `Runtime` 无法继续安全推进的内核错误。
    """


class KernelUnsupportedOperationError(KernelError):
    """表示当前 kernel 尚未支持的操作或能力边界。

    说明:
        当 `kernel` 已识别到调用方请求的语义，但当前阶段明确不支持时，
        应优先抛出该异常，而不是直接使用内置 `NotImplementedError`。
    """


class KernelToolConfigurationError(KernelError):
    """表示工具注册、装配或归一化阶段的配置错误。

    说明:
        该异常用于表达工具定义本身不合法，例如重复 id、缺失必要元信息
        或传入对象不满足工具装配约束。
    """


class KernelToolLookupError(KernelError):
    """表示工具查找失败异常。

    说明:
        当 Runtime 或 ToolService 已明确知道要查找某个工具，但当前
        工具视图中不存在对应对象时，应抛出该异常。
    """
