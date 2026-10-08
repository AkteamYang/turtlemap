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
from turtlemap.kernel.models.enums import (
    AgentFrameChangeReason,
    AgentFrameChangeType,
    EventType,
    SessionStateSaveKind,
    TaskStateKind,
    TaskStatus,
)
from turtlemap.kernel.models.models import (
    BaseAgentFrameChange,
    BaseProcessingTask,
    MessageState,
    ToolState,
)
from turtlemap.kernel.runtime import BaseRuntime
from turtlemap.kernel.tool.models import RESERVED_UNIT_TYPE_LLM_CALL
from turtlemap.kernel.tool.models import RESERVED_UNIT_TYPE_LLM_CALL
from turtlemap.os.agent import Agent
from turtlemap.os.context.prompt import build_bullet_list
from turtlemap.os.event_bus.models import (
    AgentFrameChangeEvent,
    InputEvent,
    InterruptedEvent,
    RuntimeEvent,
    RuntimeEventPhase,
    TaskOutput,
)
from turtlemap.os.exceptions import OSRuntimeError
from turtlemap.os.tool.build_in.resume_task import ResumeTaskTool
from turtlemap.os.tool import LLMCallExecutionUnit
from turtlemap.os.tool.model import AgentFrameChange
from turtlemap.os.tool.tool import LLMCallExecutionResult
from turtlemap.shared.ids import (
    PREFIX_EVENT_ID,
    PREFIX_INPUT_ID,
    PREFIX_INTERRUPTION_REQUEST_ID,
    generate_prefixed_id,
)
from turtlemap.shared.logger import Logger
from turtlemap.shared.typing import ensure_instance

