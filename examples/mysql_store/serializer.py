#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/14 13:01
# @Author  : YaHaoo
# @File    : serializer.py

"""examples MySQL 状态序列化边界定义。"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ValidationError

from turtlemap.kernel.exceptions import KernelRuntimeError
from turtlemap.kernel.models import SessionStateSaveKind

from turtlemap.os.models import SessionState
from turtlemap.shared.json_parser import dump_to_static_json
from .models import (
    AgentLongTermMemoryRecord,
    SessionStateRecord,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class StateSerializer:
    """表示 runtime model 与 persistence model 之间的转换器。

    说明:
        kernel 中需要持久化恢复的状态对象本身已经是 pydantic model，
        因此该类不再维护重复 schema，只负责拆分数据库表结构和处理少量
        持久化边界差异，例如业务持久化子类的 version/schema_version
        可走显式列，而 AgentState 运行现场直接保存在 state_json 中。
    """

    __slots__ = ()

    @staticmethod
    def build_session_state_record(
        session_state: SessionState,
        save_kind: SessionStateSaveKind,
    ) -> SessionStateRecord:
        """把运行时 SessionState 转换为 session_state 持久化记录。

        参数:
            session_state: 当前待保存的会话状态快照。
            save_kind: 本次保存动作的语义类型。

        返回:
            可写入 session_state 表的持久化记录。

        说明:
            `SessionState.agent_name2agent_state` 是 Runtime 恢复所需完整现场，
            直接随 SessionState JSON 保存。若业务子类存在 `version` 和
            `schema_version`，由持久化表字段承接，不写入 JSON。
        """

        state_json = dump_to_static_json(session_state)
        return SessionStateRecord(
            id=0,
            session_id=session_state.session_id,
            schema_version=getattr(session_state, "schema_version", 0),
            version=getattr(session_state, "version", 0),
            state_json=state_json,
            save_kind=save_kind.value,
        )

    @staticmethod
    def restore_session_state(record: SessionStateRecord) -> SessionState:
        """从 session_state 持久化记录恢复运行时 SessionState。

        参数:
            record: 从 session_state 表读取到的持久化记录。

        返回:
            已恢复的 SessionState；其中 `agent_name2agent_state` 已从 JSON 恢复，
            OSService 会继续按当前 root_agent 可达图做兼容校验。
        """

        session_state = StateSerializer._validate_model_json(SessionState, record.state_json)

        return session_state

    @staticmethod
    def build_agent_long_term_memory_record(
        uid: int,
        memory_text: str,
    ) -> AgentLongTermMemoryRecord:
        """构造长期记忆主数据持久化记录。

        参数:
            uid: 长期记忆所属用户唯一标识。
            memory_text: 当前生效的长期记忆文本。

        返回:
            可写入 agent_long_term_memory 表的记录。

        说明:
            长期记忆版本由 MySQL 自增列分配；这里使用 0 作为写入前占位值。
        """

        return AgentLongTermMemoryRecord(
            uid=uid,
            memory_text=memory_text,
            version=0,
        )

    @staticmethod
    def _validate_model_json(model_type: type[ModelT], raw_json: str) -> ModelT:
        """使用 runtime pydantic model 从 JSON 文本恢复对象。

        参数:
            model_type: 目标 runtime pydantic model 类型。
            raw_json: 当前待解析的 JSON 文本。

        返回:
            已完成校验的 runtime model 对象。
        """

        try:
            return model_type.model_validate_json(raw_json)
        except ValidationError as exc:
            raise KernelRuntimeError(f"{model_type.__name__} JSON 校验失败：{exc}") from exc
