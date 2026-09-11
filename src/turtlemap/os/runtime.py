#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/15 14:50
# @Author  : YaHaoo
# @File    : runtime.py

"""turtlemap 外围 OS Runtime 实现。"""

from __future__ import annotations

from copy import deepcopy
from typing import List

from typing_extensions import override

from turtlemap.kernel.agent import BaseAgent
from turtlemap.kernel.models import (
    BaseAgentState,
    Input,
    InterruptionRequest,
    InterruptionRequestType,
    ObservableEvent,
    RuntimeArtifact,
)
from turtlemap.kernel.models.enums import EventType, SessionStateSaveKind, TaskStatus
from turtlemap.kernel.models.models import BaseProcessingTask, MessageState
from turtlemap.kernel.runtime import BaseRuntime
from turtlemap.os.agent import Agent
from turtlemap.os.event_bus.models import (
    InputEvent,
    InterruptedEvent,
    RuntimeEvent,
    RuntimeEventPhase,
    TaskOutput,
)
from turtlemap.os.exceptions import OSRuntimeError
from turtlemap.shared.ids import (
    PREFIX_EVENT_ID,
    PREFIX_INPUT_ID,
    PREFIX_INTERRUPTION_REQUEST_ID,
    generate_prefixed_id,
)
from turtlemap.shared.logger import Logger
from turtlemap.shared.typing import ensure_instance

from .event_bus import EventBus, ResultCollector, RuntimeRunResult
from .models import ProcessingTask, SessionState
from .protocols import StateStoreProtocol
from .service import OSService


