#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 16:19
# @Author  : YaHaoo
# @File    : repository.py

"""turtlemap MySQL StateStore 表级数据访问实现。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncContextManager, AsyncIterator, TypeVar, cast

from aiomysql.connection import Connection
from aiomysql.cursors import DictCursor
from aiomysql.pool import Pool
from pydantic import BaseModel
from pymysql.err import IntegrityError

from turtlemap.os.context.models import (
    ContextCompressionTaskRecord,
    ContextCompressionTaskStatus,
)
from turtlemap.shared import exponential_backoff_retry
from turtlemap.shared.ids import build_executor_instance_id

from .models import (
    AgentLongTermMemoryRecord,
    MessageProjectionRecord,
    SessionMetaRecord,
    SessionStateRecord,
)

RecordT = TypeVar("RecordT", bound=BaseModel)


class MySQLStateStoreRepository:
    """封装 StateStore 相关 MySQL 表操作。

    说明:
        本类只负责 SQL 读写、版本分配和必要事务，不直接依赖 kernel runtime
        model。runtime model 与 persistence model 的转换由 `StateSerializer`
        在 repository 外部完成。
    """

    def __init__(self, pool: Pool) -> None:
        """初始化 MySQL StateStore Repository。

        参数:
            pool: aiomysql 连接池；每次操作从池中获取连接并自动归还。
        """

        self._pool = pool

    async def load_latest_session_state(
        self,
        session_id: str,
        schema_version: int = 0,
    ) -> SessionStateRecord | None:
        """加载指定会话的最新 SessionState 快照。

        参数:
            session_id: 待加载的会话 id。
            schema_version: 当前业务代码期望的 SessionState 数据结构版本。

        返回:
            最新会话快照记录；不存在时返回 `None`。
        """

        sql = """
        SELECT
            id,
            session_id,
            schema_version,
            version,
            state_json,
            save_kind,
            created_at
        FROM session_state
        WHERE session_id = %s
          AND schema_version = %s
        ORDER BY version DESC, id DESC
        LIMIT 1
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (session_id, schema_version))
                row = await cursor.fetchone()
        if row is None:
            return None
        
        return self._model_validate_row(SessionStateRecord, row)

    async def load_first_session_state(
        self,
        schema_version: int = 0,
    ) -> SessionStateRecord | None:
        """查询最新写入的 SessionState 快照。

        参数:
            schema_version: 当前业务代码期望的 SessionState 数据结构版本。

        返回:
            最新写入的会话快照记录；不存在时返回 `None`。
        """

        sql = """
        SELECT
            id,
            session_id,
            schema_version,
            version,
            state_json,
            save_kind,
            created_at
        FROM session_state
        WHERE schema_version = %s
        ORDER BY id DESC
        LIMIT 1
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (schema_version,))
                row = await cursor.fetchone()
        if row is None:
            return None

        return self._model_validate_row(SessionStateRecord, row)

    @exponential_backoff_retry(
        retry_exceptions=(IntegrityError,),
        max_attempts=3,
        initial_delay_seconds=0.05,
        max_delay_seconds=0.2,
    )
    async def save_session_state(
        self,
        record: SessionStateRecord,
    ) -> int:
        """使用乐观锁推进 SessionState，失败时追加写入最新版本。

        参数:
            record: 待写入的会话记录；version 表示调用方持有的当前版本。

        返回:
            保存成功后的新乐观锁版本。
        """

        next_version = record.version + 1
        update_sql = """
        UPDATE session_state
        SET
            version = %s,
            state_json = %s,
            save_kind = %s
        WHERE session_id = %s
          AND schema_version = %s
          AND version = %s
        """
        latest_version_sql = """
        SELECT version
        FROM session_state
        WHERE session_id = %s
          AND schema_version = %s
        ORDER BY version DESC, id DESC
        LIMIT 1
        """
        insert_sql = """
        INSERT INTO session_state (
            session_id,
            schema_version,
            version,
            state_json,
            save_kind
        ) VALUES (%s, %s, %s, %s, %s)
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                # 先按调用方持有的版本原子更新，避免检查与写入之间出现竞态。
                await cursor.execute(
                    update_sql,
                    (
                        next_version,
                        record.state_json,
                        record.save_kind,
                        record.session_id,
                        record.schema_version,
                        record.version,
                    ),
                )
                if cursor.rowcount == 1:
                    return next_version

                # update 失败说明当前版本不存在或已被推进，改按数据库最新版本追加。
                await cursor.execute(
                    latest_version_sql,
                    (
                        record.session_id,
                        record.schema_version,
                    ),
                )
                latest_row = await cursor.fetchone()
                latest_version = int(latest_row["version"]) if latest_row is not None else 0
                insert_version = latest_version + 1

                # 并发插入相同新版本时依赖唯一键触发 IntegrityError，再由 retry 重跑。
                await cursor.execute(
                    insert_sql,
                    (
                        record.session_id,
                        record.schema_version,
                        insert_version,
                        record.state_json,
                        record.save_kind,
                    ),
                )
                return insert_version

    async def create_session_meta(
        self,
        session_id: str,
        title: str,
    ) -> SessionMetaRecord:
        """创建或恢复一条会话元信息记录。

        参数:
            session_id: 当前会话唯一标识。
            title: 当前会话展示标题。

        返回:
            已创建或更新后的会话元信息记录。
        """

        sql = """
        INSERT INTO session_meta (
            session_id,
            title,
            deleted
        ) VALUES (%s, %s, 0)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            deleted = 0,
            updated_at = CURRENT_TIMESTAMP
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (session_id, title))

        record = await self.load_session_meta(session_id=session_id)
        if record is None:
            raise RuntimeError(f"创建会话元信息失败，session_id={session_id}")
        return record

    async def load_session_meta(
        self,
        session_id: str,
    ) -> SessionMetaRecord | None:
        """加载指定会话元信息。

        参数:
            session_id: 当前会话唯一标识。

        返回:
            会话元信息记录；不存在时返回 `None`。
        """

        sql = """
        SELECT
            session_id,
            title,
            deleted,
            created_at,
            updated_at
        FROM session_meta
        WHERE session_id = %s
        LIMIT 1
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (session_id,))
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._model_validate_row(SessionMetaRecord, row)

    async def update_default_session_title(
        self,
        session_id: str,
        default_title: str,
        title: str,
    ) -> bool:
        """仅当当前会话仍为默认标题时更新展示标题。

        参数:
            session_id: 当前会话唯一标识。
            default_title: 允许被替换的默认标题。
            title: 从用户输入提取出的新标题。

        返回:
            成功更新时返回 True；会话不存在、已删除或标题已变化时返回 False。
        """

        sql = """
        UPDATE session_meta
        SET
            title = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE session_id = %s
          AND title = %s
          AND deleted = 0
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (title, session_id, default_title))
                return cursor.rowcount == 1

    async def list_session_meta(
        self,
        limit: int = 20,
    ) -> list[SessionMetaRecord]:
        """按更新时间倒序查询未删除会话元信息。

        参数:
            limit: 最大返回条数，调用方应传入正整数。

        返回:
            会话元信息记录列表。
        """

        sql = """
        SELECT
            session_id,
            title,
            deleted,
            created_at,
            updated_at
        FROM session_meta
        WHERE deleted = 0
        ORDER BY updated_at DESC, created_at DESC
        LIMIT %s
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (limit,))
                rows = await cursor.fetchall()
        return [
            self._model_validate_row(SessionMetaRecord, row)
            for row in rows
        ]

    async def list_session_meta_by_created_at(
        self,
        limit: int = 20,
    ) -> list[SessionMetaRecord]:
        """按创建时间查询未删除会话元信息。

        参数:
            limit: 最大返回条数，调用方应传入正整数。

        返回:
            按创建时间降序排列的会话元信息记录列表，最新创建的会话在前。
        """

        sql = """
        SELECT
            session_id,
            title,
            deleted,
            created_at,
            updated_at
        FROM session_meta
        WHERE deleted = 0
        ORDER BY created_at DESC, session_id DESC
        LIMIT %s
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (limit,))
                rows = await cursor.fetchall()
        return [
            self._model_validate_row(SessionMetaRecord, row)
            for row in rows
        ]

    async def soft_delete_session_meta(
        self,
        session_id: str,
    ) -> None:
        """软删除指定会话元信息。

        参数:
            session_id: 当前会话唯一标识。

        返回:
            无返回值。
        """

        sql = """
        UPDATE session_meta
        SET
            deleted = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE session_id = %s
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (session_id,))

    async def save_message_projection_records_once(
        self,
        session_id: str,
        run_id: str,
        records: list[MessageProjectionRecord],
    ) -> bool:
        """按 run_id 幂等批量保存前端历史消息投影。

        参数:
            session_id: 当前会话 id。
            run_id: 标识一次完整 agent 运行。
            records: 待保存的业务历史消息记录；空列表时仍会写入 run 标记。

        返回:
            本次成功获得 run 写入权时返回 True；run 已写过时返回 False。

        约束:
            先写入 message_projection_run 抢占 run_id，再写入消息投影记录；
            两步处于同一个事务中，避免只写 run 标记或只写消息的半状态。
        """

        run_sql = """
        INSERT IGNORE INTO message_projection_run (
            run_id,
            session_id
        ) VALUES (%s, %s)
        """

        message_sql = """
        INSERT IGNORE INTO message_projection (
            session_id,
            task_id,
            client_event_id,
            content_json
        ) VALUES (%s, %s, %s, %s)
        """
        values = [
            (
                record.session_id,
                record.task_id,
                record.client_event_id,
                record.content_json,
            )
            for record in records
        ]
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(run_sql, (run_id, session_id))
                if cursor.rowcount != 1:
                    return False
                if values:
                    await cursor.executemany(message_sql, values)
                return True

    async def list_message_projection_records(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[MessageProjectionRecord]:
        """查询指定会话的业务历史消息。

        参数:
            session_id: 当前会话 id。
            limit: 最大返回条数。

        返回:
            按数据库自增 id 升序排列的历史消息记录列表。
        """

        sql = """
        SELECT
            id,
            session_id,
            task_id,
            client_event_id,
            content_json,
            created_at
        FROM message_projection
        WHERE session_id = %s
        ORDER BY id ASC
        LIMIT %s
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (session_id, limit))
                rows = await cursor.fetchall()
        return [
            self._model_validate_row(MessageProjectionRecord, row)
            for row in rows
        ]

    async def exists_message_projection_client_event_id(
        self,
        session_id: str,
        client_event_id: str,
    ) -> bool:
        """判断业务历史中是否已经存在指定 client_event_id。

        参数:
            session_id: 当前会话 id。
            client_event_id: 前端幂等 id。

        返回:
            存在时返回 True。
        """

        sql = """
        SELECT 1
        FROM message_projection
        WHERE session_id = %s
          AND client_event_id = %s
        LIMIT 1
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (session_id, client_event_id))
                row = await cursor.fetchone()
        return row is not None

    async def load_agent_long_term_memory(
        self,
        uid: int,
    ) -> AgentLongTermMemoryRecord | None:
        """加载指定用户当前生效长期记忆记录。

        参数:
            uid: 当前会话所属用户标识。

        返回:
            当前生效长期记忆记录；不存在时返回 `None`。
        """

        sql = """
        SELECT uid, memory_text, version, updated_at
        FROM agent_long_term_memory
        WHERE uid = %s
        ORDER BY version DESC
        LIMIT 1
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(sql, (uid,))
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._model_validate_row(AgentLongTermMemoryRecord, row)

    @exponential_backoff_retry(
        retry_exceptions=(IntegrityError,),
        max_attempts=3,
        initial_delay_seconds=0.05,
        max_delay_seconds=0.2,
    )
    async def try_create_context_compression_task(
        self,
        session_id: str,
        agent_name: str,
    ) -> ContextCompressionTaskRecord | None:
        """尝试原子创建上下文压缩任务记录。

        参数:
            session_id: 当前会话 id。
            agent_name: 当前 Agent 名称。

        返回:
            新建或抢占过期任务时返回任务记录；已有未过期 running 任务时返回 `None`。

        说明:
            并发创建同一会话、分支和 Agent 的压缩任务时，唯一约束会让其中
            一个事务失败。该方法会通过指数退避重跑完整流程，让重试请求重新
            查询已有任务并按租约语义返回。
        """

        running_sql = """
        SELECT
            id,
            (lease_expires_at IS NULL OR lease_expires_at > CURRENT_TIMESTAMP) AS is_lease_active
        FROM context_compression_task
        WHERE session_id = %s
          AND agent_name = %s
          AND status = %s
        LIMIT 1
        FOR UPDATE
        """
        update_sql = """
        UPDATE context_compression_task
        SET
            lease_owner = %s,
            lease_expires_at = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL 10 MINUTE),
            error = NULL
        WHERE id = %s
        """
        insert_sql = """
        INSERT INTO context_compression_task (
            session_id,
            agent_name,
            status,
            lease_owner,
            lease_expires_at
        ) VALUES (%s, %s, %s, %s, DATE_ADD(CURRENT_TIMESTAMP, INTERVAL 10 MINUTE))
        """
        task_id = 0
        lease_owner = build_executor_instance_id()
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(
                    running_sql,
                    (
                        session_id,
                        agent_name,
                        ContextCompressionTaskStatus.RUNNING.value,
                    ),
                )
                running_row = await cursor.fetchone()
                if running_row is not None:
                    task_id = int(running_row["id"])
                    if bool(running_row["is_lease_active"]):
                        return None

                    await cursor.execute(
                        update_sql,
                        (
                            lease_owner,
                            task_id,
                        ),
                    )
                    return ContextCompressionTaskRecord(
                        id=task_id,
                        session_id=session_id,
                        agent_name=agent_name,
                        status=ContextCompressionTaskStatus.RUNNING,
                        lease_owner=lease_owner,
                    )

                await cursor.execute(
                    insert_sql,
                    (
                        session_id,
                        agent_name,
                        ContextCompressionTaskStatus.RUNNING.value,
                        lease_owner,
                    ),
                )
                task_id = int(cursor.lastrowid or 0)

        return ContextCompressionTaskRecord(
            id=task_id,
            session_id=session_id,
            agent_name=agent_name,
            status=ContextCompressionTaskStatus.RUNNING,
            lease_owner=lease_owner,
        )

    async def finish_context_compression_task(
        self, compression_task: ContextCompressionTaskRecord
    ) -> None:
        """将上下文压缩任务标记为已完成并释放租约。

        参数:
            compression_task: 当前需要结束的压缩任务记录，必须匹配创建时的 lease_owner。

        返回:
            无返回值。
        """

        if compression_task.lease_owner is None:
            return

        sql = """
        UPDATE context_compression_task
        SET
            status = %s,
            lease_owner = NULL,
            lease_expires_at = NULL,
            error = NULL
        WHERE id = %s
          AND lease_owner = %s
          AND status = %s
        """
        async with self._get_connection() as connection:
            async with self._cursor(connection) as cursor:
                await cursor.execute(
                    sql,
                    (
                        ContextCompressionTaskStatus.COMPLETED.value,
                        compression_task.id,
                        compression_task.lease_owner,
                        ContextCompressionTaskStatus.RUNNING.value,
                    ),
                )

    @asynccontextmanager
    async def _get_connection(self) -> AsyncIterator[Connection]:
        """获取干净连接并统一管理事务边界。

        返回:
            已回滚残留事务状态的 aiomysql 连接；正常退出时提交，异常退出时回滚。
        """

        _connection: Connection | None = None
        try:
            async with self._pool.acquire() as connection:
                _connection = connection
                assert _connection
                await _connection.rollback()
                yield connection
                await _connection.commit()
        except BaseException:
            if _connection is not None:
                await _connection.rollback()
            raise

    def _model_validate_row(
        self,
        model_type: type[RecordT],
        row: dict[str, object],
    ) -> RecordT:
        """把数据库字典行转换为指定持久化模型。

        参数:
            model_type: 目标持久化模型类型。
            row: DictCursor 返回的数据库行。

        返回:
            已完成 pydantic 校验的持久化模型。
        """

        record_data = dict(row)
        return model_type.model_validate(record_data)

    def _cursor(self, connection: Connection) -> AsyncContextManager[DictCursor]:
        """创建返回字典行的 MySQL cursor。

        参数:
            connection: aiomysql 连接对象。

        返回:
            以字段名为 key 返回查询结果的 cursor 上下文管理器。
        """

        return cast(AsyncContextManager[DictCursor], connection.cursor(DictCursor))
