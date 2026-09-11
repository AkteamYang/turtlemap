#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/07 10:14
# @Author  : YaHaoo
# @File    : typing.py

"""turtlemap 共享类型工具。"""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def ensure_instance(value: object, expected_type: type[T], value_name: str | None = None) -> T:
    """将运行时对象收窄为目标类型，不匹配时抛出明确异常。

    参数:
        value: 当前待校验并收窄的对象。
        expected_type: 期望收窄到的目标类型。
        value_name: 当前对象的可选业务名称；为空时使用通用描述。

    返回:
        通过校验后的目标类型对象。

    异常:
        TypeError: 当对象类型与期望类型不一致时抛出。
    """

    actual_type_name = type(value).__name__
    expected_type_name = expected_type.__name__
    if not isinstance(value, expected_type):
        if value_name:
            raise TypeError(
                f"{value_name} 的类型应为 {expected_type_name}，实际为 {actual_type_name}"
            )
        raise TypeError(f"对象类型应为 {expected_type_name}，实际为 {actual_type_name}")
    return value
