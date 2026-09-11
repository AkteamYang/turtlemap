#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 15:21
# @Author  : YaHaoo
# @File    : test_mysql_store.py

"""os.store MySQL 基础设施测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from turtlemap.kernel.models import (
    EventSource,
    EventType,
    Input,
    MessageRole,
    ObservableEvent,
    RuntimeArtifactType,
    SessionStateSaveKind,
    UserInputPayload,
)
from turtlemap.kernel.models import RuntimeArtifact, SystemDefinition
from turtlemap.os import AgentState, SessionState
from turtlemap.os.exceptions import SessionStateChangedError
from turtlemap.os.llm.model import LLMMessage
from turtlemap.os.service import OSService
from turtlemap.os.store import InMemoryStateStore
from turtlemap.os.context.models import ContextCompressionTaskRecord
from turtlemap.shared.logger import Logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

import mysql_store as mysql
from config import RedisConfig
from mysql_store import (
    AgentLongTermMemoryRecord,
    MysqlConfig,
    MySQLSessionState,
    SessionStateRecord,
    driver,
)


def test_mysql_config_can_load_env_file(tmp_path, monkeypatch) -> None:
    """验证业务层 MySQL 配置可以从 `.env` 文件加载。

    参数:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的环境变量隔离工具。

    返回:
        无返回值。
    """

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "TURTLEMAP_MYSQL_HOST=mysql.example",
                "TURTLEMAP_MYSQL_PORT=3307",
                "TURTLEMAP_MYSQL_USER=test-user",
                "TURTLEMAP_MYSQL_PASSWORD=test-password",
                "TURTLEMAP_MYSQL_DATABASE=test-db",
                "TURTLEMAP_MYSQL_CHARSET=utf8mb4",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("TURTLEMAP_MYSQL_HOST", raising=False)
    monkeypatch.delenv("TURTLEMAP_MYSQL_PORT", raising=False)
    monkeypatch.delenv("TURTLEMAP_MYSQL_USER", raising=False)
    monkeypatch.delenv("TURTLEMAP_MYSQL_PASSWORD", raising=False)
    monkeypatch.delenv("TURTLEMAP_MYSQL_DATABASE", raising=False)
    monkeypatch.delenv("TURTLEMAP_MYSQL_CHARSET", raising=False)

    config = MysqlConfig.from_env(env_path=env_path)

    assert config.host == "mysql.example"
    assert config.port == 3307
    assert config.user == "test-user"
    assert config.password == "test-password"
    assert config.database == "test-db"
    assert config.charset == "utf8mb4"


def test_mysql_config_prefers_real_environment_over_env_file(
    tmp_path, monkeypatch
) -> None:
    """验证业务层 MySQL 配置优先读取真实系统环境变量。

    参数:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的环境变量隔离工具。

    返回:
        无返回值。
    """

    env_path = tmp_path / ".env"
    env_path.write_text(
        "TURTLEMAP_MYSQL_HOST=file-host\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TURTLEMAP_MYSQL_HOST", "env-host")

    config = MysqlConfig.from_env(env_path=env_path)

    assert config.host == "env-host"


def test_mysql_config_reads_environment_when_env_file_missing(
    tmp_path, monkeypatch
) -> None:
    """验证 `.env` 文件不存在时仍会读取真实系统环境变量。

    参数:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的环境变量隔离工具。

    返回:
        无返回值。
    """

    missing_env_path = tmp_path / ".env.missing"
    monkeypatch.setenv("TURTLEMAP_MYSQL_HOST", "env-only-host")

    config = MysqlConfig.from_env(env_path=missing_env_path)

    assert config.host == "env-only-host"


def test_mysql_config_can_be_built_by_field_names() -> None:
    """验证业务代码可以直接按字段名构造 MySQL 配置。

    返回:
        无返回值。
    """

    config = MysqlConfig(
        host="127.0.0.2",
        port=3310,
        user="field-user",
        password="field-password",
        database="field-db",
    )

    assert config.host == "127.0.0.2"
    assert config.port == 3310
    assert config.user == "field-user"
    assert config.password == "field-password"
    assert config.database == "field-db"
    assert config.charset == "utf8mb4"


def test_redis_config_can_load_env_file(tmp_path, monkeypatch) -> None:
    """验证业务层 Redis 配置可以从 `.env` 文件加载。

    参数:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的环境变量隔离工具。

    返回:
        无返回值。
    """

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "TURTLEMAP_REDIS_HOST=redis.example",
                "TURTLEMAP_REDIS_PORT=6380",
                "TURTLEMAP_REDIS_USERNAME=default",
                "TURTLEMAP_REDIS_PASSWORD=123456&*()_+",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("TURTLEMAP_REDIS_HOST", raising=False)
    monkeypatch.delenv("TURTLEMAP_REDIS_PORT", raising=False)
    monkeypatch.delenv("TURTLEMAP_REDIS_USERNAME", raising=False)
    monkeypatch.delenv("TURTLEMAP_REDIS_PASSWORD", raising=False)

    config = RedisConfig.from_env(env_path=env_path)

    assert config.host == "redis.example"
    assert config.port == 6380
    assert config.username == "default"
    assert config.password == "123456&*()_+"


def test_redis_config_reads_environment_when_env_file_missing(
    tmp_path, monkeypatch
) -> None:
    """验证 Redis `.env` 文件不存在时仍会读取真实系统环境变量。

    参数:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的环境变量隔离工具。

    返回:
        无返回值。
    """

    missing_env_path = tmp_path / ".env.missing"
    monkeypatch.setenv("TURTLEMAP_REDIS_HOST", "env-redis-host")

    config = RedisConfig.from_env(env_path=missing_env_path)

    assert config.host == "env-redis-host"


class _FakeLogger:
    """记录测试期间写入的日志消息。

    属性:
        info_messages: 通过 info 写入的消息列表。
        warning_messages: 通过 warning 写入的消息列表。
    """

    def __init__(self) -> None:
        """初始化空的日志消息容器。"""

        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str) -> None:
        """记录 info 级别日志消息。

        参数:
            message: 待记录的日志内容。

        返回:
            无返回值。
        """

        self.info_messages.append(message)

    def warning(self, message: str) -> None:
        """记录 warning 级别日志消息。

        参数:
            message: 待记录的日志内容。

        返回:
            无返回值。
        """

        self.warning_messages.append(message)


class _FakeStateStoreRepository:
    """记录 MySQLStateStore 写入意图的 fake repository。

    属性:
        session_record: 最近一次保存的会话记录。
    """

    def __init__(self) -> None:
        """初始化 fake repository 的记录槽位。"""

        self.session_record: SessionStateRecord | None = None

    async def load_latest_session_state(
        self,
        session_id: str,
    ) -> SessionStateRecord | None:
        """模拟加载最新会话状态。

        参数:
            session_id: 待加载的会话 id。

        返回:
            当前测试不预置会话状态，因此始终返回 `None`。
        """

        _ = session_id
        return None

    async def save_session_state(
        self,
        record: SessionStateRecord,
    ) -> int:
        """模拟 MySQL 保存会话快照。

        参数:
            record: 待保存的会话持久化记录。

        返回:
            写入的会话乐观锁版本号。
        """

        self.session_record = record
        return record.version + 1

    async def load_agent_long_term_memory(
        self,
        uid: int,
    ) -> AgentLongTermMemoryRecord | None:
        """模拟加载长期记忆主数据。

        参数:
            uid: 当前用户 id。

        返回:
            当前测试不预置长期记忆，因此返回 `None`。
        """

        _ = uid
        return None

    async def try_create_context_compression_task(
        self,
        session_state,
        agent_name: str,
    ) -> ContextCompressionTaskRecord | None:
        """模拟创建上下文压缩任务。

        参数:
            session_state: 当前会话状态。
            agent_name: Agent 名称。

        返回:
            当前测试不覆盖该路径，因此返回 `None`。
        """

        _ = (session_state, agent_name)
        return None

    async def finish_context_compression_task(
        self, compression_task: ContextCompressionTaskRecord
    ) -> None:
        """模拟结束上下文压缩任务。

        参数:
            compression_task: 当前需要结束的压缩任务记录。

        返回:
            无返回值。
        """

        _ = compression_task


def test_get_mysql_pool_raises_when_not_initialized(monkeypatch) -> None:
    """验证未初始化连接池时会抛出明确错误。"""

    monkeypatch.setattr(driver, "_mysql_pool", None)

    with pytest.raises(RuntimeError, match="MySQL 连接池尚未初始化"):
        mysql.get_mysql_pool()


def test_load_sql_statements_can_split_non_empty_statements(tmp_path) -> None:
    """验证 migration SQL 文件会按分号拆分并过滤空语句。"""

    migration_path = tmp_path / "001_init.sql"
    migration_path.write_text(
        "CREATE TABLE demo (id BIGINT);\n\n" "INSERT INTO demo(id) VALUES (1);\n" " ; ",
        encoding="utf-8",
    )

    sql_statements = mysql._load_sql_statements(migration_path)

    assert sql_statements == [
        "CREATE TABLE demo (id BIGINT)",
        "INSERT INTO demo(id) VALUES (1)",
    ]


def test_context_compression_task_migration_deduplicates_same_task_scope() -> None:
    """验证压缩任务表按会话和 Agent 建立唯一约束。

    返回:
        无返回值。
    """

    migration_path = Path("examples/mysql_store/migrations/001_init_state_store.sql")
    migration_sql = migration_path.read_text(encoding="utf-8")

    assert "UNIQUE KEY uk_context_compression_task" in migration_sql
    assert "session_id,\n        agent_name" in migration_sql
    assert "running_dedup_key" not in migration_sql

    # SessionState 使用会话、结构版本和乐观锁版本组成唯一键，允许保留版本行。
    assert (
        "UNIQUE KEY uk_session_state (session_id, schema_version, version)"
        in migration_sql
    )


def test_init_mysql_runs_pool_and_table_initialization(monkeypatch) -> None:
    """验证公开初始化入口会串联连接池和表结构初始化。

    参数:
        monkeypatch: pytest 提供的属性替换工具。

    返回:
        无返回值。
    """

    created_pool = object()
    init_calls: list[tuple[str, object]] = []

    async def fake_init_mysql_pool(**kwargs: object) -> object:
        """模拟连接池初始化并记录调用参数。

        参数:
            kwargs: 公开初始化入口透传的连接池配置参数。

        返回:
            fake MySQL 连接池对象。
        """

        init_calls.append(("pool", kwargs))
        return created_pool

    async def fake_init_mysql_tables(**kwargs: object) -> None:
        """模拟表结构初始化并记录调用参数。

        参数:
            kwargs: 公开初始化入口透传的 migration 参数。

        返回:
            无返回值。
        """

        init_calls.append(("tables", kwargs))

    monkeypatch.setattr(driver, "_init_mysql_pool", fake_init_mysql_pool)
    monkeypatch.setattr(driver, "_init_mysql_tables", fake_init_mysql_tables)

    pool = asyncio.run(mysql.init_mysql())

    assert pool is created_pool
    assert init_calls == [
        ("pool", {"mysql_config": None}),
        ("tables", {}),
    ]


def test_close_mysql_pool_reads_latest_shared_logger(monkeypatch) -> None:
    """验证 MySQL 模块不会缓存旧 logger。

    参数:
        monkeypatch: pytest 提供的属性替换工具。

    返回:
        无返回值。
    """

    fake_logger = _FakeLogger()
    monkeypatch.setattr(driver, "_mysql_pool", None)
    monkeypatch.setattr(Logger, "logger", fake_logger)

    asyncio.run(mysql.close_mysql_pool())

    assert fake_logger.info_messages == ["MySQL 连接池关闭跳过，当前连接池未初始化"]


def test_mysql_state_store_saves_os_session_state() -> None:
    """验证 MySQLStateStore 保存会话后会回写存储层分配的版本。"""

    repository = _FakeStateStoreRepository()
    state_store = mysql.MySQLStateStore(repository=repository)
    session_state = MySQLSessionState(
        session_id="session_1",
        agent_name2agent_state={
            "root_agent": AgentState(
                agent_name="root_agent",
                system=SystemDefinition(role="assistant", objective="help user"),
                history=[
                    RuntimeArtifact(
                        type=RuntimeArtifactType.INPUT,
                        payload=Input(
                            input_id="input_1",
                            events=[
                                ObservableEvent(
                                    event_id="event_1",
                                    event_type=EventType.USER_INPUT,
                                    source=EventSource.USER,
                                    payload=UserInputPayload(content="你好"),
                                )
                            ],
                        ),
                    ),
                ],
            )
        },
    )

    asyncio.run(
        state_store.save_session_state(
            session_state=session_state,
            save_kind=SessionStateSaveKind.CHECKPOINT,
        )
    )

    assert session_state.version == 1
    assert repository.session_record is not None
    # Repository 接收调用方持有的预期版本，并返回保存成功后的新版本。
    assert repository.session_record.version == 0
    assert repository.session_record.schema_version == session_state.schema_version
    assert repository.session_record.save_kind == SessionStateSaveKind.CHECKPOINT.value
    state_data = json.loads(repository.session_record.state_json)
    assert "version" not in state_data
    assert "schema_version" not in state_data
    assert "agent_name2agent_state" in state_data
    assert "history" in state_data["agent_name2agent_state"]["root_agent"]


def test_in_memory_state_store_rejects_stale_session_version() -> None:
    """验证内存 Store 会拒绝使用旧版本覆盖已推进的会话状态。"""

    state_store = InMemoryStateStore()
    session_state = MySQLSessionState(session_id="session_1")
    asyncio.run(
        state_store.save_session_state(
            session_state,
            save_kind=SessionStateSaveKind.CHECKPOINT,
        )
    )
    stale_session_state = session_state.model_copy(deep=True)

    asyncio.run(
        state_store.save_session_state(
            session_state,
            save_kind=SessionStateSaveKind.CHECKPOINT,
        )
    )

    with pytest.raises(SessionStateChangedError, match="SessionState 已发生变更"):
        asyncio.run(
            state_store.save_session_state(
                stale_session_state,
                save_kind=SessionStateSaveKind.CHECKPOINT,
            )
        )


def test_os_service_raises_after_optimistic_lock_conflict() -> None:
    """验证 OSService 遇到乐观锁冲突时不再创建业务分支。"""

    state_store = InMemoryStateStore()
    session_state = MySQLSessionState(session_id="session_1")
    asyncio.run(
        state_store.save_session_state(
            session_state,
            save_kind=SessionStateSaveKind.CHECKPOINT,
        )
    )
    stale_session_state = session_state.model_copy(deep=True)
    asyncio.run(
        state_store.save_session_state(
            session_state,
            save_kind=SessionStateSaveKind.CHECKPOINT,
        )
    )

    os_service = OSService.__new__(OSService)
    os_service._state_store = state_store
    os_service.session_state = stale_session_state
    with pytest.raises(SessionStateChangedError, match="SessionState 已发生变更"):
        asyncio.run(
            os_service.save_session_state(
                save_kind=SessionStateSaveKind.CHECKPOINT,
            )
        )
