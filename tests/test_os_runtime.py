#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 18:53
# @Author  : YaHaoo
# @File    : test_os_runtime.py

"""os Runtime 默认依赖装配测试。"""

from __future__ import annotations

from typing import Any
import asyncio

from turtlemap.os.event_bus import MessageEvent, RuntimeEventPhase, RuntimeRunResult
from turtlemap.kernel.models import SystemDefinition
from turtlemap.os.models import SessionState
from turtlemap.os import Agent, Runtime
from turtlemap.os import runtime as runtime_module
from turtlemap.os.context.models import ContextCompressionTaskRecord


class _FakeContextBuildProvider:
    """用于 Runtime 装配测试的上下文构建器占位实现。"""

    def __init__(self, **kwargs: object) -> None:
        """接收 Runtime 透传的构造参数。

        参数:
            kwargs: Runtime 默认装配时传入的上下文构建依赖。
        """

        self.kwargs = kwargs


class _FakeMySQLStateStore:
    """用于验证 Runtime 使用服务启动阶段创建好的 MySQL pool。"""

    # fake store 接收到的 MySQL pool。
    pool: Any

    def __init__(self, pool: Any) -> None:
        """记录 Runtime 工厂传入的 pool。

        参数:
            pool: 服务启动阶段已初始化好的 MySQL 连接池对象。
        """

        self.pool = pool

    async def try_create_context_compression_task(
        self,
        session_state,
        agent_name: str,
    ) -> ContextCompressionTaskRecord | None:
        """保持 fake store 满足压缩任务存储协议。

        参数:
            session_state: 当前会话状态。
            agent_name: 当前 Agent 名称。

        返回:
            测试中不创建真实压缩任务，固定返回 `None`。
        """

        _ = (session_state, agent_name)
        return None

    async def finish_context_compression_task(
        self, compression_task: ContextCompressionTaskRecord
    ) -> None:
        """保持 fake store 满足压缩任务完成协议。

        参数:
            compression_task: 当前需要结束的压缩任务记录。

        返回:
            无返回值。
        """

        _ = compression_task


class _FakeEventBus:
    """用于验证 Runtime 生命周期内事件通道创建与关闭。"""

    # 已创建的事件通道标识列表。
    created_event_bus_ids: list[str] = []

    # 已关闭的事件通道标识列表。
    closed_event_bus_ids: list[str] = []

    # 已注册的 listener 映射。
    listener_id2listener: dict[str, object] = {}

    # 已取消注册的 listener 标识列表。
    unsubscribed_listener_ids: list[str] = []

    @classmethod
    def create_event_bus(cls) -> str:
        """模拟创建事件通道。

        返回:
            新创建的事件通道标识。
        """

        event_bus_id = f"event_bus_{len(cls.created_event_bus_ids) + 1}"
        cls.created_event_bus_ids.append(event_bus_id)
        return event_bus_id

    @classmethod
    def close_event_bus(cls, event_bus_id: str) -> None:
        """模拟关闭事件通道。

        参数:
            event_bus_id: 待关闭的事件通道标识。

        返回:
            无返回值。
        """

        cls.closed_event_bus_ids.append(event_bus_id)

    @classmethod
    def subscribe(
        cls,
        event_bus_id: str,
        listener: object,
        completion_listener: object | None = None,
        event_types: tuple[object, ...] | set[object] | None = None,
    ) -> str:
        """模拟注册事件监听者。

        参数:
            event_bus_id: 当前事件通道标识。
            listener: 当前待注册的监听者。
            completion_listener: 当前事件完成普通 listener 通知后触发的回调。
            event_types: 当前监听者关注的事件类型集合。

        返回:
            监听者注册标识。
        """

        _ = (event_bus_id, completion_listener, event_types)
        listener_id = f"listener_{len(cls.listener_id2listener) + 1}"
        cls.listener_id2listener[listener_id] = listener
        return listener_id

    @classmethod
    def unsubscribe(cls, event_bus_id: str, listener_id: str) -> None:
        """模拟取消事件监听者。

        参数:
            event_bus_id: 当前事件通道标识。
            listener_id: 当前监听者注册标识。

        返回:
            无返回值。
        """

        _ = event_bus_id
        cls.unsubscribed_listener_ids.append(listener_id)
        cls.listener_id2listener.pop(listener_id, None)

    @classmethod
    def reset(cls) -> None:
        """清理 fake event bus 记录。"""

        cls.created_event_bus_ids = []
        cls.closed_event_bus_ids = []
        cls.listener_id2listener = {}
        cls.unsubscribed_listener_ids = []


