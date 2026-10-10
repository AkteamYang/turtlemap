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
from enum import Enum
import random

from pydantic import BaseModel, Field

from turtlemap import TurtleMapConfig
from turtlemap.kernel.models.enums import ToolResultStatus
from turtlemap.kernel.tool import ToolDescriptor, ToolResult, tool
from turtlemap.kernel.tool.models import AgentDescriptor
from turtlemap.os.agent import Agent
from turtlemap.os.context.models import SectionItem, SystemInstruction
from turtlemap.os.context.prompt import build_bullet_list
from turtlemap.os.tool.build_in.recollection import K_TOOL_NAME_RECOLLECTION
from turtlemap.os.tool.model import ToolInputContext


class WeatherQueryInput(BaseModel):
    """表示天气工具输入参数。"""

    # 待查询天气的城市名称。
    city: str = Field(description="城市名")


class CurrentLocationInput(BaseModel):
    """表示当前位置工具输入参数。"""


class OrderStatusQueryInput(BaseModel):
    """表示订单状态查询工具输入参数。"""

    # 待查询状态的订单号。
    order_id: str = Field(description="订单号")


class CustomerIssueType(str, Enum):
    """表示售后问题登记时支持的问题类型。"""

    # 用户对商品、服务或订单提出的一般反馈。
    FEEDBACK = "反馈"

    # 用户申请退回已购商品。
    RETURN = "退货"


class AfterSalesIssueInput(BaseModel):
    """表示售后问题登记工具输入参数。"""

    # 关联售后问题的订单号。
    order_id: str = Field(description="订单号")

    # 当前问题的业务分类。
    issue_type: CustomerIssueType = Field(description="问题类型，仅支持反馈或退货")

    # 用户提交的问题具体描述。
    description: str = Field(description="问题描述")


class HumanServiceInput(BaseModel):
    """表示人工客服转接工具输入参数。"""

    # 关联人工服务请求的订单号。
    order_id: str = Field(description="订单号")

    # 需要人工客服处理的问题描述。
    description: str = Field(description="问题描述")


