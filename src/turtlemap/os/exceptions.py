#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 09:28
# @Author  : YaHaoo
# @File    : exceptions.py

"""turtlemap os 层异常定义。"""

from __future__ import annotations

from turtlemap.shared.exceptions import ErrorBase


class OSErrorBase(ErrorBase):
    """表示 os 层基础异常。"""

class OSRuntimeError(OSErrorBase):
    """表示 os 层运行时异常。"""


class SessionStateChangedError(OSErrorBase):
    """表示 SessionState 保存期间目标会话已被其他执行流程推进。"""


class ContextCompressionError(OSErrorBase):
    """表示上下文压缩链路中的基础异常。"""


class NoCompressibleHistoryError(ContextCompressionError):
    """表示当前没有足够的 history 可供压缩。"""