from .event_bus import EventBus, ResultCollector, RuntimeRunResult
from .interceptor.agent_interceptor import AgentInterceptor
from .interceptor import BaseAgentInterceptor
from .interceptor.context import AgentInterceptorContext, K_HAS_CONTINUE_TAG
from .llm.model import LLMMessage
from .interceptor.executor import InterceptorExecutor
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
        session_state: SessionState,
        state_store: StateStoreProtocol | None = None,
        interceptors: list[BaseAgentInterceptor] | None = None,
        event_bus_id: str | None = None,
    ) -> None:
        """初始化 OS 层 Runtime。

        参数:
            root_agent: 业务侧显式提供的根 Agent。
            session_state: 当前 Runtime 绑定的 os 层会话状态。
            state_store: 可选状态存储实现；为空时使用 os 层默认 InMemoryStateStore。
            interceptors: 追加在 os 默认拦截器后的运行期拦截器列表。
            event_bus_id: 可选事件通道标识；传入时仅借用该通道，否则由 Runtime 创建。
        """
        super().__init__(
            root_agent=root_agent,
            session_state=session_state,
        )
        self.state_store = state_store

        # os 默认拦截器优先处理框架控制标记，业务拦截器在其后追加执行。
        self.interceptors = [AgentInterceptor(), *(interceptors or [])]

        # 执行器只负责 hook 调度；每个调用点的动态上下文仍由 Runtime 构建。
        self.interceptor_executor = InterceptorExecutor(self.interceptors)

        if event_bus_id:
            # 外部传入的事件通道只借用，不接管生命周期。
            self.event_bus_id = event_bus_id
            self._owns_event_bus = False
        else:
            # Runtime 自建事件通道时，需要在析构阶段主动释放。
            self.event_bus_id = EventBus.create_event_bus()
            self._owns_event_bus = True

        # 创建 os_service 基础能力代理
        os_service = OSService(
            event_bus_id=self.event_bus_id,
            config=root_agent.config,
            session_state=session_state,
            state_store=state_store,
            interceptor_executor=self.interceptor_executor,
        )
        self.os_service = os_service

        # 最近一次成功 checkpoint 的稳定快照，只用于异常后的状态回退。
        self.last_session_state: SessionState | None = None

        # tool_provider 绑定 os_service，便于 tool_provider 调用 os 通用能力
        for agent in self.agent_name2agent.values():
            agent = ensure_instance(agent, Agent)
            agent.bind_service(os_service)

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

    @override
    def resolve_owner_agent(self) -> Agent:
        """返回当前控制权栈顶的 os 层 Agent。

        返回:
            当前会话控制权栈顶对应的精确 `Agent` 实例。

        异常:
            TypeError: 可达 Agent 图中存在非 os 层 `Agent` 时抛出。

        说明:
            kernel 只承诺返回 `BaseAgent`；os Runtime 的后续事件、上下文和拦截器
            链路依赖 `Agent` 的展示名称与工具服务能力，因此在此统一完成类型收窄。
        """

        return ensure_instance(
            super().resolve_owner_agent(),
            Agent,
            "当前 Owner Agent",
        )

    async def init_session(
        self,
        resume: bool = True,
    ) -> None:
        """装配当前 Runtime 需要执行的会话状态。

        参数:
            resume: 是否保留上次运行遗留的输入与进行中任务并尝试恢复。

        返回:
            无返回值。
        """

        # OSService 统一负责会话状态后处理，Runtime 只接收可运行的状态对象。
        await self.os_service.load_session_state(
            root_agent=self.root_agent,
            resume=resume,
        )

        self.last_session_state = deepcopy(self.real_session_state)

    async def run(  # type: ignore
        self,
        input_events: list[ObservableEvent],
        auto_finish: bool = True,
    ) -> RuntimeRunResult:
        """执行一次 os Runtime 主流程并返回稳定输出结果。

        参数:
            input_events: 本轮进入 Runtime 的标准化事件。
            auto_finish: 是否在本次运行结束后自动标记完成。业务侧需要先写入
                history 或执行其他收尾操作时可设为 `False`，并在完成后显式调用
                `finish()`。

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

            # Replay 已完成的 task
            await self._replay_finished_task_events_if_needed()

            # 执行生成
            try:
                if input_events:
                    input_context = AgentInterceptorContext(
                        agent=self.resolve_owner_agent(),
                        session_state=self.real_session_state,
                    )
                    input_events = await self.interceptor_executor.input_before_process(
                        context=input_context,
                        input_events=input_events,
                    )
                await super().run(
                    input_events=input_events,
                )

                # 当前任务可能未产生新 event 无法触发本任务历史 event 的 replay
                # 待完成后由 runtime 补发一次
                await self._replay_finished_task_events_if_needed()

                # 收集产物
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
            if auto_finish:
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

        # history 归属于会话，所有 Agent 后续都从同一份会话产物投影上下文。
        self.real_session_state.history.extend(history_artifacts)

    @override
    async def _override_service_task_did_complete(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
    ) -> None:
        """根据任务终态结果确定后续 Agent 控制权变更。

        参数:
            owner_agent: 当前完成任务的 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 已写入 history、尚未从 owner_state 移除的完成任务。

        返回:
            无返回值。

        说明:
            handoff 复用工具执行结果中产生的 `AgentFrameChange`；普通 subagent
            任务则读取最终 LLMMessage 的 Runtime 参数。没有控制权变更时保持
            `task.agent_frame_change` 为 `None`。
        """

        _ = owner_state
        task.agent_frame_change = self._build_task_agent_frame_change(
            owner_agent=ensure_instance(
                owner_agent,
                Agent,
                "已完成任务的 Owner Agent",
            ),
            task=task,
        )

    def _build_task_agent_frame_change(
        self,
        owner_agent: Agent,
        task: BaseProcessingTask,
    ) -> AgentFrameChange | None:
        """从完成任务的工具结果或最终消息构建控制权栈变更。

        参数:
            owner_agent: 已完成任务的精确 os 层 Agent。
            task: 已完成、待关闭的任务现场。

        返回:
            handoff 时返回工具产生的 push 变更；普通 subagent 未声明继续执行时
            返回 pop 变更；无需变更时返回 `None`。
        """
        # handoff 场景 push frame
        if task.state.kind == TaskStateKind.TOOL:
            tool_state = ensure_instance(task.state, ToolState, "已完成工具任务状态")
            execution_unit = tool_state.execution_units[-1]
            if not execution_unit.result:
                return None

            if execution_unit.result.handoff is not None:
                return ensure_instance(execution_unit.result.handoff, AgentFrameChange, "工具任务的 Agent 控制权变更")

        # subagent 完成任务 pop frame
        current_agent_is_subagent = len(self.real_session_state.agent_frames) > 1
        if not current_agent_is_subagent:
            return None

        message = self._get_completed_task_message(task)
        if message is None or message.runtime_params.get(K_HAS_CONTINUE_TAG, False):
            return None

        current_frame = self.real_session_state.agent_frames[-1]
        parent_frame = self.real_session_state.agent_frames[-2]
        if current_frame.agent_name != owner_agent.agent_name:
            raise OSRuntimeError(
                "完成任务的 Owner Agent 与当前控制权栈顶不一致："
                f"owner_agent={owner_agent.agent_name}, "
                f"current_agent={current_frame.agent_name}, task_id={task.task_id}"
            )
        
        return AgentFrameChange(
            source=current_frame.agent_name,
            target=parent_frame.agent_name,
            type=AgentFrameChangeType.POP,
            reason=AgentFrameChangeReason.HANDOFF_RETURN,
        )

    @staticmethod
    def _get_completed_task_message(task: BaseProcessingTask) -> LLMMessage | None:
        """读取完成任务最终可用于决定控制流的 assistant 消息。

        参数:
            task: 已完成的消息或工具循环任务。

        返回:
            MessageState 的最终消息，或 ToolState 最后一个 continuation LLM 消息；
            任务未形成 LLM 消息时返回 `None`。
        """

        if task.state.kind == TaskStateKind.MESSAGE:
            message_state = ensure_instance(task.state, MessageState, "已完成消息任务状态")
            if message_state.message is None:
                return None

            return ensure_instance(message_state.message.payload, LLMMessage, "已完成消息任务产物")

        if task.state.kind == TaskStateKind.TOOL:
            tool_state = ensure_instance(task.state, ToolState, "已完成工具任务状态")
            if not tool_state.execution_units:
                return None

            execution_unit = tool_state.execution_units[-1]
            if execution_unit.unit_type != RESERVED_UNIT_TYPE_LLM_CALL:
                return None

            execution_unit = ensure_instance(execution_unit, LLMCallExecutionUnit, "已完成工具任务的最后一个执行单元")
            if execution_unit.result is not None:
                return execution_unit.real_result.message

        return None

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
                agent_display_name=ensure_instance(
                    self.agent_name2agent.get(agent_name),
                    Agent,
                    "InputEvent 所属 Agent",
                ).name,
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

    @override
    async def _override_service_agent_frame_did_change(
        self,
        owner_agent: BaseAgent,
        source_task: BaseProcessingTask,
        frame_change: BaseAgentFrameChange
    ) -> None:
        """发布任务触发的 Agent 控制权栈变更事件。

        参数:
            owner_agent: 完成当前任务的来源 Agent。
            source_task: 触发本次控制权变更的源任务。
            frame_change: 已执行的 push 或 pop 控制权变更。

        返回:
            无返回值。

        说明:
            handoff 使用 push，subagent 完成并返回父 Agent 时使用 pop；两类事件均
            以源任务为归属发布，供展示层和业务观测统一消费。
        """
        agent_frame_change = ensure_instance(frame_change, AgentFrameChange)
        display_owner_agent = ensure_instance(
            owner_agent,
            Agent,
            "AgentFrameChangeEvent 所属 Agent",
        )
        await ProcessingTask.publish(
            source_task,
            self.event_bus_id,
            AgentFrameChangeEvent(
                event_id=generate_prefixed_id(PREFIX_EVENT_ID),
                session_id=self.real_session_state.session_id,
                agent_name=owner_agent.agent_name,
                agent_display_name=display_owner_agent.name,
                task_id=source_task.task_id,
                payload=agent_frame_change
            )
        )

    @override
    def _overrideable_pop_task(self, owner_state: BaseAgentState, task: BaseProcessingTask) -> None:
        super()._overrideable_pop_task(owner_state, task)

        # 保存已完成任务的 event，用于回放
        p_task = ensure_instance(task, ProcessingTask)
        self.os_service.save_finished_task_evnets(p_task)

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
                    if task.event_buffer.did_replay:
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
        task.event_buffer.did_replay = True  # 避免flush历史event
        resume_prompt = build_bullet_list([
            "用户重试时：`{}`"
        ])

        # 发起中断请求
        request, event = await self.os_service.perform_interruption_request(
            task=task,
            agent_name=owner_state.agent_name,
            agent_display_name=ensure_instance(
                owner_agent,
                Agent,
                "中断任务所属 Agent",
            ).name,
            type=InterruptionRequestType.EXCEPTION_RESUME,
            assistant_content="任务执行过程中发生异常，需要重试吗？",
            request_params={},
            resume_prompt=resume_prompt
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

    async def _replay_finished_task_events_if_needed(self):
        for buffer in self.real_session_state.finished_task_events:
            if buffer.did_replay:
                continue

            for e in buffer.events:
                e.is_replay_event = True
                await ProcessingTask.publish(
                    task=None,
                    event_bus_id=self.event_bus_id, 
                    event=e
                )
            buffer.did_replay = True
