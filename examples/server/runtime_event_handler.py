#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/03 22:42
# @Author  : YaHaoo
# @File    : runtime_event_handler.py

"""RuntimeEvent 流式处理器。"""

from __future__ import annotations

import asyncio
import time

from turtlemap.os.event_bus import RuntimeEvent

from .constants import SSE_HEARTBEAT_INTERVAL_SECONDS
from .projector import RuntimeEventProjector
from .schemas import ServerMessageEvent, ServerMessageEventType, SseEventType
from .sse import format_sse
from .stream_context import CompletionStreamContext
from .stream_store import RedisRunStreamStore, SseRecord


class RuntimeEventHandler:
    """处理一次 Runtime 运行期间的事件投影、缓存和 SSE 发送。

    字段:
        _session_id: 当前会话 id。
        _run_id: 当前 Runtime 运行 id。
        _context: 当前 SSE 请求上下文，用于判断响应通道是否取消。
        _queue: 接口层消费的 SSE 字符串队列。
        _stream_store: 当前 run 的短期 Redis Stream 缓存。
        _next_sequence_value: 当前 run 内下一个待分配的 SSE id 数值。
        _start: 当前 handler 创建时间，用于计算 complete 耗时。
        _heartbeat_task: 当前 run 的心跳发送任务。
        _heartbeat_stop_event: 通知心跳任务退出的状态。

    约束:
        Handler 是单次 Runtime 运行的临时对象，应在 Runtime 初始化 session 后创建。
    """

    def __init__(
        self,
        session_id: str,
        run_id: str,
        context: CompletionStreamContext,
        queue: asyncio.Queue[str],
        stream_store: RedisRunStreamStore,
    ) -> None:
        """初始化运行期事件处理器。

        参数:
            session_id: 当前会话 id。
            run_id: 当前 Runtime 运行 id。
            context: 当前 SSE 请求上下文。
            queue: 接口层消费的 SSE 字符串队列。
            stream_store: 短期 Redis Stream 缓存。
        """

        # Handler 只依赖当前运行的两个稳定标识，不持有完整会话状态。
        self._session_id = session_id
        self._run_id = run_id

        # 当前 SSE 请求上下文，客户端断开时只影响输出，不影响后台生成。
        self._context = context

        # 外层接口消费的 SSE 字符串队列。
        self._queue = queue

        # 短期 Redis Stream 缓存，用于中断续接。
        self._stream_store = stream_store

        # 单次 SSE 流内自增 id，恢复续接依赖 Redis Stream 中保存的 sse_id。
        self._next_sequence_value = 1

        # Handler 生命周期覆盖一次完整 agent run，用单调时钟计算 complete 耗时。
        self._start = time.perf_counter()

        # 当前 run 的心跳任务，由 init 启动，由服务端收尾阶段显式停止。
        self._heartbeat_task: asyncio.Task[None] | None = None

        # 心跳任务停止信号，避免用 cancel 打断正在写入 Redis 的心跳。
        self._heartbeat_stop_event = asyncio.Event()

        # SSE id 在真正写出时分配，锁住写出段即可避免并发事件拿号乱序。
        self._emit_lock = asyncio.Lock()

        # 本轮需要写入业务 history 的稳定消息事件。
        self._history_events: list[ServerMessageEvent] = []

    @property
    def history_events(self) -> list[ServerMessageEvent]:
        """返回本轮可写入业务 history 的稳定消息事件。

        返回:
            稳定消息事件列表。
        """

        return self._history_events

    async def init(self) -> None:
        """初始化当前 Handler 的运行期索引。

        返回:
            无返回值。

        约束:
            Handler 创建时必须已经能拿到 run_id。completion 提交还必须携带
            client_event_id，因此 active run 索引在进入 Runtime.run 前注册；
            只有页面首次 resume 不注册该幂等索引。
        """

        if not self._run_id:
            raise RuntimeError("RuntimeEventHandler 初始化失败：run_id 不能为空")

        client_event_id = self._context.client_event_id
        if client_event_id:
            await self._stream_store.register_active_run(
                session_id=self._session_id,
                run_id=self._run_id,
                client_event_id=client_event_id,
            )
        elif not self._context.page_resume:
            raise RuntimeError("RuntimeEventHandler 初始化失败：client_event_id 不能为空")

        self._start_heartbeat()

    async def close(self) -> None:
        """关闭当前 handler 的运行期后台任务。

        返回:
            无返回值。

        约束:
            该方法只负责资源兜底清理，不发送业务 complete 事件。
        """

        await self._stop_heartbeat()

    def _start_heartbeat(self) -> None:
        """启动当前 run 的后台心跳任务。

        返回:
            无返回值。

        约束:
            重复调用不会创建多个任务；心跳任务只负责发送 heartbeat，不控制
            Runtime 生命周期。
        """

        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return

        self._heartbeat_stop_event.clear()
        self._heartbeat_task = asyncio.create_task(self._run_heartbeat_loop())

    async def _stop_heartbeat(self) -> None:
        """停止当前 run 的后台心跳任务。

        返回:
            无返回值。

        约束:
            complete 事件发送前应先停止心跳，避免 complete 后继续追加 heartbeat。
        """

        if self._heartbeat_task is None:
            return

        self._heartbeat_stop_event.set()
        await self._heartbeat_task
        self._heartbeat_task = None

    async def listen(self, event: RuntimeEvent) -> None:
        """监听 RuntimeEvent 并写入短期 stream 与发送队列。

        参数:
            event: 当前 Runtime 事件。

        返回:
            无返回值。
        """

        projected_events = RuntimeEventProjector.project_runtime_event(
            event=event,
            run_id=self._run_id,
        )
        self._history_events.extend(self._filter_history_events(projected_events))
        for projected_event in projected_events:
            record = SseRecord(
                sse_id="",
                event=SseEventType.CHUNK,
                data=projected_event.model_dump_json(),
            )
            await self._emit(record)

    def history_add_complete_event(self) -> ServerMessageEvent:
        """创建并记录本轮 complete 业务事件。

        返回:
            本轮 complete 前端事件。

        约束:
            本方法只补齐业务 history 需要的 complete 事件；SSE id 由 `_emit`
            在发送前统一写入 `SseRecord.sse_id`。
        """

        complete_event = RuntimeEventProjector.build_complete_event(
            session_id=self._session_id,
            run_id=self._run_id,
            task_id=self._get_latest_history_task_id(),
            duration_ms=self._calculate_duration_ms(),
        )
        self._history_events.extend(self._filter_history_events([complete_event]))
        return complete_event

    async def send_complete(self, complete_event: ServerMessageEvent) -> None:
        """发送本次 Runtime 业务完成事件。

        返回:
            无返回值。

        约束:
            complete 是普通 chunk，也需要写入 Redis Stream，便于恢复流判断结束。
            发送 complete 前必须先停止 heartbeat，保证 complete 是最后一个 chunk。
        """

        await self._stop_heartbeat()
        complete_record = SseRecord(
            sse_id="",
            event=SseEventType.CHUNK,
            data=complete_event.model_dump_json(),
        )
        await self._emit(complete_record, is_finish=True)

    async def _send_heartbeat(self) -> None:
        """发送并缓存一次 heartbeat。

        返回:
            无返回值。

        约束:
            heartbeat 进入 Redis Stream，用于长时间无业务输出时推进 last_event_id。
        """

        # stop 一旦设置，heartbeat 不能再写入 Redis，避免 complete 之后继续追加事件。
        if self._heartbeat_stop_event.is_set():
            return

        heartbeat_record = SseRecord(
            sse_id="",
            event=SseEventType.HEARTBEAT,
            data="",
        )
        await self._emit(heartbeat_record)

    async def _run_heartbeat_loop(self) -> None:
        """按固定间隔发送当前 run 的 heartbeat。

        返回:
            无返回值。

        约束:
            心跳进入 Redis Stream，并在当前 SSE 响应未取消时写入队列；请求取消
            只影响响应通道，不影响 Redis 中的恢复游标推进。
        """

        while True:
            try:
                await asyncio.wait_for(
                    self._heartbeat_stop_event.wait(),
                    timeout=SSE_HEARTBEAT_INTERVAL_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                await self._send_heartbeat()

    def _next_sequence(self) -> int:
        """分配当前 run 的下一个 SSE id 数值。

        返回:
            当前 run 内下一个单调递增的 SSE id 数值。
        """

        sequence = self._next_sequence_value
        self._next_sequence_value += 1
        return sequence

    def _calculate_duration_ms(self) -> int:
        """计算当前 handler 从创建到当前时刻的耗时。

        返回:
            当前 handler 生命周期累计耗时，单位毫秒。
        """

        return max(0, int((time.perf_counter() - self._start) * 1000))

    def _get_latest_history_task_id(self) -> str | None:
        """读取本轮稳定 history 中最后一个事件的 task_id。

        返回:
            最后一条稳定 history 事件的 task_id；本轮无稳定 history 时返回 None。
        """

        if not self._history_events:
            return None
        return self._history_events[-1].task_id

    def _filter_history_events(
        self,
        events: list[ServerMessageEvent],
    ) -> list[ServerMessageEvent]:
        """筛选需要进入业务历史表的稳定消息事件。

        参数:
            events: Runtime 投影后的前端业务事件。

        返回:
            可落入 message_projection 的稳定消息事件列表。
        """

        return [
            event
            for event in events
            if event.type in {
                ServerMessageEventType.INPUT,
                ServerMessageEventType.MESSAGE_FINAL,
                ServerMessageEventType.TOOL_CALL,
                ServerMessageEventType.TOOL_RESULT,
                ServerMessageEventType.CONTEXT_COMPRESSION_FINAL,
                ServerMessageEventType.INTERRUPTED,
                ServerMessageEventType.COMPLETE,
            }
        ]

    async def _emit(
        self,
        record: SseRecord,
        is_finish: bool = False,
    ) -> None:
        """写入 Redis Stream 并向外层响应队列发送 SSE 字符串。

        参数:
            record: 待发送的 SSE 记录。
            is_finish: 当前记录是否为 stream 的完成标记。

        返回:
            无返回值。
        """

        if not self._run_id:
            raise RuntimeError("SSE 事件写入失败：run_id 不能为空")

        async with self._emit_lock:
            # record.sse_id 必须在 Redis 写入前确定，避免依赖调用方提前拿号。
            record.sse_id = str(self._next_sequence())

            # 业务 chunk / heartbeat 必须先进入 Redis，再发送给当前 SSE 响应。
            await self._stream_store.add_record(self._run_id, record, is_finish=is_finish)

            # 请求取消只关闭当前响应通道，后台生成和 Redis 写入仍继续执行。
            if self._context.request_cancelled:
                return

            await self._queue.put(
                format_sse(
                    sse_id=record.sse_id,
                    event=record.event,
                    data=record.data,
                )
            )
