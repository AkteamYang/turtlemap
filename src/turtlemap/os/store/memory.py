#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/10 19:40
# @Author  : YaHaoo
# @File    : memory.py

"""turtlemap os 层内存态 StateStore 实现。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


from turtlemap.kernel.models import SessionStateSaveKind
from turtlemap.kernel.models import BaseSessionState
from turtlemap.os.exceptions import SessionStateChangedError
from turtlemap.shared.ids import build_executor_instance_id
from turtlemap.shared.typing import ensure_instance

from turtlemap.os.context.models import (
    ContextCompressionResult,
    ContextCompressionTaskRecord,
    ContextCompressionTaskStatus,
)
from ..models import SessionState
from turtlemap.os.protocols import StateStoreProtocol


@dataclass(slots=True)
class InMemoryStateStore(StateStoreProtocol):
    """表示 os 层默认内存态状态存储。

    说明:
        当前实现主要服务最小运行闭环与本地开发调试，所有状态仅保存在
        进程内存中，不提供跨进程持久化能力。MySQL 版本应放在同一
        `os.store` 包下，并使用独立 persistence model 与 serializer。
    """

    # 会话 id 到 SessionState 的映射。
    session_id2state: dict[str, SessionState] = field(default_factory=dict)

    # uid 到当前生效长期记忆文本的映射；当前等待用户身份模型重新接入。
    uid2long_term_memory: dict[int, str] = field(default_factory=dict)

    # 上下文压缩任务表的内存模拟，正式 MySQL 版本应独立建表保存。
    context_compression_tasks: list[ContextCompressionTaskRecord] = field(default_factory=list)

    async def load_session_state(
        self,
        session_id: str,
    ) -> SessionState:
        """加载或创建当前有效会话状态。

        参数:
            session_id: 待加载的会话 id。

        返回:
            若存在会话快照则返回会话状态对象，否则返回新建状态。
        """

        current_state = self.session_id2state.get(session_id)
        if current_state is not None:
            # 内存实现也模拟持久化快照边界，避免运行期修改污染已保存版本。
            return deepcopy(current_state)

        # 内存 store 作为 SDK 默认运行态存储，直接兜底创建最小 SessionState。
        return SessionState(session_id=session_id)

    async def save_session_state(
        self,
        session_state: BaseSessionState,
        save_kind: SessionStateSaveKind,
    ) -> None:
        """按指定保存语义保存新的会话状态快照。

        参数:
            session_state: 待保存的会话状态对象。
            save_kind: 当前保存动作对应的保存语义类型。

        返回:
            无返回值。

        说明:
            SDK 默认 SessionState 不携带乐观锁版本；若业务层传入的子类提供
            `version` 字段，内存 store 会按同名字段模拟乐观锁行为。
        """

        _ = save_kind
        os_session_state = ensure_instance(session_state, SessionState, "会话状态")
        current_state = self.session_id2state.get(os_session_state.session_id)
        current_version = getattr(current_state, "version", None)
        expected_version = getattr(os_session_state, "version", None)
        if current_state is not None and current_version != expected_version:
            raise SessionStateChangedError(
                "SessionState 已发生变更，"
                f"session_id={os_session_state.session_id}, "
                f"expected_version={expected_version}, "
                f"actual_version={current_version}"
            )

        # 业务子类若携带 version，则保存成功后推进版本；SDK 默认状态无需版本。
        if expected_version is not None:
            next_version = 1 if current_version is None else current_version + 1
            setattr(os_session_state, "version", next_version)
        snapshot = deepcopy(os_session_state)
        self.session_id2state[os_session_state.session_id] = snapshot

    async def load_agent_long_term_memory(
        self,
        session_state: BaseSessionState,
        agent_name: str,
    ) -> str:
        """加载当前 Agent 生效的长期记忆文本。

        参数:
            session_state: 当前会话状态；当前实现不再从会话状态读取用户维度。
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
            session_state: 当前会话状态，内存实现会从中读取 session_id。
            agent_name: 当前需要压缩治理的 Agent 名称。

        返回:
            若当前会话、分支和 Agent 没有 running 压缩任务，则返回新建任务；
            若已经存在 running 压缩任务且租约未过期，则返回 `None`；若已存在
            running 任务但租约已过期，则正式 store 应更新该任务并返回更新后记录。

        说明:
            当前内存实现只服务单进程开发调试，不提供真实跨进程原子性。
            正式存储实现应使用数据库事务或唯一约束保证检查、创建新任务和
            抢占过期任务不可分割。
        """

        session_state = ensure_instance(session_state, SessionState, "会话状态")
        session_id = session_state.session_id
        if session_id not in self.session_id2state:
            return None

        for compression_task in self.context_compression_tasks:
            is_same_session = compression_task.session_id == session_id
            is_same_agent = compression_task.agent_name == agent_name
            is_running = compression_task.status == ContextCompressionTaskStatus.RUNNING
            if is_same_session and is_same_agent and is_running:
                return None

        compression_task = ContextCompressionTaskRecord(
            id=len(self.context_compression_tasks) + 1,
            session_id=session_id,
            agent_name=agent_name,
            status=ContextCompressionTaskStatus.RUNNING,
            # lease_owner 标识本次创建任务的执行实例，完成时需要匹配它。
            lease_owner=build_executor_instance_id(),
        )
        self.context_compression_tasks.append(compression_task)
        return compression_task

    async def finish_context_compression_task(
        self, compression_task: ContextCompressionTaskRecord
    ) -> None:
        """将内存态上下文压缩任务标记为已完成。

        参数:
            compression_task: 当前需要结束的压缩任务记录。

        返回:
            无返回值；任务不存在时保持幂等，不额外报错。
        """

        for stored_task in self.context_compression_tasks:
            if stored_task.id != compression_task.id:
                continue
            if stored_task.lease_owner != compression_task.lease_owner:
                return

            stored_task.status = ContextCompressionTaskStatus.COMPLETED
            stored_task.lease_owner = None
            stored_task.lease_expires_at = None
            stored_task.error = None
            return

    async def save_context_compression_result(
        self,
        session_state: BaseSessionState,
        agent_name: str,
        compression_result: ContextCompressionResult,
    ) -> None:
        """读取最新内存会话状态并尝试保存压缩结果。

        参数:
            session_state: 调度压缩时使用的会话状态，用于读取 session_id。
            agent_name: 当前压缩结果所属 Agent 名称。
            compression_result: 当前后台压缩完成后的结果对象。

        返回:
            无返回值；若最新状态已无法安全合并，则跳过保存。
        """

        from turtlemap.os.context.compress import ContextCompressionProvider

        os_session_state = ensure_instance(session_state, SessionState, "会话状态")
        stored_session_state = self.session_id2state.get(os_session_state.session_id)
        if stored_session_state is None:
            return

        # 从最新快照复制后再合并，避免在乐观锁保存前污染已保存状态。
        latest_session_state = deepcopy(stored_session_state)
        owner_state = latest_session_state.agent_name2agent_state.get(agent_name)
        if owner_state is None:
            return

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
