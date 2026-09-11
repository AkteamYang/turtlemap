#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/02 18:26
# @Author  : YaHaoo
# @File    : agent.py

"""示例服务端工具与 Agent 构造。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import random

from pydantic import BaseModel, Field

from turtlemap import TurtleMapConfig
from turtlemap.kernel.models.enums import ToolResultStatus
from turtlemap.kernel.tool import ToolDescriptor, ToolResult, tool
from turtlemap.os.agent import Agent
from turtlemap.os.context.models import SystemInstruction
from turtlemap.os.tool.build_in.recollection import K_TOOL_NAME_RECOLLECTION
from turtlemap.os.tool.model import ToolInputContext


class WeatherQueryInput(BaseModel):
    """表示天气工具输入参数。"""

    # 待查询天气的城市名称。
    city: str = Field(description="城市名")


class CurrentLocationInput(BaseModel):
    """表示当前位置工具输入参数。"""


@tool(
    descriptor=ToolDescriptor(
        name="query_weather",
        capability="查询指定城市的 mock 天气数据。",
        use_cases=[],
        anti_use_cases=[],
    ),
    input_model=WeatherQueryInput,
    # requires_confirmation=True
)
async def query_weather(input_model: WeatherQueryInput) -> ToolResult:
    """返回指定城市的 mock 天气数据。

    参数:
        input_model: 已由 ToolService 校验并绑定上下文的工具输入模型。

    返回:
        可回传给 LLM 的标准工具结果。
    """

    tool_context = ToolInputContext.get_input_context(input_model)
    event_bus_id = tool_context.event_bus_id if tool_context else ""
    city = input_model.city.strip() or "未知城市"
    await asyncio.sleep(random.randint(100, 400)/1000.0)
    return ToolResult(
        content=f"{city} 当前晴，26 摄氏度，东北风 2 级，湿度 48%。这是 mock 天气数据。",
        # content=f"{city} 调用服务失败。",
        purpose="provide real-time weather information",
        raw_data={
            "city": city,
            "weather": "sunny",
            "temperature_celsius": 26,
            "wind": "东北风 2 级",
            "humidity": "48%",
            "event_bus_id": event_bus_id,
            "mock": True,
        },
        # status=ToolResultStatus.FAILED,
    expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
)


@tool(
    descriptor=ToolDescriptor(
        name="获取位置",
        capability="获取用户当前的 mock 地理位置。",
        use_cases=[],
        anti_use_cases=[],
    ),
    tool_id="get_current_location",
    input_model=CurrentLocationInput,
    requires_confirmation=True
)
async def get_current_location(input_model: CurrentLocationInput) -> ToolResult:
    """返回当前 mock 地理位置。

    参数:
        input_model: 已由 ToolService 校验并绑定上下文的工具输入模型。

    返回:
        包含位置名称、坐标和精度的标准工具结果。
    """

    tool_context = ToolInputContext.get_input_context(input_model)
    event_bus_id = tool_context.event_bus_id if tool_context else ""

    # 示例服务未接入真实定位能力，固定返回可复现的 mock 定位结果。
    await asyncio.sleep(random.randint(300, 800) / 1000.0)
    return ToolResult(
        content="当前位置：上海市浦东新区，陆家嘴街道（mock 定位数据）。",
        purpose="provide the user's current location for location-aware tasks",
        raw_data={
            "location_name": "上海市浦东新区陆家嘴街道",
            "latitude": 31.236,
            "longitude": 121.501,
            "accuracy_meters": 50,
            "event_bus_id": event_bus_id,
            "mock": True,
        },
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=100),
    )


def build_agent(config: TurtleMapConfig) -> Agent:
    """构建 Agent。

    参数:
        config: TurtleMap 运行配置。

    返回:
        已装配的 Agent。
    """

    return Agent(
        agent_name="main_agent",
        system=SystemInstruction(
            role="你是一个可靠的 AI 通用助手。",
            objective="理解用户目标并将结果整理成自然、清晰的回答。",
            constraints=(
                "- 回答风格应当是拟人的，符合人类用语习惯，因此不要在回答中直接描述系统内部的工具调用、上下文检索或记忆读取等过程，"
                f"推荐你可以换拟人的说法，我给你提供几个例子参考，但不需要照抄：'调用搜索工具'->'我查一下'，'调用{K_TOOL_NAME_RECOLLECTION}'->'我回忆一下'。\n"
                "- 在调用工具前建议先输出简短的思考避免直接调用工具。"
            ),
            input_format="用户会用自然语言提出问题或任务。",
            output_format="回答语言与用户输入保持一致",
        ),
        config=config,
        tools=[query_weather, get_current_location],
    )