def _build_test_agent() -> Agent:
    """构造 Runtime 装配测试使用的最小 Agent。

    返回:
        可用于创建 Runtime 的 os 层 Agent。
    """

    return Agent(
        agent_name="root_agent",
        system=SystemDefinition(role="assistant", objective="help user"),
    )


def test_runtime_constructor_uses_mysql_store(monkeypatch) -> None:
    """验证 Runtime 构造函数默认使用 MySQL 状态存储。"""

    created_pool = object()
    _FakeEventBus.reset()
    monkeypatch.setattr(runtime_module, "get_mysql_pool", lambda: created_pool)
    monkeypatch.setattr(runtime_module, "MySQLStateStore", _FakeMySQLStateStore)
    monkeypatch.setattr(runtime_module, "ContextBuildProvider", _FakeContextBuildProvider)
    monkeypatch.setattr(runtime_module, "EventBus", _FakeEventBus)

    runtime = Runtime(
        root_agent=_build_test_agent(),
    )

    assert isinstance(runtime.services.state_store, _FakeMySQLStateStore)
    assert runtime.services.state_store.pool is created_pool
    assert runtime.event_bus_id == "event_bus_1"
    assert _FakeEventBus.created_event_bus_ids == ["event_bus_1"]


def test_runtime_del_closes_event_bus(monkeypatch) -> None:
    """验证 Runtime 析构时会关闭当前事件通道。"""

    created_pool = object()
    _FakeEventBus.reset()
    monkeypatch.setattr(runtime_module, "get_mysql_pool", lambda: created_pool)
    monkeypatch.setattr(runtime_module, "MySQLStateStore", _FakeMySQLStateStore)
    monkeypatch.setattr(runtime_module, "ContextBuildProvider", _FakeContextBuildProvider)
    monkeypatch.setattr(runtime_module, "EventBus", _FakeEventBus)

    runtime = Runtime(
        root_agent=_build_test_agent(),
    )

    runtime.__del__()

    assert runtime.event_bus_id is None
    assert _FakeEventBus.closed_event_bus_ids == ["event_bus_1"]


def test_runtime_run_returns_runtime_run_result(monkeypatch) -> None:
    """验证 os Runtime.run 会注册 collector 并返回运行结果对象。"""

    created_pool = object()
    _FakeEventBus.reset()
    monkeypatch.setattr(runtime_module, "get_mysql_pool", lambda: created_pool)
    monkeypatch.setattr(runtime_module, "MySQLStateStore", _FakeMySQLStateStore)
    monkeypatch.setattr(runtime_module, "ContextBuildProvider", _FakeContextBuildProvider)
    monkeypatch.setattr(runtime_module, "EventBus", _FakeEventBus)

    async def fake_kernel_run(self, input_events, session_id=None):
        """模拟 kernel Runtime.run，并向已注册 collector 投递稳定事件。

        参数:
            self: 当前 Runtime 实例。
            input_events: 本轮输入事件列表。
            session_id: 本轮会话 id。

        返回:
            fake 会话状态。
        """

        _ = input_events
        listener = _FakeEventBus.listener_id2listener["listener_1"]
        listener(
            MessageEvent(
                event_id="event_1",
                event_bus_id=self.event_bus_id,
                session_id=session_id or "session_1",
                agent_name="root_agent",
                content="hello",
                event_phase=RuntimeEventPhase.FINAL,
                sequence=1,
            )
        )
        return SessionState(
            session_id=session_id or "session_1",
            version=9,
        )

    monkeypatch.setattr(runtime_module.BaseRuntime, "run", fake_kernel_run)

    runtime = Runtime(
        root_agent=_build_test_agent(),
    )

    result = asyncio.run(
        runtime.run(
            input_events=[],
            session_id="session_1",
        )
    )

    assert isinstance(result, RuntimeRunResult)
    assert result.session_id == "session_1"
    assert result.session_version == 9
    assert result.event_bus_id == "event_bus_1"
    assert result.outputs[0].outputs[0].event_id == "event_1"
    assert _FakeEventBus.unsubscribed_listener_ids == ["listener_1"]