class Runtime(BaseRuntime):
    """表示 os 层默认 Runtime 壳实现。

    说明:
        当前类负责创建 os 层统一服务代理，并将其注入 BaseRuntime，
        避免业务侧直接感知 kernel 与 os 内部依赖装配细节。
    """

    def __init__(
        self,
        root_agent: Agent,
        state_store: StateStoreProtocol | None = None,
    ) -> None:
        """初始化 OS 层 Runtime。

        参数:
            root_agent: 业务侧显式提供的根 Agent。
            state_store: 可选状态存储实现；为空时使用 os 层默认 InMemoryStateStore。
        """
        super().__init__(
            root_agent=root_agent,
        )
        self.state_store = state_store

        if root_agent.event_bus_id:
            # 外部传入的事件通道只借用，不接管生命周期。
            self.event_bus_id = root_agent.event_bus_id
            self._owns_event_bus = False
        else:
            # Runtime 自建事件通道时，需要在析构阶段主动释放。
            self.event_bus_id = EventBus.create_event_bus()
            root_agent.event_bus_id = self.event_bus_id
            self._owns_event_bus = True

        # 创建 os_service 基础能力代理
        os_service = OSService(
            event_bus_id=self.event_bus_id,
            config=root_agent.config,
            state_store=state_store,
        )
        self.os_service = os_service

        # 最近一次成功 checkpoint 的稳定快照，只用于异常后的状态回退。
        self.last_session_state: SessionState | None = None

        # tool_provider 绑定 os_service，便于 tool_provider 调用 os 通用能力
        root_agent.bind_service(os_service)

    def __del__(self) -> None:
        """在 Runtime 被销毁时释放运行时事件通道。

        返回:
            无返回值。

        说明:
            析构阶段可能发生在解释器清理过程中，因此这里不向外抛出异常。
            只有当前 Runtime 自己创建的 `event_bus_id` 才会关闭；
            外部传入的事件通道由创建方负责生命周期。
        """

        if not self._owns_event_bus:
            return

        event_bus_id = getattr(self, "event_bus_id", None)
        if event_bus_id is None:
            return

        try:
            EventBus.close_event_bus(event_bus_id)
            self.event_bus_id = ""
        except Exception:
            return
        
    @property
    def real_session_state(self) -> SessionState:
        return self.os_service.real_session_state

    async def init_session(
        self,
        session_state: SessionState,
        resume: bool = True,
    ) -> None:
        """装配当前 Runtime 需要执行的会话状态。

        参数:
            session_state: 外部业务层加载或新建的 os 会话状态。
            resume: 是否保留上次运行遗留的输入与进行中任务并尝试恢复。

        返回:
            无返回值。
        """

        # OSService 统一负责会话状态后处理，Runtime 只接收可运行的状态对象。
        await self.os_service.load_session_state(
            session_state=session_state,
            root_agent=self.root_agent,
            resume=resume,
        )

        self.last_session_state = deepcopy(self.real_session_state)

    async def run(  # type: ignore
        self,
        input_events: list[ObservableEvent],
    ) -> RuntimeRunResult:
        """执行一次 os Runtime 主流程并返回稳定输出结果。

        参数:
            input_events: 本轮进入 Runtime 的标准化事件。

        返回:
            本次运行对应的 RuntimeRunResult，包含本轮稳定输出列表。

        说明:
            os 层 run 会先为当前 event_bus_id 注册 ResultCollector，再执行
            kernel Runtime 主流程。主流程结束后读取 collector 收集到的稳定
            outputs，并注销本轮 collector 监听者。
        """

        listener_ids = []
        try:
            if self.event_bus_id is None:
                raise RuntimeError("Runtime 事件通道已释放，不能继续执行 run")

            # 注册结果收集器
            result_collector = ResultCollector(event_bus_id=self.event_bus_id)
            self.os_service.collector = result_collector
            listener_ids = [
                EventBus.subscribe(  # 结果收集器
                    self.event_bus_id,
                    listener=result_collector.collect,
                    completion_listener=result_collector.finalize_outputs,
                    event_types=ResultCollector.listen_event_types(),
                ),
                EventBus.subscribe(  # buffer 收集
                    self.event_bus_id,
                    self._write_buffer,
                ),
            ]
            run_id = self.real_session_state.run_id

            # 执行生成
            try:
                await super().run(
                    input_events=input_events,
                    session_state=self.real_session_state,
                )
                outpust = result_collector.get_outputs()
            except Exception as exc:
                outpust = await self._pause_task_after_exception(input_events, exc)

            # 执行完成先构造稳定结果；默认场景随后自动清理本轮运行现场。
            run_result = RuntimeRunResult(
                session_id=self.real_session_state.session_id,
                run_id=run_id,
                event_bus_id=self.event_bus_id,
                outputs=outpust,
            )
            await self.finish()
            return run_result
        finally:
            for listener_id in listener_ids:
                EventBus.unsubscribe(self.event_bus_id, listener_id)

    async def finish(self) -> None:
        """标记本次 Runtime 执行完成。

        返回:
            无返回值。

        说明:
            清理本次执行的过程数据如 event_buffer、run_id 等；下次执行时将是新的会话。
        """

        await self.os_service.checkpoint_for_run_finish()
    
    @override
    async def _override_service_commit_runtime_checkpoint(self) -> None:
        """提交一次 Runtime checkpoint。

        说明:
            该方法会同时提交 `SessionState` 与全部 `BaseAgentState` 的新快照，
            并在提交成功后回写当前内存态版本号。
        """

        await self.os_service.save_session_state(
            save_kind=SessionStateSaveKind.CHECKPOINT,
        )
        self.last_session_state = deepcopy(self.real_session_state)

    @override
    async def _override_service_generate_assistant_message(
        self,
        owner_agent: BaseAgent,
        task: BaseProcessingTask,
        no_tool_call: bool = False,
    ) -> RuntimeArtifact:
        """生成一次 assistant Runtime 产物。

        参数:
            owner_agent: 当前拥有控制权的 Agent。
            task: 当前正在生成 assistant 消息的任务现场。
            no_tool_call: 是否禁用当前轮工具调用；透传给 OSService 处理。

        返回:
            os 层生成的 Runtime 产物。
        """

        return await self.os_service.generate_assistant_message_with_task(
            owner_agent=owner_agent,
            task=task,
            no_tool_call=no_tool_call,
        )

    @override
    async def _override_service_save_task_history(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
    ) -> None:
        """保存任务闭合后应写入 history 的 Runtime 产物。

        参数:
            owner_agent: 当前完成任务的 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 当前已完成或已暂停、需要整理 history 的任务现场。
        """

        history_artifacts = self.os_service.context_build_provider.build_task_artifacts(
            owner_agent=owner_agent,
            owner_state=owner_state,
            task=task,
        )
        if not history_artifacts:
            return

        # history 是 BaseAgentState 的一部分，checkpoint 保存 BaseAgentState 时会一起落库。
        owner_state.history.extend(history_artifacts)

    @override
    async def _override_service_input_did_process(
        self,
        task: BaseProcessingTask,
        agent_name: str,
        processed_input: Input,
    ) -> None:
        """在任务消费输入后发布对应的 InputEvent。

        参数:
            task: 消费当前输入的新建任务，或已恢复并继续执行的暂停任务。
            agent_name: 当前任务所属 Agent 名称。
            processed_input: 已被当前任务消费的标准输入包。

        返回:
            无返回值。

        说明:
            每次 `run` 都使用新的 ResultCollector。无论是创建新任务还是
            恢复暂停任务，都必须发送消费后的输入事件，才能让本轮最终事件
            按 `task_id` 与正确输入组成完整 `TaskOutput`。
        """

        await ProcessingTask.publish(
            task,
            self.event_bus_id,
            InputEvent(
                event_id=generate_prefixed_id(PREFIX_EVENT_ID),
                session_id=self.real_session_state.session_id,
                agent_name=agent_name,
                task_id=task.task_id,
                event_phase=RuntimeEventPhase.FINAL,
                input=processed_input,
            )
        )

    @override
    def _override_service_create_task_from_input(self, input: Input) -> BaseProcessingTask:

        # 目前仅支持创建 message 任务
        task = ProcessingTask(
            state=MessageState(origin_input_id=input.input_id),
            start_input=input,
        )
        return task

    def _write_buffer(self, event: RuntimeEvent) -> None:
        """将已发送事件写入所属任务的可恢复 buffer。

        参数:
            event: 当前由 event bus 发送、需要随任务持久化的 Runtime 事件。

        返回:
            无返回值。

        说明:
            buffer 仅承接带 task_id 的任务输出。无任务归属的事件属于会话级观测，
            不参与任务恢复重放。
        """

        for agent_state in self.real_session_state.agent_name2agent_state.values():
            for task in agent_state.processing_tasks:
                if isinstance(task, ProcessingTask) and task.task_id == event.task_id:
                    if task.event_buffer.did_flush_history_events:
                        task.event_buffer.events.append(event)
                    return

        Logger.logger.warning(
            f"未找到事件所属任务，跳过恢复 buffer 写入："
            f"event_id={event.event_id}, task_id={event.task_id}"
        )

    async def _pause_task_after_exception(self, input_events: list[ObservableEvent], exc: Exception) -> List[TaskOutput]:
        """回退到最近稳定快照，并把受影响任务转换为可恢复中断。

        参数:
            exc: Runtime 主流程捕获到的原始异常。

        返回:
            无返回值；成功时会保存暂停后的稳定会话状态并发送中断事件。

        说明:
            若不存在成功 checkpoint，或稳定快照中无法定位活动任务，当前方法
            不会猜测恢复位置，而是重新抛出原始异常。
        """

        if self.last_session_state is None:
            raise exc

        rollback_state = deepcopy(self.last_session_state)
        self.session_state = rollback_state
        self.os_service.session_state = rollback_state
        owner_state = rollback_state.top_agent_state()
        owner_agent = self.agent_name2agent[owner_state.agent_name]
        task = next(
            (
                item
                for item in owner_state.processing_tasks
                if item.state.is_running
            ),
            None,
        )
        if task is None:
            raise exc

        task = ensure_instance(task, ProcessingTask)
        task.event_buffer.did_flush_history_events = True  # 避免flush历史event

        # 发起中断请求
        request, event = await self.os_service.perform_interruption_request(
            task=task,
            agent_name=owner_state.agent_name,
            type=InterruptionRequestType.EXCEPTION_RESUME,
            reason="任务因运行异常暂停。",
            request_params={}
        )
        task.state.status = TaskStatus.PAUSED

        # 由子类实现会话历史的生成和持久化
        await self._override_service_save_task_history(
            owner_agent=owner_agent,
            owner_state=owner_state,
            task=task,
        )
        await self._override_service_commit_runtime_checkpoint()
        Logger.logger.warning(
            f"Runtime 任务异常后已回退并暂停：task_id={task.task_id}, "
            f"request_id={request.request_id}", exc_info=True
        )
        return [
            TaskOutput(
                input=Input(
                    input_id=generate_prefixed_id(PREFIX_INPUT_ID),
                    events=input_events,
                    ),
                outputs=[event]
            )
        ]
