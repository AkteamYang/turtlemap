#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 16:19
# @Author  : YaHaoo
# @File    : state_store.py

"""turtlemap MySQL StateStore 实现。"""

from __future__ import annotations

from aiomysql.pool import Pool
from pydantic import Field

from turtlemap.kernel.models import BaseSessionState, SessionStateSaveKind
from turtlemap.os.context.models import (
    ContextCompressionResult,
    ContextCompressionTaskRecord,
)
from turtlemap.os.models import SessionState
from turtlemap.os.protocols import StateStoreProtocol
from turtlemap.shared.logger import Logger
from turtlemap.shared.typing import ensure_instance

from .driver import get_mysql_pool
from .repository import MySQLStateStoreRepository
from .serializer import StateSerializer


class MySQLSessionState(SessionState):
    """表示 examples 业务层 MySQL 持久化使用的 SessionState 子类。

    说明:
        SDK 默认 SessionState 只承载运行现场；MySQL 示例额外维护乐观锁
        `version` 和数据结构 `schema_version`，用于持久化层安全更新。
    """

    # SessionState 乐观锁版本，用于判断保存时是否覆盖了其他执行流程。
    # @note 不做序列化，反序列化时从数据库字段读取
    version: int = Field(default=0, exclude=True)

    # SessionState 数据结构版本，用于判断快照结构是否兼容。
    # @note 不做序列化，反序列化时从数据库字段读取
    schema_version: int = Field(default=0, exclude=True)


class MySQLStateStore(StateStoreProtocol):
    """基于 MySQL 的 Runtime 状态存储实现。

    说明:
        本类承接 OSService 内部状态存储需求与 os 层后台压缩任务协议，
        负责把 Runtime 状态转换成 MySQL 持久化记录，并通过 repository 完成
        实际表操作。第一阶段保持与现有协议兼容，后续可再收敛为
        `save_checkpoint(...)` 事务化总入口。
    """

    __slots__ = ("repository",)

    def __init__(
        self,
        pool: Pool | None = None,
        *,
        repository: MySQLStateStoreRepository | None = None,
    ) -> None:
        """初始化 MySQLStateStore。

        参数:
            pool: 可选 aiomysql 连接池；为空时读取进程级全局连接池。
            repository: 可选表级数据访问对象，主要用于测试注入。

        返回:
            无返回值。
        """

        # Repository 只关心 MySQL 表读写，便于后续替换或测试注入。
        if repository is not None:
            self.repository = repository
            return

        effective_pool = pool or get_mysql_pool()
        self.repository = MySQLStateStoreRepository(effective_pool)

    async def load_session_state(
        self,
        session_id: str,
        schema_version: int = 0,
    ) -> MySQLSessionState:
        """加载或创建当前有效会话状态。

        参数:
            session_id: 待加载的会话 id。
            schema_version: 当前业务代码期望的 SessionState 数据结构版本。

        返回:
            若存在会话快照则返回恢复后的会话状态对象，否则返回新建状态。
        """

        record = await self.repository.load_latest_session_state(
            session_id=session_id,
            schema_version=schema_version,
        )
        if record is not None:
            session_state = MySQLSessionState.model_validate_json(record.state_json)
            session_state.version = record.version
            session_state.schema_version = record.schema_version
            return session_state

        # demo 业务层持久化入口负责首次会话兜底，Runtime 只消费外部传入状态。
        return MySQLSessionState(session_id=session_id, schema_version=schema_version)

    async def save_session_state(
        self,
        session_state: BaseSessionState,
        save_kind: SessionStateSaveKind,
    ) -> None:
        """按指定保存语义使用乐观锁保存当前会话状态。

        参数:
            session_state: 待保存的会话状态对象。
            save_kind: 当前保存动作对应的保存语义类型。

        返回:
            无返回值。
        """

        session_state = ensure_instance(session_state, MySQLSessionState, "MySQL 会话状态")

        # record.version 是调用方持有的预期版本，只有 Repository 保存成功后才回写新版本。
        record = StateSerializer.build_session_state_record(
            session_state=session_state,
            save_kind=save_kind,
        )
        session_state.version = await self.repository.save_session_state(record)

    async def load_agent_long_term_memory(
        self,
        session_state: BaseSessionState,
        agent_name: str,
    ) -> str:
        """加载当前 Agent 生效的长期记忆文本。

        参数:
            session_state: 当前 os 层会话状态；当前实现不再从会话状态读取用户维度。
            agent_name: 当前需要加载长期记忆的 Agent 名称。

        返回:
            当前长期记忆身份模型未接入，固定返回空字符串。
        """

        _ = (session_state, agent_name)

        # SessionState 不再携带 uid，长期记忆身份维度后续单独接入。
        return ""

    async def try_create_context_compression_task(
        self,
        session_state: BaseSessionState,
        agent_name: str,
    ) -> ContextCompressionTaskRecord | None:
        """尝试创建一个上下文压缩任务。

        参数:
            session_state: 当前会话状态，MySQL 实现会从中读取 session_id。
            agent_name: 当前需要压缩治理的 Agent 名称。

        返回:
            若当前会话、分支和 Agent 没有 running 压缩任务，则返回新建任务；
            若已经存在 running 任务且租约未过期，则返回 `None`；若租约已过期，
            则刷新租约并返回原任务。
        """

        session_state = ensure_instance(session_state, SessionState, "会话状态")
        return await self.repository.try_create_context_compression_task(
            session_id=session_state.session_id,
            agent_name=agent_name,
        )

    async def finish_context_compression_task(
        self, compression_task: ContextCompressionTaskRecord
    ) -> None:
        """将上下文压缩任务标记为已完成。

        参数:
            compression_task: 当前需要结束的压缩任务记录。

        返回:
            无返回值。
        """

        await self.repository.finish_context_compression_task(
            compression_task=compression_task
        )

    async def save_context_compression_result(
        self,
        session_state: BaseSessionState,
        agent_name: str,
        compression_result: ContextCompressionResult,
    ) -> None:
        """读取 MySQL 最新会话状态并尝试保存压缩结果。

        参数:
            session_state: 调度压缩时使用的会话状态，用于读取 session_id 和 schema_version。
            agent_name: 当前压缩结果所属 Agent 名称。
            compression_result: 当前后台压缩完成后的结果对象。

        返回:
            无返回值；若最新状态已无法安全合并，则跳过保存。
        """

        from turtlemap.os.context.compress import ContextCompressionProvider

        mysql_session_state = ensure_instance(
            session_state,
            MySQLSessionState,
            "MySQL 会话状态",
        )

        # 基于最新session_state保存一次，原来的session可能已经结束
        latest_session_state = await self.load_session_state(
            session_id=mysql_session_state.session_id,
            schema_version=mysql_session_state.schema_version,
        )
        owner_state = latest_session_state.agent_name2agent_state.get(agent_name)
        if owner_state is None:
            return

        # 后台压缩基于历史快照生成，只有最新状态仍匹配快照前缀时才允许写回。
        compression_result.merged = ContextCompressionProvider.merge_compacted_result(
            owner_agent_state=owner_state,
            compression_result=compression_result,
        )
        if not compression_result.merged:
            return

        await self.save_session_state(
            latest_session_state,
            save_kind=SessionStateSaveKind.BACKGROUND_CONTEXT_COMPRESSION,
        )
