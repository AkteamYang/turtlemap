#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/15 13:53
# @Author  : YaHaoo
# @File    : collector.py

"""Runtime 事件结果收集器。"""

from __future__ import annotations
from copy import deepcopy
from typing import Sequence, cast

from sympy.strategies.branch.tests.test_core import even

from turtlemap.os.exceptions import OSRuntimeError
from turtlemap.shared.typing import ensure_instance

from .models import (
    CustomEvent,
    ContextCompressionEvent,
    ContextCompressionMode,
    InputEvent,
    InterruptedEvent,
    MessageEvent,
    RuntimeEvent,
    RuntimeEventPhase,
    RuntimeEventType,
    TaskOutput,
    ToolCallEvent,
    ToolResultEvent,
)


class ResultCollector:
    """表示基于事件监听的 Runtime 输出收集器。

    说明:
        ResultCollector 与 event bus 解耦，只作为普通 listener 接收事件。
        它将已闭合事件链转换为对应的 FINAL RuntimeEvent；过程事件只用于
        聚合，不直接进入最终 outputs。
    """

    __slots__ = (
        "event_bus_id",
        "_event_id2events",
        "_parent_event_id2event_ids",
    )

    def __init__(self, event_bus_id: str) -> None:
        """初始化指定事件通道的结果收集器。

        参数:
            event_bus_id: 当前收集器监听的事件通道标识。
        """

        # 当前收集器绑定的事件通道标识。
        self.event_bus_id = event_bus_id

        # event_id 到同一逻辑事件链路事件列表的映射。
        self._event_id2events: dict[str, list[RuntimeEvent]] = {}

        # parent_event_id 到子事件 id 列表的映射。
        self._parent_event_id2event_ids: dict[str, list[str]] = {}


    @staticmethod
    def listen_event_types() -> tuple[RuntimeEventType, ...]:
        """返回 ResultCollector 需要监听的 Runtime 事件类型。

        返回:
            当前最终结果收集器关注的事件类型元组。
        """

        return (
            RuntimeEventType.INPUT,
            RuntimeEventType.MESSAGE,
            RuntimeEventType.TOOL_CALL,
            RuntimeEventType.TOOL_RESULT,
            RuntimeEventType.CONTEXT_COMPRESSION,
            RuntimeEventType.INTERRUPTED,
        )

    def collect(self, event: RuntimeEvent) -> None:
        """收集单个事件。

        参数:
            event: 当前待收集的 Runtime 事件。

        返回:
            无返回值。
        """

        # 自定义事件不属于 Runtime 标准输出聚合范围，业务层应自行订阅和处理。
        if isinstance(event, CustomEvent) or event.event_type == RuntimeEventType.CUSTOM:
            return

        # 异步压缩只作为观测事件通知，不进入 RuntimeRunResult 输出收集。
        if (
            event.event_type == RuntimeEventType.CONTEXT_COMPRESSION
            and isinstance(event, ContextCompressionEvent)
            and event.compression_mode == ContextCompressionMode.ASYNC
        ):
            return

        # 同一流式事件链共享 event_id，只有首次收集该事件链时登记父子关系。
        if event.event_id not in self._event_id2events and event.parent_event_id:
            self._parent_event_id2event_ids.setdefault(
                event.parent_event_id,
                [],
            ).append(event.event_id)

        self._event_id2events.setdefault(event.event_id, []).append(event)

    async def finalize_outputs(self, event: RuntimeEvent) -> None:
        """在事件完成普通 listener 通知后尝试生成最终稳定输出。

        参数:
            event: 当前已经完成普通 listener 通知的事件对象。

        返回:
            无返回值。

        说明:
            EventBus 会在每个事件完成普通 listener 通知后触发该回调。
            collector 只在事件链闭合阶段刷新最终输出，避免 STARTED 或
            IN_PROGRESS 阶段过早聚合未完成事件链。
        """

        # 按照预定发送了END就不会发送FINAL，因此仅对RuntimeEventPhase.END进行聚合
        if event.event_phase not in {RuntimeEventPhase.END} \
            or not self.has_collected_event_chain(event.event_id):
            return

        # 事件链闭合后主动生成并派发 FINAL 事件。
        final_event = self._build_final_event(event)

        from .event_bus import EventBus

        await EventBus.publish(self.event_bus_id, final_event)

    def get_outputs(self) -> Sequence[TaskOutput]:
        """返回当前已收集的稳定输出分组。

        返回:
            按 task_id 分组后的 TaskOutput 列表；每组包含任务输入和对应输出序列。
        """

        final_events = [
            self._build_final_event_by_id(event_id, set())
            for event_id in self._get_top_level_event_ids()
        ]
        final_events.sort(key=lambda final_event: final_event.sequence)
        return self._build_task_outputs(final_events)


    def get_final_event_with_event_id(self, event_id: str) -> RuntimeEvent:
        """根据 event_id 聚合对应的 FINAL RuntimeEvent。

        参数:
            event_id: 当前待聚合事件链路的稳定 id。

        返回:
            可直接被 Runtime 或业务层消费的 FINAL RuntimeEvent。
        """

        return self._build_final_event_by_id(event_id, set())

    def has_collected_event_chain(self, event_id: str) -> bool:
        """判断指定事件链是否已经被当前收集器收集。

        参数:
            event_id: 待检查的事件链唯一标识。

        返回:
            若当前收集器已经记录过该 event_id 对应的事件链，则返回 True。
        """

        return event_id in self._event_id2events


    @staticmethod
    def _group_final_events_by_task_id(
        final_events: Sequence[RuntimeEvent],
    ) -> list[list[RuntimeEvent]]:
        """按 task_id 将顶层 FINAL 事件聚合为内部临时二维列表。

        参数:
            final_events: 已按 sequence 排序的顶层 FINAL 事件列表。

        返回:
            按 task_id 首次出现顺序排列的事件分组列表。
        """

        task_ids: list[str | None] = []
        task_id2final_events: dict[str | None, list[RuntimeEvent]] = {}
        for final_event in final_events:
            # 显式记录 task 首次出现顺序，避免依赖 dict 插入顺序这一隐含知识。
            if final_event.task_id not in task_id2final_events:
                task_ids.append(final_event.task_id)
            task_id2final_events.setdefault(final_event.task_id, []).append(final_event)
        return [task_id2final_events[task_id] for task_id in task_ids]

    def _build_task_outputs(
        self,
        final_events: Sequence[RuntimeEvent],
    ) -> list[TaskOutput]:
        """按任务输入构建业务层可直接消费的 TaskOutput 列表。

        参数:
            final_events: 已按 sequence 排序的顶层 FINAL 事件列表。

        返回:
            按 task_id 首次出现顺序排列的任务输出列表。
        """

        task_outputs: list[TaskOutput] = []
        for final_event_group in self._group_final_events_by_task_id(final_events):
            if not final_event_group:
                continue

            input_event = ensure_instance(final_event_group[0], InputEvent)
            if input_event.event_type != RuntimeEventType.INPUT:
                raise OSRuntimeError(
                    f"task 第一条event 必须是 INPUT类型：task_id={input_event.task_id}, 当前 type={input_event.event_type}"
                )

            task_outputs.append(
                TaskOutput(
                    input=input_event.input,
                    outputs=final_event_group[1:],
                )
            )
        return task_outputs

    def _build_child_final_events(
        self,
        parent_event_id: str,
        building_event_ids: set[str] | None = None,
    ) -> list[RuntimeEvent]:
        """递归构建指定父事件下的子 FINAL 事件列表。

        参数:
            parent_event_id: 当前父事件对应的 event id。
            building_event_ids: 当前递归链路中正在构建的事件 id 集合。

        返回:
            按起始事件 sequence 排序的子 FINAL 事件列表。
        """

        if building_event_ids is None:
            building_event_ids = set()

        final_events: list[RuntimeEvent] = []
        for child_event_id in self._parent_event_id2event_ids.get(parent_event_id, []):
            final_events.append(
                self._build_final_event_by_id(
                    event_id=child_event_id,
                    building_event_ids=building_event_ids,
                )
            )
        return sorted(final_events, key=lambda event: event.sequence)

    def _build_final_event_by_id(
        self,
        event_id: str,
        building_event_ids: set[str],
    ) -> RuntimeEvent:
        """根据 event_id 递归构建携带子事件树的 FINAL RuntimeEvent。

        参数:
            event_id: 当前待聚合事件链路的稳定 id。
            building_event_ids: 当前递归链路中正在构建的事件 id 集合。

        返回:
            携带稳定完整载荷的 FINAL RuntimeEvent。
        """

        if event_id in building_event_ids:
            raise OSRuntimeError(
                f"构建 FINAL 事件时发现 parent_event_id 循环：event_id={event_id}"
            )

        events = self._event_id2events.get(event_id, [])
        if not events:
            raise OSRuntimeError(f"构建 FINAL 事件时未找到 events：event_id={event_id}")

        building_event_ids.add(event_id)
        try:
            first_event = events[0]
            match first_event.event_type:
                case RuntimeEventType.INPUT:
                    final_event = ensure_instance(events[0], InputEvent)
                case RuntimeEventType.MESSAGE:
                    final_event = MessageEvent.create_final_event_from_events(
                        cast(list[MessageEvent], events)
                    )
                case RuntimeEventType.TOOL_CALL:
                    final_event = ensure_instance(events[0], ToolCallEvent)
                case RuntimeEventType.TOOL_RESULT:
                    tool_result_event = ToolResultEvent.create_final_event_from_events(
                        cast(list[ToolResultEvent], events)
                    )
                    tool_result_event.state_events = self._build_child_final_events(
                        parent_event_id=tool_result_event.event_id,
                        building_event_ids=building_event_ids,
                    )
                    final_event = tool_result_event
                case RuntimeEventType.CONTEXT_COMPRESSION:
                    final_event = ContextCompressionEvent.create_final_event_from_events(
                        cast(list[ContextCompressionEvent], events)
                    )
                case RuntimeEventType.INTERRUPTED:
                    final_event = ensure_instance(events[-1], InterruptedEvent)
                case RuntimeEventType.CUSTOM:
                    raise OSRuntimeError(
                        "CustomEvent 不支持 Runtime 标准聚合，请由业务层处理"
                    )
                case _:
                    raise OSRuntimeError(
                        "当前事件类型不支持构建 FINAL 事件："
                        f"event_type={first_event.event_type}"
                    )
        finally:
            building_event_ids.remove(event_id)

        return final_event

    def _get_top_level_event_ids(self) -> list[str]:
        """获取当前收集器中所有顶层事件 id。

        返回:
            按起始 sequence 排序后的顶层事件 id 列表。
        """
        event_ids: list[str] = []
        for event_id, events in self._event_id2events.items():
            if not events:
                continue

            first_event = min(events, key=lambda event: event.sequence)
            # 显式 parent 设计下，父级不在本次输出事件集合中即视为顶层输出。
            if first_event.parent_event_id not in self._event_id2events:
                event_ids.append(event_id)
        return sorted(
            event_ids,
            key=lambda current_event_id: min(
                event.sequence for event in self._event_id2events[current_event_id]
            ),
        )

    def _build_final_event(self, event: RuntimeEvent) -> RuntimeEvent:
        """根据已闭合事件链构建对外通知用 FINAL 事件。

        参数:
            event: 当前触发完成回调的 END 事件。

        返回:
            与原事件链同 event_id 的 FINAL 事件，载荷替换为聚合后的稳定结果。

        说明:
            FINAL 事件只作为对外通知使用，collector 在 `collect` 中会忽略
            已收集事件链上的派生 FINAL，避免污染原始 STARTED -> END 聚合序列。
        """

        return self._build_final_event_by_id(event.event_id, set())
