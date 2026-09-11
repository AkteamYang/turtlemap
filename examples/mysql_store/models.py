#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/14 13:01
# @Author  : YaHaoo
# @File    : models.py

"""examples MySQL 状态持久化模型定义。

说明:
    本模块中的对象表示数据库或其他持久化介质使用的 persistence model，
    不直接复用 kernel runtime model。StateStore 实现负责在二者之间完成转换。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

JsonText = str


class StoreRecordModel(BaseModel):
    """表示 os/store 持久化记录的 pydantic 基类。

    说明:
        该基类统一 persistence model 的校验行为。当前允许按属性名构造，
        并禁止额外字段静默进入，避免数据库记录 schema 和代码模型不一致。
    """

    # 统一禁止额外字段，避免持久化结构被隐式污染。
    model_config = ConfigDict(extra="forbid")


class SessionStateRecord(StoreRecordModel):
    """表示 session_state 表的一条持久化记录。

    说明:
        该结构服务数据库落库，不等同于 kernel 的 `SessionState`。
        `state_json` 承载序列化后的会话快照内容。
    """

    # session_state 表自增主键，仅表示持久化记录身份。
    id: int

    # 会话唯一标识。
    session_id: str

    # SessionState 数据结构版本，用于判断快照结构是否兼容。
    schema_version: int

    # SessionState 乐观锁版本，用于表达运行态状态递进次数。
    version: int

    # 序列化后的 SessionState 快照内容。
    state_json: JsonText

    # 本次保存语义，例如 checkpoint / input_enqueue。
    save_kind: str

    # 创建时间。
    created_at: datetime | None = None


class SessionMetaRecord(StoreRecordModel):
    """表示 session_meta 表的一条会话元信息记录。

    说明:
        该模型服务 examples HTTP 接口展示会话列表。运行恢复仍以
        `session_state` 快照为准，元信息只承载标题、软删除和时间戳。
    """

    # 会话唯一标识。
    session_id: str

    # 会话展示标题。
    title: str

    # 是否已经软删除。
    deleted: bool = False

    # 创建时间。
    created_at: datetime | None = None

    # 更新时间。
    updated_at: datetime | None = None


class MessageProjectionRecord(StoreRecordModel):
    """表示 message_projection 表的一条按任务聚合的前端历史记录。

    说明:
        该表是 examples 服务端应用层 history 数据源，不直接复用 Runtime
        内部 `BaseAgentState.history`。
    """

    # 数据库自增主键，用于维护业务 history 的稳定展示顺序。
    id: int | None = None

    # 会话唯一标识。
    session_id: str

    # 当前任务 id；无法归属具体任务时为空。
    task_id: str | None = None

    # 分组首个 input 的前端幂等 id。
    client_event_id: str | None = None

    # 前端可复用展示事件列表的 JSON 字符串。
    content_json: JsonText

    # 创建时间。
    created_at: datetime | None = None


class MessageProjectionRunRecord(StoreRecordModel):
    """表示 message_projection_run 表的一条 run 写入记录。

    说明:
        该表用于控制每个 run_id 只允许把稳定消息写入业务 history 一次，
        防止恢复、重试或异常边界导致重复投影。
    """

    # 标识一次完整 agent 运行。
    run_id: str

    # 会话唯一标识。
    session_id: str

    # 创建时间。
    created_at: datetime | None = None


class AgentLongTermMemoryRecord(StoreRecordModel):
    """表示 agent_long_term_memory 表的一条持久化记录。

    说明:
        长期记忆是用户级跨会话主数据，不等同于 BaseAgentState 快照中的 memory 视图。
    """

    # 用户唯一标识。
    uid: int

    # 当前生效长期记忆文本。
    memory_text: str

    # 长期记忆版本号。
    version: int

    # 更新时间。
    updated_at: datetime | None = None
