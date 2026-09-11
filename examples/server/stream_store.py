#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/02 18:26
# @Author  : YaHaoo
# @File    : stream_store.py

"""示例服务端短期运行流缓存。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from examples.server.constants import REDIS_EXPIRE_EXTEND, REDIS_STREAM_MAX_LEN, STREAM_STORE_ACTIVE_RUN_EXPIRE, STREAM_STORE_READ_BLOCK_TIME, STREAM_STORE_STREAM_FINISH_EXPIRE, STREAM_STORE_STREAM_RUNNING_EXPIRE
from turtlemap.shared.logger import Logger

from .schemas import SseEventType
from .time_utils import now_ms


class SseRecord(BaseModel):
    """表示一条可缓存和重放的 SSE 记录。"""

    # 对外发送的 SSE id。
    sse_id: str

    # SSE event 名称。
    event: SseEventType

    # 已序列化的 data 字符串。
    data: str


class ActiveRunInfo(BaseModel):
    """表示当前会话中一个活动 run 的索引信息。"""

    # 标识一次完整 agent 运行。
    run_id: str

    # 前端幂等 id。
    client_event_id: str

    # run 开始时间。
    start_ts_ms: int


class RedisRunStreamStore:
    """基于 Redis Hash 和 Stream 的短期运行流缓存。

    约束:
        本类只保存未完成 run 的短期事件流，稳定历史仍以 MySQL SessionState 为准。
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """初始化 Redis 运行流缓存。

        参数:
            host: Redis 地址。
            port: Redis 端口。
            username: Redis ACL 用户名；未启用 ACL 时为空。
            password: Redis 密码；未配置密码时为空。
        """

        self.host = host
        self.port = port
        self.redis = Redis(
            host=host,
            port=port,
            username=username,
            password=password,
            decode_responses=True,
        )

    async def register_active_run(
        self,
        session_id: str,
        run_id: str,
        client_event_id: str,
    ) -> None:
        """记录一个正在运行的 completion。

        参数:
            session_id: 当前会话 id。
            run_id: 当前 agent 运行 id。
            client_event_id: 前端幂等 id。

        返回:
            无返回值。
        """
        name = self._active_run_name(session_id)
        data_obj = ActiveRunInfo(
            run_id=run_id,
            client_event_id=client_event_id,
            start_ts_ms=now_ms(),
        )
        await self.redis.hset(name=name, key=run_id, value=data_obj.model_dump_json())
        await self.redis.expire(name, STREAM_STORE_ACTIVE_RUN_EXPIRE)

    async def find_run_by_client_event_id(
        self,
        session_id: str,
        client_event_id: str,
    ) -> ActiveRunInfo | None:
        """按 `client_event_id` 查找同一会话中的活动 run。

        参数:
            session_id: 当前会话 id。
            client_event_id: 前端幂等 id。

        返回:
            命中的活动 run；不存在时返回 `None`。
        """
        name = self._active_run_name(session_id)
        run_id2RunInfoStr = await self.redis.hgetall(name)
        run_id2RunInfo = {
            k: ActiveRunInfo.model_validate_json(v) for k, v in run_id2RunInfoStr.items()
        }
        return next((v for v in run_id2RunInfo.values() if v.client_event_id == client_event_id), None)

    async def finish_run(self, session_id: str, run_id: str) -> None:
        """清理已正常结束的活动 run。

        参数:
            session_id: 当前会话 id。
            run_id: 当前 agent 运行 id。

        返回:
            无返回值。
        """
        name = self._active_run_name(session_id)
        await self.redis.hdel(name, run_id)

    async def add_record(self, run_id: str, record: SseRecord, is_finish: bool = False) -> None:
        """向指定 run 的 stream 追加一条 SSE 记录。

        参数:
            run_id: 当前 agent 运行 id。
            record: 已序列化的 SSE 记录。

        返回:
            无返回值。
        """
        name = self._run_stream(run_id)
        await self.redis.xadd(
            name=name,
            fields=cast(Any, record.model_dump(mode="json")),
            id=record.sse_id,
            maxlen=REDIS_STREAM_MAX_LEN,
        )

        # 完成后的超时更短
        if is_finish:
            expire = STREAM_STORE_STREAM_FINISH_EXPIRE
        else:
            expire = STREAM_STORE_STREAM_RUNNING_EXPIRE
        await self.redis.expire(name, expire)

    async def has_stream(self, run_id: str) -> bool:
        """判断指定 run 是否存在可恢复 stream。

        参数:
            run_id: 当前 agent 运行 id。

        返回:
            若存在 stream 记录则返回 True。
        """
        name = self._run_stream(run_id)
        any_list = await self.redis.xrange(name=name, count=1)
        await self.redis.expire(name=name, time=REDIS_EXPIRE_EXTEND, gt=True)
        return True if any_list else False

    async def read_range(
        self,
        run_id: str,
        last_event_id: str | None,
        limit: int,
        contain_last_event_id: bool = False
    ) -> list[SseRecord]:
        """读取指定 SSE id 范围的 stream 记录。

        参数:
            run_id: 当前 agent 运行 id。
            last_event_id: 最后一条消息 id，读取结果不包含它；None 时从第一条返回。
            limit: 最大读取条数。

        返回:
            可补发给前端的 SSE 记录列表。
        """
        name = self._run_stream(run_id)
        if last_event_id is None:
            min_id = "-"
        else:
            min_id = f"{last_event_id}" if contain_last_event_id else f"({last_event_id}"
        data_list = await self.redis.xrange(name=name, min=min_id, count=limit)
        if not data_list:
            return []

        await self.redis.expire(name=name, time=REDIS_EXPIRE_EXTEND, gt=True)
        return [SseRecord.model_validate(fields) for _, fields in data_list]

    async def read(self, run_id: str, last_event_id: str) -> list[SseRecord]:
        """阻塞读取指定 run 中 last_event_id 之后的新事件。

        参数:
            run_id: 当前 agent 运行 id。
            last_event_id: 客户端或服务端已消费的最后一条 SSE id。

        返回:
            读取到的 SSE 记录列表；超时或 stream 不存在时返回空列表。

        约束:
            Redis XREAD 本身按 id 读取后续消息，不包含 last_event_id 对应记录。
        """

        name = self._run_stream(run_id)
        start = time.perf_counter()
        try:
            # redis-py 在部分阻塞读场景下可能不按 XREAD BLOCK 时间正常空返回，
            # 这里仅对本次阻塞读加应用层兜底，不修改 Redis client 的全局超时配置。
            data = await asyncio.wait_for(
                self.redis.xread(
                    streams={
                        name: last_event_id,
                    },
                    block=STREAM_STORE_READ_BLOCK_TIME,
                ),
                timeout=STREAM_STORE_READ_BLOCK_TIME/1000,
            )
        except (asyncio.TimeoutError, RedisTimeoutError):
            Logger.logger.warning(
                f"Redis Stream 超时，run_id={run_id}, "
                f"last_event_id={last_event_id}, block_ms={STREAM_STORE_READ_BLOCK_TIME}, "
                f"elapsed_ms={int((time.perf_counter() - start) * 1000)}"
            )
            data = []
        if not data:
            return []

        await self.redis.expire(name=name, time=REDIS_EXPIRE_EXTEND, gt=True)
        stream_items = cast(list[tuple[str, list[tuple[str, dict[str, Any]]]]], data)
        _, stream_records = stream_items[0]
        return [SseRecord.model_validate(fields) for _, fields in stream_records]

    def _active_run_name(self, session_id: str) -> str:
        """构造当前会话活动 run 索引 key。

        参数:
            session_id: 当前会话 id。

        返回:
            Redis Hash key。
        """

        return f"sse:active_run_ids:{session_id}"

    def _run_stream(self, run_id: str) -> str:
        """构造当前 run 对应的 Redis Stream key。

        参数:
            run_id: 标识一次完整 agent 运行。

        返回:
            Redis Stream key。
        """

        return f"sse:run_stream:{run_id}"
