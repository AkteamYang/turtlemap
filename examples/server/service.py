#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/02 18:26
# @Author  : YaHaoo
# @File    : service.py

"""示例服务端应用服务。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from turtlemap import TurtleMapConfig
from turtlemap.kernel.models import (
    EventSource,
    EventType,
    InterruptionResponsePayload,
    ObservableEvent,
    SessionStateSaveKind,
    UserInputPayload,
)
from turtlemap.os.event_bus import EventBus
from turtlemap.os.models import SessionState
from turtlemap.os.runtime import Runtime
from turtlemap.os.service import OSService
from turtlemap.shared.ids import (
    PREFIX_OBSERVABLE_EVENT_ID,
    PREFIX_SESSION_ID,
    generate_prefixed_id,
)
from turtlemap.shared.logger import Logger
from turtlemap.shared.typing import ensure_instance

from examples.mysql_store import MessageProjectionRecord, MySQLStateStore, SessionMetaRecord

from .constants import (
    SCHEMA_VERSION,
    SSE_TIMEOUT,
    STREAM_STORE_READ_LIMIT,
)
from .runtime_event_handler import RuntimeEventHandler
from .schemas import (
    ApiCode,
    ApiResponse,
    AppInfoResponse,
    CompletionRequest,
    InputEventData,
    ServerMessageEvent,
    ServerMessageEventType,
    SessionInfoResponse,
    SessionResponse,
    SseEventType,
    UserInfoResponse,
)
from .sse import format_sse, response_json, success_response
from .stream_context import CompletionStreamContext
from .stream_store import RedisRunStreamStore, SseRecord
from .time_utils import datetime_to_ms
from .agent import build_agent

DEFAULT_SESSION_TITLE = "新的会话"


class ServerAppService:
    """封装示例服务端的会话与 completion 用例。

    字段:
        _config: TurtleMap 运行配置。
        _stream_store: 短期 SSE 流缓存，当前以内存实现模拟 Redis Stream。
        _state_store: MySQL Runtime 状态存储。

    约束:
        本类负责应用编排，不直接定义 FastAPI 路由。
    """

    def __init__(
        self,
        config: TurtleMapConfig,
        stream_store: RedisRunStreamStore,
    ) -> None:
        """初始化应用服务。

        参数:
            config: TurtleMap 运行配置。
            stream_store: 短期 SSE 流缓存。
        """

        # TurtleMap 运行配置，用于构造每次请求独立的 Agent。
        self._config = config

        # 短期 SSE 流缓存，后续可替换为真实 Redis Stream 实现。
        self._stream_store = stream_store

        # MySQL StateStore 作为服务级持久化依赖复用，避免请求内重复构造。
        self._state_store = MySQLStateStore()

    async def create_session(self, title: str) -> ApiResponse:
        """创建一个新的会话。

        参数:
            title: 会话展示标题。

        返回:
            包含新会话 id 和标题的统一响应。
        """

        session_id = generate_prefixed_id(PREFIX_SESSION_ID)
        safe_title = title.strip() or DEFAULT_SESSION_TITLE
        record = await self._repository().create_session_meta(
            session_id=session_id,
            title=safe_title,
        )
        session_state = await self._state_store.load_session_state(
            session_id=session_id,
            schema_version=SCHEMA_VERSION,
        )
        await self._state_store.save_session_state(
            session_state,
            save_kind=SessionStateSaveKind.CHECKPOINT,
        )
        return success_response(self._build_session_response(record).model_dump())

    async def get_info(self, limit: int = 20) -> ApiResponse:
        """查询页面初始化所需的基础信息。

        参数:
            limit: 最大返回会话条数。

        返回:
            包含写死用户信息、最近活动会话和会话列表的统一响应。
        """

        safe_limit = min(max(limit, 1), 100)
        records = await self._repository().list_session_meta_by_created_at(limit=safe_limit)
        active_record = await self._load_active_session_meta()

        # active_session 以 session_state 为准，sessions 按创建时间稳定展示。
        data = AppInfoResponse(
            user_info=UserInfoResponse(
                avatar="https://api.dicebear.com/9.x/personas/svg?seed=turtlemap",
                name="YaHaoo",
            ),
            session_info=SessionInfoResponse(
                active_session=(
                    self._build_session_response(active_record)
                    if active_record is not None
                    else None
                ),
                sessions=[
                    self._build_session_response(record)
                    for record in records
                ],
            ),
        )
        return success_response(data.model_dump())

    async def delete_session(self, session_id: str) -> ApiResponse:
        """软删除指定会话。

        参数:
            session_id: 当前会话 id。

        返回:
            统一空响应。
        """

        await self._repository().soft_delete_session_meta(session_id=session_id)
        return success_response(None)

    async def get_history(self, session_id: str) -> ApiResponse:
        """查询指定会话的稳定展示历史。

        参数:
            session_id: 当前会话 id。

        返回:
            包含历史消息列表的统一响应。
        """

        if not await self._session_exists(session_id):
            return ApiResponse(
                code=ApiCode.SESSION_NOT_FOUND,
                message="会话不存在",
                success=False,
                data=None,
            )

        records = await self._repository().list_message_projection_records(
            session_id=session_id,
            limit=100,
        )
        history = [
            self._build_history_message(record)
            for record in records
        ]
        return success_response(history)

    async def stream_completion(
        self,
        session_id: str,
        request: CompletionRequest,
    ) -> AsyncIterator[str]:
        """生成 completion SSE 流。

        参数:
            session_id: 当前会话 id。
            request: completion 请求体，支持用户输入或中断恢复响应。

        返回:
            SSE 字符串异步迭代器。
        """

        context = CompletionStreamContext(
            session_id=session_id,
            event_type=request.event_type,
            query=request.query,
            interruption_response=request.interruption_response,
            last_event_id=request.last_event_id,
            client_event_id=request.client_event_id,
            page_resume=False,
        )
        async for item in self._stream_response(context):
            yield item

    async def stream_resume_completion(self, session_id: str) -> AsyncIterator[str]:
        """生成页面首次加载恢复 SSE 流。

        参数:
            session_id: 当前会话 id。

        返回:
            SSE 字符串异步迭代器。
        """

        context = CompletionStreamContext(
            session_id=session_id,
            event_type=None,
            query=None,
            interruption_response=None,
            last_event_id=None,
            client_event_id=None,
            page_resume=True,
        )
        async for item in self._stream_response(context):
            yield item

    async def _stream_response(
        self,
        context: CompletionStreamContext,
    ) -> AsyncIterator[str]:
        """启动后台执行任务并消费 SSE 队列。

        参数:
            context: 当前 completion 请求上下文。

        返回:
            SSE 字符串异步迭代器。
        """

        queue: asyncio.Queue[str] = asyncio.Queue()

        # 后台任务承载真实生成流程，SSE generator 只负责消费响应队列。
        task = asyncio.create_task(self._execute_stream_context(context, queue))
        try:
            while True:
                # Task done 表示生产者协程代码已终止；队列也空时不会再有响应可发送。
                if task.done() and queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=SSE_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    continue
                yield item

            await task
        finally:
            if not task.done():
                # 客户端断开只停止当前 SSE 输出，生成任务继续跑完并写入短期 stream。
                context.request_cancelled = True

    async def _execute_stream_context(
        self,
        context: CompletionStreamContext,
        queue: asyncio.Queue[str],
    ) -> None:
        """执行 completion 请求并把所有 SSE 输出写入队列。

        参数:
            context: 当前 completion 请求上下文。
            queue: 接口层消费的 SSE 字符串队列。

        返回:
            无返回值。
        """

        try:
            if not await self._session_exists(context.session_id):
                await self._put_error(context, queue, ApiCode.SESSION_NOT_FOUND, "会话不存在")
                return

            session_state = await self._load_session_state(context.session_id)

            # resume 接口没有入参，恢复追加或 replay 由服务端运行时状态决定。
            if context.page_resume:
                await self._execute_page_resume(
                    context=context,
                    session_state=session_state,
                    queue=queue,
                )
                return

            # 普通 completion 生成调用，场景：
            # - 正常生成，不带 last_event_id
            # - 断流接续，带 last_event_id
            await self._execute_completion(
                context=context,
                session_state=session_state,
                queue=queue,
            )
        except Exception as error:
            Logger.logger.exception(f"completion SSE 执行异常")
            await self._put_error(context, queue, ApiCode.RUNTIME_ERROR, "Runtime 执行失败")

    async def _execute_page_resume(
        self,
        context: CompletionStreamContext,
        session_state: SessionState,
        queue: asyncio.Queue[str],
    ) -> None:
        """执行页面首次加载恢复逻辑。

        参数:
            context: 当前 completion 请求上下文。
            session_state: 当前会话状态。
            queue: 接口层消费的 SSE 字符串队列。

        返回:
            无返回值。
        """

        # 页面首次加载优先从当前未完成 run 的 stream 全量补发，前端保留内容追加。
        # 这里只代表返回了缓存，但是不一定成功，比如流没写完进程就挂了，此时给到客户端失败状态
        if session_state.run_id \
            and await self._stream_store.has_stream(session_state.run_id) \
            and await self._put_cached_records(
                context=context,
                queue=queue,
                session_state=session_state,
                run_id=session_state.run_id,
                last_event_id=None,  # 从第一个开始返回
            ):
            return

        # 没有可恢复 stream ，重走一遍 Runtime。
        await self._run_and_stream_events(
            session_state=session_state,
            input_events=[],
            start_code=ApiCode.OK,
            start_message="ok",
            context=context,
            queue=queue,
        )

    async def _execute_completion(
        self,
        context: CompletionStreamContext,
        session_state: SessionState,
        queue: asyncio.Queue[str],
    ) -> None:
        """执行普通 completion 或中断续接逻辑。

        参数:
            context: 当前 completion 请求上下文。
            session_state: 当前会话状态。
            queue: 接口层消费的 SSE 字符串队列。

        返回:
            无返回值。
        """
        if context.event_type not in {
            EventType.USER_INPUT,
            EventType.INTERRUPTION_RESPONSE,
        }:
            await self._put_error(context, queue, ApiCode.INVALID_ARGUMENT, "不支持的输入类型")
            return

        client_event_id = context.client_event_id or ""
        if context.event_type == EventType.USER_INPUT and not (context.query or "").strip():
            await self._put_error(context, queue, ApiCode.INVALID_ARGUMENT, "query 不能为空")
            return

        if not client_event_id:
            await self._put_error(
                context,
                queue,
                ApiCode.INVALID_ARGUMENT,
                "client_event_id 不能为空",
            )
            return

        # 如果是活动中的运行实例，表明用户是重复输入。
        active_run = await self._stream_store.find_run_by_client_event_id(
            session_id=context.session_id,
            client_event_id=client_event_id,
        )
        if active_run is not None:
            # 只有 Redis Stream 可读且能补发到 complete，才认为原运行链路仍然健康。
            if await self._stream_store.has_stream(active_run.run_id):
                if await self._put_cached_records(
                    context=context,
                    queue=queue,
                    session_state=session_state,
                    run_id=active_run.run_id,
                    last_event_id=context.last_event_id,
                ):
                    return

        # stream 不存在或者不可用时，检查history
        # 请求已经写入稳定历史时不重复生成，让前端刷新 history 对齐最终态。
        if await self._repository().exists_message_projection_client_event_id(
            session_id=context.session_id,
            client_event_id=client_event_id,
        ):
            await self._put_start(
                context=context,
                queue=queue,
                code=ApiCode.RELOAD_PAGE,
                message="reload_page",
                session_id=context.session_id,
                run_id="",
            )
            await self._put_end(context, queue)
            return

        # 没有缓存和历史的接续生成场景一般是生成中redis和chat服务都重启了
        # 此时应当重新刷新页面，走resume恢复流程
        if context.last_event_id is not None:
            await self._put_start(
                context=context,
                queue=queue,
                code=ApiCode.RELOAD_PAGE,
                message="reload_page",
                session_id=context.session_id,
                run_id="",
            )
            await self._put_end(context, queue)
            return

        # 走到这里是新请求，client_event_id 透传到 source_id 作为幂等标识。
        input_event = self._build_completion_input_event(context)
        await self._run_and_stream_events(
            session_state=session_state,
            input_events=[input_event],
            start_code=ApiCode.OK,
            start_message="ok",
            context=context,
            queue=queue,
        )

    async def _put_cached_records(
        self,
        context: CompletionStreamContext,
        queue: asyncio.Queue[str],
        session_state: SessionState,
        run_id: str,
        last_event_id: str | None,
    ) -> bool:
        """按 last_event_id 补发缓存中的 SSE 事件。

        参数:
            context: 当前 completion 请求上下文。
            queue: 接口层消费的 SSE 字符串队列。
            session_state: 当前会话 session_state。
            run_id: 当前 agent 运行 id。
            last_event_id: 客户端已收到的最后一个 SSE id；为空时从头补发。

        返回:
            成功补发到 complete 时返回 True；stream 不可用或超时时返回 False。
        """
        awaiting_send = await self._stream_store.read_range(run_id, last_event_id, STREAM_STORE_READ_LIMIT)
        records: list[SseRecord] = list(awaiting_send)
        if not records:
            return False

        # Redis Stream 续接只补发缓存事件，前端保持默认追加展示即可。
        await self._put_start(
            context=context,
            queue=queue,
            code=ApiCode.OK,
            message="ok",
            session_id=session_state.session_id,
            run_id=run_id,
        )

        # 发送redis队列缓存，先从stream缓存中读取，再block等待
        did_read_all_cache = False
        all_finished = False
        while True:

            # 发送sse
            for record in awaiting_send:
                if record.event == SseEventType.CHUNK:
                    record.event = SseEventType.STREAM_CHUNK
                await self._put_sse(
                    context=context,
                    queue=queue,
                    record=record,
                )

            # 判断是否已经结束
            last_one = records[-1]
            if last_one.event in [SseEventType.STREAM_CHUNK, SseEventType.CHUNK]:
                event = ServerMessageEvent.model_validate_json(last_one.data)
                if event.type == ServerMessageEventType.COMPLETE:
                    all_finished = True
                    break

            # 读取数据，先读缓存后block读取
            awaiting_send = []
            if not did_read_all_cache:
                awaiting_send = await self._stream_store.read_range(run_id, last_one.sse_id, STREAM_STORE_READ_LIMIT)
                if not awaiting_send:
                    did_read_all_cache = True
            else:
                awaiting_send = await self._stream_store.read(run_id=run_id, last_event_id=last_one.sse_id)

                # 超时了
                if not awaiting_send:
                    break
            
            records.extend(awaiting_send)

        # 结束事件
        if all_finished:
            await self._put_end(context, queue)
        else:

            # 流不完整，大概率是生成服务挂掉后的残留，此时更新run_id避开无效缓存
            await OSService.checkpoint_for_new_run_id(session_state, self._state_store)
            await self._put_error(context, queue, ApiCode.RELOAD_PAGE, "Redis stream 不完整")
        return True

    async def _put_sse(
        self,
        context: CompletionStreamContext,
        queue: asyncio.Queue[str],
        record: SseRecord,
    ) -> None:
        """向响应队列写入 SSE 记录，已取消请求不再写入。

        参数:
            context: 当前 completion 请求上下文。
            queue: 接口层消费的 SSE 字符串队列。
            record: 待发送的 SSE 记录。

        返回:
            无返回值。
        """

        # request_cancelled 是响应通道状态，不影响后台 Runtime 和短期 stream 写入。
        if context.request_cancelled:
            return
        await queue.put(
            format_sse(
                sse_id=record.sse_id,
                event=record.event,
                data=record.data,
            )
        )

    async def _run_and_stream_events(
        self,
        session_state: SessionState,
        input_events: list[ObservableEvent],
        start_code: ApiCode,
        start_message: str,
        context: CompletionStreamContext,
        queue: asyncio.Queue[str],
    ) -> None:
        """运行 Runtime 并把 RuntimeEvent 投影为 SSE。

        参数:
            session_state: 当前会话状态。
            input_events: 本轮输入事件；页面恢复重放时为空。
            start_code: start 控制事件 code。
            start_message: start 控制事件 message。
            context: 当前 completion 请求上下文。
            queue: 接口层消费的 SSE 字符串队列。

        返回:
            无返回值。
        """

        # 创建并初始化 agent runtime
        runtime = self._build_runtime()
        await runtime.init_session(session_state, resume=context.page_resume)

        # 事件处理器
        handler = RuntimeEventHandler(
            session_id=runtime.real_session_state.session_id,
            run_id=runtime.real_session_state.run_id,
            context=context,
            queue=queue,
            stream_store=self._stream_store,
        )
        await handler.init()
        listener_id = EventBus.subscribe(
            runtime.event_bus_id,
            handler.listen,
        )

        # 启动 Agent Runtime
        try:

            # 开始事件
            start_run_id = runtime.real_session_state.run_id
            await self._put_start(
                context=context,
                queue=queue,
                code=start_code,
                message=start_message,
                session_id=session_state.session_id,
                run_id=start_run_id,
            )

            # 执行
            await runtime.run(input_events=input_events)

            # 保存history
            history_is_empty = len(handler.history_events) <= 0
            complete_event = handler.history_add_complete_event()
            if not history_is_empty:
                await self._save_history_events(
                    session_id=session_state.session_id,
                    run_id=start_run_id,
                    events=handler.history_events,
                )

            # 收尾注意顺序：
            # 1、发送 completion chunk，标记 chunks 数据结束
            await handler.send_complete(complete_event)

            # 2、从活动实例中移除当前run_id
            if start_run_id and context.client_event_id:
                # Runtime.run 正常返回表示 checkpoint 已完成，此时再清理短期流缓存。
                await self._stream_store.finish_run(
                    session_id=session_state.session_id,
                    run_id=start_run_id,
                )

            # 3、发送 sse 完成消息
            await self._put_end(context, queue)
        finally:
            await handler.close()
            EventBus.unsubscribe(runtime.event_bus_id, listener_id)

    async def _put_start(
        self,
        context: CompletionStreamContext,
        queue: asyncio.Queue[str],
        code: ApiCode,
        message: str,
        session_id: str,
        run_id: str,
    ) -> None:
        """向 SSE 队列写入 start 事件。

        参数:
            context: 当前 completion 请求上下文。
            queue: 接口层消费的 SSE 字符串队列。
            code: SSE 控制操作码。
            message: 操作码文本。
            session_id: 当前会话 id。
            run_id: 当前 agent 运行 id。

        返回:
            无返回值。
        """

        record = SseRecord(
            sse_id="null",
            event=SseEventType.START,
            data=response_json(
                code=code,
                message=message,
                success=True,
                data={
                    "session_id": session_id,
                    "run_id": run_id,
                },
            ),
        )
        await self._put_sse(
            context=context,
            queue=queue,
            record=record,
        )

    async def _put_error(
        self,
        context: CompletionStreamContext,
        queue: asyncio.Queue[str],
        code: ApiCode,
        message: str,
    ) -> None:
        """向 SSE 队列写入 error 事件。

        参数:
            context: 当前 completion 请求上下文。
            queue: 接口层消费的 SSE 字符串队列。
            code: 错误码。
            message: 错误说明。

        返回:
            无返回值。
        """

        record = SseRecord(
            sse_id="null",
            event=SseEventType.ERROR,
            data=response_json(code, message, False, None),
        )
        await self._put_sse(
            context=context,
            queue=queue,
            record=record,
        )

    async def _put_end(
        self,
        context: CompletionStreamContext,
        queue: asyncio.Queue[str],
    ) -> None:
        """向 SSE 队列写入 end 事件。

        参数:
            context: 当前 completion 请求上下文。
            queue: 接口层消费的 SSE 字符串队列。

        返回:
            无返回值。
        """

        record = SseRecord(
            sse_id="null",
            event=SseEventType.END,
            data="{}",
        )
        await self._put_sse(
            context=context,
            queue=queue,
            record=record,
        )

    async def _session_exists(self, session_id: str) -> bool:
        """判断指定会话是否存在且未被删除。

        参数:
            session_id: 当前会话 id。

        返回:
            会话存在且未软删除时返回 True。
        """

        record = await self._repository().load_session_meta(session_id=session_id)
        return record is not None and not record.deleted

    async def _load_session_state(self, session_id: str) -> SessionState:
        """加载当前会话状态。

        参数:
            session_id: 当前会话 id。

        返回:
            MySQL 中最新的 SessionState。
        """

        return await self._state_store.load_session_state(
            session_id=session_id,
            schema_version=SCHEMA_VERSION,
        )

    def _build_runtime(self) -> Runtime:
        """构建一次请求使用的 Runtime。

        返回:
            已装配示例 Agent 和 MySQL StateStore 的 Runtime。
        """

        return Runtime(
            root_agent=build_agent(self._config),
            state_store=self._state_store,
        )

    def _build_completion_input_event(
        self,
        context: CompletionStreamContext,
    ) -> ObservableEvent:
        """构建 Runtime 可消费的 completion 输入事件。

        参数:
            context: 当前 completion 请求上下文。

        返回:
            根据输入类型构建的标准 ObservableEvent。

        异常:
            RuntimeError: 请求上下文未通过 completion 输入约束时抛出。
        """

        if context.event_type == EventType.USER_INPUT:
            client_event_id = context.client_event_id
            if not context.query or not client_event_id:
                raise RuntimeError("构建用户输入事件失败：query 或 client_event_id 为空")
            return ObservableEvent(
                event_id=generate_prefixed_id(PREFIX_OBSERVABLE_EVENT_ID),
                event_type=EventType.USER_INPUT,
                source=EventSource.USER,
                source_id=client_event_id,
                payload=UserInputPayload(content=context.query.strip()),
            )

        if context.event_type == EventType.INTERRUPTION_RESPONSE:
            client_event_id = context.client_event_id
            if not client_event_id:
                raise RuntimeError("构建中断恢复事件失败：client_event_id 为空")
            return ObservableEvent(
                event_id=generate_prefixed_id(PREFIX_OBSERVABLE_EVENT_ID),
                event_type=EventType.INTERRUPTION_RESPONSE,
                source=EventSource.USER,
                source_id=client_event_id,
                payload=ensure_instance(
                    context.interruption_response,
                    InterruptionResponsePayload,
                    "中断恢复响应",
                ),
            )

        raise RuntimeError(f"构建 completion 输入事件失败：不支持类型 {context.event_type}")

    def _build_session_response(self, record: SessionMetaRecord) -> SessionResponse:
        """构建会话响应模型。

        参数:
            record: MySQL 会话元信息记录。

        返回:
            API 会话响应数据。
        """

        return SessionResponse(
            session_id=record.session_id,
            title=record.title,
            created_at=datetime_to_ms(record.created_at),
            updated_at=datetime_to_ms(record.updated_at),
        )

    async def _load_active_session_meta(self) -> SessionMetaRecord | None:
        """从最新 SessionState 快照反查当前活动会话元信息。

        返回:
            当前活动会话元信息；没有状态或会话已删除时返回 None。
        """

        session_state_record = await self._repository().load_first_session_state(
            schema_version=SCHEMA_VERSION,
        )
        if session_state_record is None:
            return None

        # active_session 以 Runtime 状态为准，展示字段仍从业务会话表读取。
        active_record = await self._repository().load_session_meta(
            session_id=session_state_record.session_id,
        )
        if active_record is None or active_record.deleted:
            return None
        return active_record

    async def _save_history_events(
        self,
        session_id: str,
        run_id: str,
        events: list[ServerMessageEvent],
    ) -> None:
        """把稳定业务事件写入 message_projection 表。

        参数:
            session_id: 当前会话 id。
            run_id: 标识一次完整 agent 运行。
            events: 本轮 Runtime 产生的稳定消息事件。

        返回:
            无返回值。

        约束:
            该方法只保存前端 history 需要的业务读模型，不修改 Runtime state；
            每个 run_id 只能成功写入一次，避免重试或恢复路径重复投影。
        """

        if not run_id:
            Logger.logger.error(f"保存业务 history 失败，run_id 为空，session_id={session_id}")
            return

        records = self._build_message_projection_records(events)
        saved = await self._repository().save_message_projection_records_once(
            session_id=session_id,
            run_id=run_id,
            records=records,
        )
        if not saved:
            Logger.logger.info(f"业务 history 已写入过，跳过重复 run，run_id={run_id}")
            return

        await self._update_default_session_title_from_input(
            session_id=session_id,
            events=events,
        )

    def _build_message_projection_records(
        self,
        events: list[ServerMessageEvent],
    ) -> list[MessageProjectionRecord]:
        """按 task_id 把稳定业务事件转换为 history 持久化记录。

        参数:
            events: 本轮 Runtime 产生的稳定业务事件。

        返回:
            可保存的消息投影记录列表。每个有效 task_id 分组仅生成一条记录，
            `content_json` 保留从首个 input 开始的完整稳定展示事件。
        """

        records: list[MessageProjectionRecord] = []
        for grouped_events in self._group_history_events_by_task_id(events):
            input_event = grouped_events[0]
            input_data = ensure_instance(
                input_event.data,
                InputEventData,
                "input 业务事件 data",
            )
            records.append(
                MessageProjectionRecord(
                    session_id=input_event.session_id,
                    task_id=input_event.task_id,
                    client_event_id=input_data.client_event_id,
                    content_json=self._dump_history_events(grouped_events),
                )
            )
        return records

    def _group_history_events_by_task_id(
        self,
        events: list[ServerMessageEvent],
    ) -> list[list[ServerMessageEvent]]:
        """按 task_id 聚合以 input 开始的稳定 history 事件。

        参数:
            events: 按 Runtime 发生顺序排列的稳定业务事件。

        返回:
            按首个有效 input 出现顺序排列的任务事件分组。

        约束:
            每个 task_id 的第一条可保存事件必须是 input。无法归属 task_id，
            或在 input 之前到达的事件会被丢弃并记录 warning，避免持久化出
            无法恢复的半轮数据。
        """

        task_id2events: dict[str, list[ServerMessageEvent]] = {}
        grouped_events: list[list[ServerMessageEvent]] = []
        for event in events:
            task_id = event.task_id
            if not task_id:
                Logger.logger.warning(
                    f"跳过缺少 task_id 的 history 事件，event_id={event.event_id}, type={event.type.value}"
                )
                continue

            task_events = task_id2events.get(task_id)
            if task_events is None:
                if event.type != ServerMessageEventType.INPUT:
                    Logger.logger.warning(
                        f"跳过未以 input 开始的 task history 事件，task_id={task_id}, "
                        f"event_id={event.event_id}, type={event.type.value}"
                    )
                    continue

                task_events = [event]
                task_id2events[task_id] = task_events
                grouped_events.append(task_events)
                continue

            task_events.append(event)

        return grouped_events

    async def _update_default_session_title_from_input(
        self,
        session_id: str,
        events: list[ServerMessageEvent],
    ) -> None:
        """使用首个用户输入补全默认会话标题。

        参数:
            session_id: 当前会话 id。
            events: 本轮 Runtime 产生的稳定消息事件。

        返回:
            无返回值。

        约束:
            只在 session_meta.title 仍为默认值时更新，避免覆盖已命名会话。
        """

        for event in events:
            if event.type != ServerMessageEventType.INPUT:
                continue

            input_data = ensure_instance(
                event.data,
                InputEventData,
                "input 业务事件 data",
            )
            title = input_data.user_input
            if not title:
                return

            updated = await self._repository().update_default_session_title(
                session_id=session_id,
                default_title=DEFAULT_SESSION_TITLE,
                title=title,
            )
            if updated:
                Logger.logger.info(
                    f"默认会话标题已根据用户输入更新，session_id={session_id}, title={title}"
                )
            return

    def _dump_history_events(self, events: list[ServerMessageEvent]) -> str:
        """序列化 history 消息中的展示事件列表。

        参数:
            events: 需要保存到单条 message_projection.content_json 的事件列表。

        返回:
            JSON 字符串。
        """

        return json.dumps(
            [event.model_dump(mode="json") for event in events],
            ensure_ascii=False,
        )

    def _build_history_message(self, record: MessageProjectionRecord) -> dict[str, Any]:
        """把 message_projection 记录转换为 history 接口响应。

        参数:
            record: 单条业务历史消息记录。

        返回:
            前端 history 列表中的消息对象。
        """

        try:
            content = json.loads(record.content_json)
        except json.JSONDecodeError:
            Logger.logger.exception(
                f"history content_json 解析失败，id={record.id}"
            )
            content = []

        return {
            "message_id": str(record.id),
            "task_id": record.task_id,
            "client_event_id": record.client_event_id,
            "content": content,
            "created_at": datetime_to_ms(record.created_at),
        }

    def _repository(self):
        """获取 MySQL repository。

        返回:
            MySQLStateStoreRepository 实例。
        """

        return self._state_store.repository
