#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved 
#
# @Time    : 2026/07/21 09:51
# @Author  : YaHaoo
# @File    : model.py

"""os 层工具输入上下文模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from turtlemap.os.tool.service import ToolService


ATTR_TOOL_INPUT_CONTEXT = "_tool_input_context"


@dataclass
class ToolInputContext:
    """表示一次工具调用绑定到输入模型上的运行时上下文。

    属性:
        event_bus_id: 当前工具调用所属的事件通道标识。
        tool_service: 当前工具调用所属的 ToolService，可用于读取 os 层上下文。

    说明:
        该上下文通过私有属性挂到工具输入模型上，避免污染工具参数 schema。
    """

    # 当前工具调用所属的事件通道标识。
    event_bus_id: str | None = None

    # 当前工具调用所属的 ToolService，供内置工具读取运行期上下文。
    tool_service: "ToolService | None" = None

    def bind_to_input_model(self, input_model: BaseModel) -> None:
        """将当前工具上下文绑定到已反序列化的输入模型。

        参数:
            input_model: 当前工具调用的 pydantic 输入模型实例。

        返回:
            无返回值。
        """

        setattr(input_model, ATTR_TOOL_INPUT_CONTEXT, self)

    @classmethod
    def get_input_context(cls, input_model: BaseModel) -> "ToolInputContext | None":
        """从工具输入模型中读取已绑定的工具上下文。

        参数:
            input_model: 当前工具调用的 pydantic 输入模型实例。

        返回:
            已绑定的工具上下文；未绑定时返回 None。
        """

        return getattr(input_model, ATTR_TOOL_INPUT_CONTEXT, None)
