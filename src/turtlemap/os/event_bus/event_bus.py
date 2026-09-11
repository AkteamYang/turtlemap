#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/15 13:53
# @Author  : YaHaoo
# @File    : event_bus.py

"""os 层 Runtime 事件总线实现。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable
import traceback
from typing import ClassVar, TypeAlias

from turtlemap.os.exceptions import OSRuntimeError
from turtlemap.shared.ids import PREFIX_EVENT_BUS_ID, PREFIX_SUBSCRIBE_ID, generate_prefixed_id
from turtlemap.shared.logger import Logger

from .models import RuntimeEvent, RuntimeEventType

RuntimeEventListener: TypeAlias = Callable[[RuntimeEvent], Awaitable[None] | None]
RuntimeEventCompletionListener: TypeAlias = Callable[[RuntimeEvent], Awaitable[None] | None]


@dataclass(slots=True)
class _EventSubscription:
    """表示单个事件监听订阅。

    属性:
        listener: 当前监听回调函数。
        event_types: 当前订阅允许触达的事件类型集合；为空表示监听所有事件。
    """

    # 当前监听回调函数。
    listener: RuntimeEventListener

    # 当前事件完成全部 listener 通知后触发的回调函数。
    completion_listener: RuntimeEventCompletionListener | None = None

    # 当前订阅允许触达的事件类型集合；None 表示监听所有事件。
    event_types: set[RuntimeEventType] | None = None


@dataclass(slots=True)
class _EventBusChannel:
    """表示单个 event_bus_id 对应的运行时通道状态。

    属性:
        listener_id2subscription: listener id 到订阅信息的映射。
        lock: 当前通道的发送锁，用于保证事件之间按 sequence 串行通知。
    """

    # listener id 到订阅信息的映射。
    listener_id2subscription: dict[str, _EventSubscription] = field(default_factory=dict)

    # 当前通道的发送锁，保证事件按顺序处理。
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # 当前持有通道锁的协程；用于识别同一 task 内的递归 publish。
    locked_task: asyncio.Task | None = None


class EventBus:
    """表示 os 层事件总线。

    说明:
        事件总线是运行时通知结构，服务一次或多次 run 期间的事件发送与接收。
        每个 event_bus_id 独立维护 listener 集合；事件 sequence 由总线内部
        全局自增计数器分配。
    """

    # event_bus_id 到通道状态的映射。
    _event_bus_id2channel: ClassVar[dict[str, _EventBusChannel]] = {}

    # 事件总线内部全局自增计数器。
    _next_sequence: ClassVar[int] = 1

    @classmethod
    def create_event_bus(cls) -> str:
        """创建一个新的事件通道。

        返回:
            新创建的 event_bus_id。
        """

        event_bus_id = generate_prefixed_id(PREFIX_EVENT_BUS_ID)
        cls._event_bus_id2channel[event_bus_id] = _EventBusChannel()
        return event_bus_id

    @classmethod
    def close_event_bus(cls, event_bus_id: str) -> None:
        """关闭指定事件通道并清理监听者。

        参数:
            event_bus_id: 待关闭的事件通道标识。

        返回:
            无返回值。
        """

        cls._event_bus_id2channel.pop(event_bus_id, None)

    @classmethod
    def subscribe(
        cls,
        event_bus_id: str,
        listener: RuntimeEventListener,
        completion_listener: RuntimeEventCompletionListener | None = None,
        event_types: tuple[RuntimeEventType, ...] | None = None,
    ) -> str:
        """为指定事件通道注册监听者。

        参数:
            event_bus_id: 目标事件通道标识。
            listener: 当前待注册的事件回调函数。
            completion_listener: 当前事件完成全部 listener 通知后触发的可选回调函数。
            event_types: 当前监听者关注的事件类型集合；为空时监听所有事件。

        返回:
            当前监听者的注册标识。
        """

        channel = cls._ensure_channel(event_bus_id)
        if channel is None:
            raise OSRuntimeError(f"订阅的 channel 不存在, event_bus_id {event_bus_id}")

        listener_id = generate_prefixed_id(PREFIX_SUBSCRIBE_ID)
        channel.listener_id2subscription[listener_id] = _EventSubscription(
            listener=listener,
            completion_listener=completion_listener,
            event_types=set(event_types) if event_types is not None else None,
        )
        return listener_id

    @classmethod
    def unsubscribe(cls, event_bus_id: str, listener_id: str) -> None:
        """取消指定监听者注册。

        参数:
            event_bus_id: 目标事件通道标识。
            listener_id: 待取消的监听者注册标识。

        返回:
            无返回值。
        """

        channel = cls._event_bus_id2channel.get(event_bus_id)
        if channel is None:
            return
        channel.listener_id2subscription.pop(listener_id, None)

    @classmethod
    async def publish(
        cls,
        event_bus_id: str,
        event: RuntimeEvent,
    ) -> RuntimeEvent | None:
        """发布一个 RuntimeEvent 并等待全部监听者处理完成。

        参数:
            event_bus_id: 目标事件通道标识。
            event: 待发送的 Runtime 事件；`sequence` 会由 event bus 覆盖分配。

        返回:
            已写入 sequence 的事件对象。

        说明:
            同一个 event_bus_id 下事件按 sequence 串行通知。单个事件的多个
            listener 会并发执行；单个 listener 异常会被隔离记录，不影响其他
            listener，也不影响总线继续运行。
        """
        channel = cls._ensure_channel(event_bus_id)
        if channel is None:
            return

        # 单个事件发布流程，包含普通监听和发布完成后的回调监听。
        event.created_ts_ms = int(time.time() * 1000) if event.created_ts_ms <= 0 else event.created_ts_ms
        async def block():
            event.sequence = cls._allocate_sequence()
            await cls._notify_listeners(event_bus_id, channel, event)

            # 完成回调中可能自动发送 final 事件，因此需要允许同一 task 递归进入。
            await cls._notify_completion_listeners(event_bus_id, channel, event)

        # 同一 task 已持有锁时直接执行，避免 completion 回调里递归 publish 死锁。
        if channel.lock.locked() and channel.locked_task == asyncio.current_task():
            await block()
        else:
            async with channel.lock:
                channel.locked_task = asyncio.current_task()
                try:
                    await block()
                finally:
                    channel.locked_task = None
        return event

    @classmethod
    def reset(cls) -> None:
        """重置事件总线运行时状态。

        返回:
            无返回值。

        说明:
            该方法主要服务测试隔离；业务运行中不应在仍有活跃 run 时调用。
        """

        cls._event_bus_id2channel = {}
        cls._next_sequence = 1

    @classmethod
    def _ensure_channel(cls, event_bus_id: str) -> _EventBusChannel | None:
        """获取指定事件通道，缺失时抛出明确错误。

        参数:
            event_bus_id: 目标事件通道标识。

        返回:
            当前事件通道状态。
        """

        channel = cls._event_bus_id2channel.get(event_bus_id)
        if channel is None:
            stack = "".join(traceback.format_stack())
            Logger.logger.warning(f"事件通道不存在或已关闭：event_bus_id={event_bus_id}, trace: {stack}")
        return channel

    @classmethod
    def _allocate_sequence(cls) -> int:
        """分配下一个全局事件序号。

        返回:
            当前事件应使用的 sequence。
        """

        sequence = cls._next_sequence
        cls._next_sequence += 1
        return sequence

    @classmethod
    async def _notify_listeners(
        cls,
        event_bus_id: str,
        channel: _EventBusChannel,
        event: RuntimeEvent,
    ) -> None:
        """并发通知单个事件的所有监听者。

        参数:
            channel: 当前事件通道状态。
            event: 当前待通知的事件对象。

        返回:
            无返回值。
        """

        subscription_items = [
            (listener_id, subscription)
            for listener_id, subscription in channel.listener_id2subscription.items()
            if subscription.event_types is None or event.event_type in subscription.event_types
        ]
        if not subscription_items:
            return

        results = await asyncio.gather(
            *[
                cls._call_listener(subscription.listener, event)
                for _, subscription in subscription_items
            ],
            return_exceptions=True,
        )
        for (listener_id, _), result in zip(subscription_items, results, strict=True):
            if isinstance(result, BaseException):
                Logger.logger.error(
                    "event bus 通知异常 "
                    f"event_bus_id={event_bus_id}, "
                    f"event_id={event.event_id}, "
                    f"sequence={event.sequence}, "
                    f"listener_id={listener_id}, "
                    f"error={repr(result)}",
                    exc_info=result,
                )

    @classmethod
    async def _notify_completion_listeners(
        cls,
        event_bus_id: str,
        channel: _EventBusChannel,
        event: RuntimeEvent,
    ) -> None:
        """在普通 listener 完成后并发通知当前事件的完成回调。

        参数:
            channel: 当前事件通道状态。
            event: 当前已经完成普通 listener 通知的事件对象。

        返回:
            无返回值。
        """
        for listener_id, subscription in channel.listener_id2subscription.items():
            if subscription.completion_listener is not None:
                try:
                    await cls._call_completion_listener(subscription.completion_listener, event)
                except Exception as e:
                    Logger.logger.exception(
                        "event bus 完成回调异常 "
                        f"event_bus_id={event_bus_id}, "
                        f"event_id={event.event_id}, "
                        f"sequence={event.sequence}, "
                        f"listener_id={listener_id}, "
                        f"error={repr(e)}",
                    )

    @classmethod
    async def _call_listener(
        cls,
        listener: RuntimeEventListener,
        event: RuntimeEvent,
    ) -> None:
        """调用单个监听者并兼容同步或异步回调。

        参数:
            listener: 当前监听回调。
            event: 当前待传入的事件。

        返回:
            无返回值。
        """

        _ = cls
        result = listener(event)
        if isawaitable(result):
            await result

    @classmethod
    async def _call_completion_listener(
        cls,
        listener: RuntimeEventCompletionListener,
        event: RuntimeEvent,
    ) -> None:
        """调用单个完成回调并兼容同步或异步实现。

        参数:
            listener: 当前完成回调。
            event: 当前已经完成普通 listener 通知的事件对象。

        返回:
            无返回值。
        """

        _ = cls
        result = listener(event)
        if isawaitable(result):
            await result
