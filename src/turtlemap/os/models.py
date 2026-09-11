#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/12 14:05
# @Author  : YaHaoo
# @File    : models.py

"""turtlemap os 层会话状态模型定义。"""

from __future__ import annotations
from pydantic import Field

from turtlemap.kernel.models import (
    BaseSessionState,
    BaseAgentState,
)
from turtlemap.kernel.models.enums import EventType
from turtlemap.kernel.models.models import BaseProcessingTask, Input
from turtlemap.os.event_bus.event_bus import EventBus
from turtlemap.os.event_bus.models import InputEvent, InterruptedEvent, RunntimeEventBuffer, RuntimeEvent, RuntimeEventType
from turtlemap.shared.typing import ensure_instance


@BaseAgentState.register_type
class AgentState(BaseAgentState):
    """表示 os 层可持久化的 Agent 主观状态。

    说明:
        该模型继承 kernel `BaseAgentState`，用于恢复 os 层运行现场。
        结构兼容边界由业务层 SessionState 子类整体承载。
    """

    # 当前 AgentState 多态类型名，用于 SessionState 恢复 os 层状态子类。
    type_name: str = Field(default="agent_state")


class SessionState(BaseSessionState):
    """表示 os 层可扩展的会话状态。

    说明:
        该模型继承 kernel 最小 `BaseSessionState`，用于承接业务层在会话维度
        的扩展语义。会话持久化身份、乐观锁版本和数据结构版本属于应用层
        存储模型，不进入 SDK 默认 SessionState。
    """

    # 当前会话的唯一标识。
    session_id: str = Field(default="")

    # 标识一次 Runtime 运行，拿到outputs
    run_id: str = Field(default="")

@BaseProcessingTask.register_type
class ProcessingTask(BaseProcessingTask):
    """表示 os 层可恢复的任务现场。

    说明:
        除 kernel 定义的任务状态外，本类保存任务范围内尚未被业务层消费的
        Runtime 事件。事件 buffer 随任务持久化，避免并发任务互相混入回放结果。
    """

    # 当前 ProcessingTask 多态类型名，用于恢复 os 层任务子类。
    type_name: str = Field(default="processing_task")

    # 当前任务产生、等待恢复重放的 Runtime 事件。
    event_buffer: RunntimeEventBuffer = Field(default_factory=RunntimeEventBuffer)

    @staticmethod
    async def publish(
        task: BaseProcessingTask,
        event_bus_id: str,
        event: RuntimeEvent,
    ) -> None:
        """通过指定通道发布任务历史与当前 Runtime 事件。

        参数:
            task: 当前事件所属任务。
            event_bus_id: 当前 Runtime 绑定的目标事件通道标识。
            event: 当前需要发布的 Runtime 事件。

        返回:
            无返回值。

        说明:
            任务恢复后首次产生新事件时，会先重放未消费的历史事件。通道标识只
            属于投递边界，不会写入 RuntimeEvent 或持久化事件 buffer。
        """

        processing_task = ensure_instance(task, ProcessingTask, "事件所属任务")
        if not processing_task.event_buffer.did_flush_history_events:
            for history_event in processing_task.event_buffer.events:

                # 中断恢复后将buffer中数据标记为历史数据
                if event.event_type == RuntimeEventType.INPUT:
                    input_event = ensure_instance(event, InputEvent)
                    if input_event.input.events[0].event_type == EventType.INTERRUPTION_RESPONSE:
                        history_event.is_history_event = True
                await EventBus.publish(event_bus_id, history_event)
            processing_task.event_buffer.did_flush_history_events = True
        await EventBus.publish(event_bus_id, event)