@tool(
    name="query_weather",
    descriptor=ToolDescriptor(
        summary="查询指定城市的天气。",
        capability_category="network",
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
    # expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
)


@tool(
    name="get_current_location",
    descriptor=ToolDescriptor(
        summary="获取用户当前位置。",
        capability_category="location",
        capability="获取用户当前的 mock 地理位置。",
        use_cases=[
            "用户询问当前位置时",
            "用户的提问需要先获取当前位置时"
        ],
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
        # expires_at=datetime.now(timezone.utc) + timedelta(seconds=100),
    )


@tool(
    name="query_order_status",
    descriptor=ToolDescriptor(
        summary="查询订单当前状态。",
        capability_category="order",
        capability="根据订单号查询 mock 订单的物流与履约状态。",
        use_cases=["用户需要查询订单进度、物流状态或预计送达时间"],
        anti_use_cases=["用户需要登记售后问题或转接人工客服"],
    ),
    input_model=OrderStatusQueryInput,
)
async def query_order_status(input_model: OrderStatusQueryInput) -> ToolResult:
    """查询指定订单号对应的 mock 订单状态。

    参数:
        input_model: 包含待查询订单号的工具输入。

    返回:
        可回传给 LLM 的订单状态结果。
    """

    order_id = input_model.order_id.strip()
    await asyncio.sleep(random.randint(100, 400) / 1000.0)
    return ToolResult(
        content=(
            f"订单 {order_id} 当前状态：已发货，正在配送中，预计明日 18:00 前送达。"
            "这是 mock 订单数据。"
        ),
        purpose="查询订单状态",
        raw_data={
            "order_id": order_id,
            "status": "已发货",
            "delivery_status": "配送中",
            "estimated_delivery_time": "明日 18:00 前",
            "mock": True,
        },
    )


@tool(
    name="register_after_sales_issue",
    descriptor=ToolDescriptor(
        summary="登记订单售后问题。",
        capability_category="after_sales",
        capability="根据订单号、问题类型和问题描述创建 mock 售后工单。",
        use_cases=["用户反馈订单问题或申请退货"],
        anti_use_cases=["用户仅需要查询订单状态或转接人工客服"],
    ),
    input_model=AfterSalesIssueInput,
)
async def register_after_sales_issue(input_model: AfterSalesIssueInput) -> ToolResult:
    """登记指定订单的 mock 售后问题。

    参数:
        input_model: 包含订单号、问题类型和问题描述的工具输入。

    返回:
        可回传给 LLM 的售后工单登记结果。
    """

    order_id = input_model.order_id.strip()
    description = input_model.description.strip()
    ticket_id = f"AS-{order_id[-6:] or 'UNKNOWN'}"
    await asyncio.sleep(random.randint(100, 400) / 1000.0)
    return ToolResult(
        content=(
            f"已为订单 {order_id} 登记{input_model.issue_type.value}问题，"
            f"工单号为 {ticket_id}。这是 mock 售后数据。"
        ),
        purpose="登记售后问题",
        raw_data={
            "ticket_id": ticket_id,
            "order_id": order_id,
            "issue_type": input_model.issue_type.value,
            "description": description,
            "mock": True,
        },
    )


@tool(
    name="request_human_service",
    descriptor=ToolDescriptor(
        summary="转接人工客服处理订单问题。",
        capability_category="after_sales",
        capability="根据订单号和问题描述创建 mock 人工服务请求。",
        use_cases=["用户明确要求人工客服协助处理订单问题"],
        anti_use_cases=["可由订单查询或售后登记直接解决的问题"],
    ),
    input_model=HumanServiceInput,
)
async def request_human_service(input_model: HumanServiceInput) -> ToolResult:
    """为指定订单创建 mock 人工服务请求。

    参数:
        input_model: 包含订单号和问题描述的工具输入。

    返回:
        可回传给 LLM 的人工服务请求结果。
    """

    order_id = input_model.order_id.strip()
    description = input_model.description.strip()
    request_id = f"HS-{order_id[-6:] or 'UNKNOWN'}"
    await asyncio.sleep(random.randint(100, 400) / 1000.0)
    return ToolResult(
        content=(
            f"已为订单 {order_id} 提交人工服务请求，服务单号为 {request_id}。"
            "这是 mock 人工服务数据。"
        ),
        purpose="提交人工服务请求",
        raw_data={
            "request_id": request_id,
            "order_id": order_id,
            "description": description,
            "mock": True,
        },
    )


def build_agent(config: TurtleMapConfig) -> Agent:
    """构建 Agent。

    参数:
        config: TurtleMap 运行配置。

    返回:
        已装配的 Agent。
    """

    # 暂时保留天气 Agent 定义，客服案例中不将其装配到主 Agent 的 handoff 图。
    # weather_agent = Agent(
    #     agent_name="weather_agent",
    #     system=SystemInstruction(
    #         name="天气助手",
    #         role="你是一个天气助手。",
    #         objective="理解用户目标并将结果整理成自然、清晰的回答。",
    #         constraints=(
    #             "- 回答风格应当是拟人的，符合人类用语习惯，因此不要在回答中直接描述系统内部的工具调用、上下文检索或记忆读取等过程，"
    #             f"推荐你可以换拟人的说法，我给你提供几个例子参考，但不需要照抄：'调用搜索工具'->'我查一下'，'调用{K_TOOL_NAME_RECOLLECTION}'->'我回忆一下'。\n"
    #             "- 在调用工具前建议先输出简短的思考（不超过50个字）避免直接调用工具，内容必须使用第二人称‘您’或‘你’直接称呼提问者。"
    #         ),
    #         input_format="用户会用自然语言提出问题或任务。",
    #         output_format="回答语言与用户输入保持一致",
    #     ),
    #     config=config,
    #     tools=[query_weather],
    #     descriptor=AgentDescriptor(
    #         summary="查询指定城市的天气。",
    #         capability_category="network",
    #         capability="查询天气数据，无需明确地址即可调用，工具本身可以向用户澄清",
    #         use_cases=["只要用户有查询天气的意图就可以调用，无需提前澄清意图"],
    #         anti_use_cases=[],
    #     ),
    # )

    pre_sales_agent = Agent(
        agent_name="pre_sales_agent",
        system=SystemInstruction(
            name="售前客服",
            role="你是负责订单状态查询的售前客服。",
            objective="确认订单号后查询订单状态，并用清晰、自然的语言告知用户。",
            constraints=(
                "- 仅处理订单状态查询；\n"
                "- 在调用工具前建议先输出简短的思考（不超过50个字）避免直接调用工具，内容必须使用第二人称‘您’或‘你’直接称呼提问者，语言与用户输入一致。"
            ),
            input_format="用户会提供订单号，或询问订单、物流与配送进度。",
            output_format="回答语言与用户输入保持一致",
        ),
        config=config,
        tools=[query_order_status],
        descriptor=AgentDescriptor(
            summary="处理订单状态、物流与配送进度查询。",
            capability_category="customer_service",
            capability="查询订单当前履约与配送状态；订单号缺失时可以向用户澄清。",
            use_cases=["用户查询订单状态、物流进度或预计送达时间"],
            anti_use_cases=["用户要求登记售后问题或转接人工客服"],
        ),
    )

    after_sales_agent = Agent(
        agent_name="after_sales_agent",
        system=SystemInstruction(
            name="售后客服",
            role="你是负责处理订单售后问题的客服。",
            objective="根据用户诉求登记反馈或退货问题，必要时协助转接人工客服。",
            constraints=(
                "- 仅处理售后问题登记与人工服务请求；\n"
                "- 在调用工具前建议先输出简短的思考（不超过30个字）避免直接调用工具，内容必须使用第二人称‘您’或‘你’直接称呼提问者，语言与用户输入一致。\n"
                "- 完成任务后无需主动追问用户其他需求"
            ),
            custom_sections=[
                SectionItem(
                    header="工作要求",
                    content=build_bullet_list(
                        [
                            "当用户咨询退货时，应当让用户提供订单号（必须）以及退货原因（除非用户拒绝提供，否则必须询问原因）"
                        ]
                    )
                )
            ],
            input_format="用户会提供订单号、问题类型或希望人工客服处理的订单问题。",
            output_format="回答语言与用户输入保持一致",
        ),
        config=config,
        tools=[register_after_sales_issue, request_human_service],
        descriptor=AgentDescriptor(
            summary="处理订单反馈、退货登记及人工客服请求。",
            capability_category="customer_service",
            capability="登记订单反馈或退货问题，并可为订单问题提交人工服务请求。",
            use_cases=["用户反馈订单问题", "用户申请退货", "用户要求人工客服协助"],
            anti_use_cases=["用户仅查询订单状态、物流进度或预计送达时间"],
        ),
    )

    return Agent(
        agent_name="main_agent",
        system=SystemInstruction(
            name="AI 助手",
            role="你是一个可靠的 AI 通用助手。",
            objective="理解用户目标并正确调用工具分配任务。",
            constraints=(
                "- 回答风格应当是拟人的，符合人类用语习惯，因此不要在回答中直接描述系统内部的工具调用、上下文检索或记忆读取等过程，"
                f"推荐你可以换拟人的说法，我给你提供几个例子参考，但不需要照抄：'调用搜索工具'->'我查一下'，'调用{K_TOOL_NAME_RECOLLECTION}'->'我回忆一下'。\n"
                "- 在调用工具前建议先输出简短的思考（不超过30个字）避免直接调用工具，内容必须使用第二人称‘您’或‘你’直接称呼提问者，语言与用户输入一致。\n"
            ),
            input_format="用户会用自然语言提出问题或任务。",
            output_format="回答语言与用户输入保持一致",
        ),
        config=config,
        tools=[get_current_location],
        handoffs=[pre_sales_agent, after_sales_agent],
    )
