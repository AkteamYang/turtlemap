#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2026 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/17 00:00
# @Author  : YaHaoo
# @File    : service.py

"""turtlemap os 层统一服务代理。"""

from __future__ import annotations
from typing import Tuple

from turtlemap.config import TurtleMapConfig
from turtlemap.kernel.agent import BaseAgent
from turtlemap.kernel.models import (
    AgentFrame,
    BaseSessionState,
    BaseProcessingTask,
    RuntimeArtifact,
    RuntimeArtifactType,
    SessionStateSaveKind,
)
from turtlemap.kernel.models.enums import InterruptionRequestType, TaskStateKind, TaskStatus
from turtlemap.kernel.models.models import BaseAgentState, InterruptionRequest
from turtlemap.kernel.tool.service import ExecutableTool
from turtlemap.os.event_bus.collector import ResultCollector
from turtlemap.os.event_bus.event_bus import EventBus
from turtlemap.os.event_bus.models import (
    InputEvent,
    InterruptedEvent,
    MessageEvent,
    RuntimeEvent,
    RuntimeEventPhase,
)
from turtlemap.os.llm.model import LLMCompletionChunk, LLMToolChoice
from turtlemap.os.context.models import (
    ContextBuildInput,
    ContextBuildResult,
    ContextCompressionResult,
)
from turtlemap.os.models import AgentState, ProcessingTask, SessionState
from turtlemap.shared.ids import (
    PREFIX_EVENT_ID,
    PREFIX_INTERRUPTION_REQUEST_ID,
    PREFIX_RUN_ID,
    generate_prefixed_id,
)
from turtlemap.shared.logger import Logger
from turtlemap.shared.typing import ensure_instance
from .context.provider import ContextBuildProvider
from .context.compress import ContextCompressionProvider
from .exceptions import OSErrorBase, OSRuntimeError
from .executor import NoopExecutor
from .store import InMemoryStateStore
from .protocols import (
    ContextBuildProtocol,
    ContextCompressionProtocol,
    ExecutorProtocol,
    ModelClientProtocol,
    StateStoreProtocol,
)
from .llm.provider import ModelClientProvider


class OSService:
    """表示 os 层事务统一代理服务。
    """

    def __init__(
        self,
        config: TurtleMapConfig | None = None,
        event_bus_id: str = "",
        state_store: StateStoreProtocol | None = None,
    ) -> None:
        """初始化 OSService。

        参数:
            config: 当前 Runtime 使用的完整应用配置；为空时从环境变量读取。
            event_bus_id: 当前 Runtime 绑定的事件通道标识。
            state_store: 可选状态存储实现；为空时使用默认 InMemoryStateStore。
        """
        self.event_bus_id = event_bus_id
        self.session_state: SessionState | None = None
        self.collector: ResultCollector | None = None

        # OSService 持有完整配置，再分发给 LLM、上下文压缩等具体 provider。
        effective_config = config or TurtleMapConfig.from_env()
        self.config = effective_config

        # LLM
        self.model_client_provider: ModelClientProtocol = ModelClientProvider(
            config=effective_config.llm
        )

        # 存储
        effective_state_store = state_store or InMemoryStateStore()
        self._state_store: StateStoreProtocol = effective_state_store

        # 后台执行器
        self.executor: ExecutorProtocol = NoopExecutor()

        # 会话压缩
        self.compression_provider: ContextCompressionProtocol = ContextCompressionProvider(
            model_client_provider=self.model_client_provider,
            state_store=self._state_store,
            keep_recent_history_rounds=(
                effective_config.context_token_budget.keep_recent_history_rounds
            ),
        )

        # 上下文构建
        self.context_build_provider: ContextBuildProtocol = ContextBuildProvider(
            compression_provider=self.compression_provider,
        )

    @property
    def real_session_state(self) -> SessionState:
        return ensure_instance(self.session_state, SessionState)
    
    @property
    def real_collector(self) -> ResultCollector:
        return ensure_instance(self.collector, ResultCollector)

    async def load_session_state(
        self,
        session_state: BaseSessionState,
        root_agent: BaseAgent,
        resume: bool = True,
    ) -> SessionState:
        """校验并装配 os 层 SessionState。

        参数:
            session_state: 外部业务层已加载或创建的会话状态对象。
            root_agent: 当前 Runtime 的根 Agent，用于解析可达 Agent 集合。
            resume: 是否保留未消费输入与运行中任务，用于继续上次中断的运行。

        返回:
            已装配 BaseAgentState 的 SessionState。
        """

        session_state = ensure_instance(session_state, SessionState, "会话状态")
        agent_name2agent = root_agent.resolve_reachable_agents()

        # 持久化状态已包含 AgentState；这里再按当前代码中的 Agent 图校验并补齐。
        session_state.agent_name2agent_state = await self._load_agent_name2agent_state(
            session_state=session_state,
            agent_name2agent=agent_name2agent,
        )

        # AgentState 补齐后再校验控制权栈，确保交给 kernel 的现场可继续推进。
        self._sanitize_agent_frames(
            session_state=session_state,
            root_agent=root_agent,
        )

        self.session_state = session_state

        # 非恢复运行只继承稳定 history，丢弃未消费输入与未结束的运行现场。
        if not resume:
            session_state.input_queue = []
            top_agent_state = session_state.top_agent_state()
            top_agent_state.processing_tasks = [
                task
                for task in top_agent_state.processing_tasks
                if not task.state.is_running
            ]

        # 移除无效任务
        for _, agent_state in session_state.agent_name2agent_state.items():
            processing_tasks = []
            for p in agent_state.processing_tasks:
                p = ensure_instance(p, ProcessingTask)
                if p.event_buffer.events and isinstance(p.event_buffer.events[0], InputEvent):
                    processing_tasks.append(p)
                else:
                    Logger.logger.warning(
                        f"Processing tasks检查：event_buffer首元素不是InputEvent，task无效被移除，task_id: {p.task_id}"
                    )
            agent_state.processing_tasks = processing_tasks

        # run_id 是当前 run 的运行批次标识，每次运行时重新生成。
        session_state.run_id = generate_prefixed_id(PREFIX_RUN_ID)
        return session_state

    async def save_session_state(
        self,
        save_kind: SessionStateSaveKind,
    ):
        """保存 SessionState。

        参数:
            save_kind: 当前保存动作对应的保存语义。

        返回:
            无返回值。
        """

        session_state = self.real_session_state
        await OSService._save_session_state(session_state, self._state_store, save_kind)

    async def generate_assistant_message_with_task(
        self,
        owner_agent: BaseAgent,
        task: BaseProcessingTask,
        no_tool_call: bool = False,
    ) -> RuntimeArtifact:
        """基于当前任务现场生成一次 assistant 产物。

        参数:
            owner_agent: 当前拥有控制权的 Agent。
            task: 当前正在生成 assistant 消息的任务现场。
            no_tool_call: 是否禁用当前轮工具调用；工具异常后的兜底生成会开启该选项。

        返回:
            os 层生成的 Runtime 产物。

        说明:
            OSService 不再额外接收 input 或过程消息；上下文构建统一从
            `task.start_input` 和 `task.state` 中恢复当前任务需要进入 LLM 的产物。
            `no_tool_call` 为工具调用失败后的兜底开关，开启时不会向模型暴露工具。
        """
        assert self.session_state is not None

        from turtlemap.os.agent import Agent

        if task.start_input is None:
            raise OSRuntimeError(f"生成 assistant 消息时缺少任务起点输入，task_id={task.task_id}")

        effective_owner_agent = ensure_instance(owner_agent, Agent, "当前 Owner Agent")
        owner_state = self.session_state.agent_name2agent_state.get(
            effective_owner_agent.agent_name
        )
        if owner_state is None:
            raise OSErrorBase(
                f"生成 assistant 消息时缺少 Owner Agent 状态：agent_name={effective_owner_agent.agent_name}"
            )
        available_executable_tools: list[ExecutableTool] = []
        tool_choice: LLMToolChoice | None = "none"

        # 禁用工具时显式传 tool_choice=none，避免模型受历史 tool_call 影响再次生成工具调用。
        if not no_tool_call:
            available_executable_tools = await effective_owner_agent.tool_provider.get_tools_with_inputs(
                input=task.start_input,
                agent=effective_owner_agent,
                session_state=self.session_state,
            )
            tool_choice = self._get_tool_choice(available_executable_tools)
        build_input = ContextBuildInput(
            session_state=self.session_state,
            owner_agent=effective_owner_agent,
            owner_agent_state=owner_state,
            task=task,
            available_tools=available_executable_tools,
        )
        context_result = await self.context_build_provider.build_llm_context(
            build_input=build_input,
        )

        # 发送 messagee 开始生成事件
        message_event_id = generate_prefixed_id(PREFIX_EVENT_ID)
        async def publish_event(event_phase: RuntimeEventPhase, chunk: LLMCompletionChunk | None = None):
            await ProcessingTask.publish(
                task,
                self.event_bus_id,
                MessageEvent(
                    event_id=message_event_id,
                    session_id=self.real_session_state.session_id,
                    agent_name=effective_owner_agent.agent_name,
                    task_id=task.task_id,
                    event_phase=event_phase,
                    chunk=chunk
                )
            )
        await publish_event(RuntimeEventPhase.STARTED)
        
        # 发送 messagee 过程事件
        async for chunk in self.model_client_provider.stream_generate_message(
            messages=context_result.messages,
            tool_schemas=context_result.tool_schemas,
            tool_choice=tool_choice
        ):
            await publish_event(RuntimeEventPhase.IN_PROGRESS, chunk)
        
        # 发送 messagee 结束生成事件
        await publish_event(RuntimeEventPhase.END)

        # 获取流式事件链聚合后的 FINAL 消息事件。
        final_event = ensure_instance(
            self.real_collector.get_final_event_with_event_id(message_event_id),
            MessageEvent,
        )
        if final_event.completion is None or not final_event.completion.choices:
            raise OSRuntimeError("LLM 未能生成有效消息")

        assistant_message = final_event.completion.choices[0].message
        artifact_type = (
            RuntimeArtifactType.TOOL_CALL
            if assistant_message.tool_calls
            else RuntimeArtifactType.ASSISTANT_MESSAGE
        )

        # 等本轮交互完成后再启动后台压缩任务
        await self.try_background_compression(
            build_input=build_input,
            context_result=context_result,
        )
        return RuntimeArtifact(
            type=artifact_type,
            payload=assistant_message,
        )

    async def try_background_compression(
        self,
        build_input: ContextBuildInput,
        context_result: ContextBuildResult,
    ) -> None:
        """在需要时调度后台上下文压缩，并在合并成功后更新存储。

        参数:
            build_input: 当前轮 LLM 上下文构建输入。
            context_result: 当前轮已经构建完成的上下文结果。

        返回:
            无返回值。
        """

        owner_agent = build_input.owner_agent
        session_state = build_input.session_state
        task = build_input.task

        async def checkpoint_after_compression(
            compression_result: ContextCompressionResult,
        ) -> None:
            """压缩结果成功合并后提交一次 checkpoint。

            参数:
                compression_result: 当前后台压缩完成后的结果对象。

            返回:
                无返回值。
            """
            if not compression_result.success:
                Logger.logger.warning(
                    f"后台压缩失败，session_id: {session_state.session_id}, run_id: {session_state.run_id}, task_id: {task.task_id}"
                )
                return 
            
            await self._merge_background_compression_result(
                owner_agent=owner_agent,
                session_state=session_state,
                compression_result=compression_result,
            )

        await self.context_build_provider.schedule_background_compression_if_needed(
            build_input=build_input,
            current_result=context_result,
            callback=checkpoint_after_compression,
        )

    async def checkpoint_for_run_finish(self) -> None:
        """清理已结束运行的临时数据并提交最终 checkpoint。

        返回:
            无返回值。
        """

        # 清理运行标记
        self.real_session_state.run_id = ""

        # pending 任务仅用于衔接近期输入；按原始顺序保留最后 5 个，避免长期累积。
        for agent_state in self.real_session_state.agent_name2agent_state.values():
            pending_task_count = 0
            retained_reversed_tasks: list[BaseProcessingTask] = []
            for task in reversed(agent_state.processing_tasks):
                if task.state.status == TaskStatus.PAUSED:
                    if pending_task_count >= 5:
                        continue
                    pending_task_count += 1
                retained_reversed_tasks.append(task)
            agent_state.processing_tasks = list(reversed(retained_reversed_tasks))

        await self.save_session_state(
            save_kind=SessionStateSaveKind.CHECKPOINT,
        )

    @staticmethod
    async def checkpoint_for_new_run_id(
        session_state: SessionState,
        state_store: StateStoreProtocol,
    ):
        if session_state.run_id:
            run_id = generate_prefixed_id(PREFIX_RUN_ID)
            session_state.run_id = run_id
            await OSService._save_session_state(session_state, state_store, SessionStateSaveKind.REFRESH)

    async def perform_interruption_request(
            self,
            task: ProcessingTask,
            agent_name: str,
            type: InterruptionRequestType,
            reason: str,
            request_params: dict
            ) -> Tuple[InterruptionRequest, InterruptedEvent]:
        request_id = generate_prefixed_id(PREFIX_INTERRUPTION_REQUEST_ID)
        interruption_request = InterruptionRequest(
            request_id=request_id,
            task_id=task.task_id,
            type=type,
            origin_task_status=task.state.status,
            reason=reason,
            params=request_params,
        )
        task.interruption_request = interruption_request
        event = InterruptedEvent(
                        event_id=generate_prefixed_id(PREFIX_EVENT_ID),
                        session_id=self.real_session_state.session_id,
                        agent_name=agent_name,
                        task_id=task.task_id,
                        event_phase=RuntimeEventPhase.FINAL,
                        interruption_type=type,
                        payload=interruption_request,
                    )
        await ProcessingTask.publish(
            task,
            self.event_bus_id,
            event
        )
        return interruption_request, event



    async def _merge_background_compression_result(
        self,
        owner_agent: BaseAgent,
        session_state: SessionState,
        compression_result: ContextCompressionResult,
    ) -> None:
        """将后台压缩结果合并到 store 中最新的会话状态。

        参数:
            owner_agent: 当前压缩结果所属的 Owner Agent。
            session_state: 调度压缩时用于定位会话身份的 SessionState。
            compression_result: 当前后台压缩完成后的结果对象。

        返回:
            无返回值；若目标状态已变化导致无法安全合并，则由具体合并路径跳过写入。
        """
        latest_owner_state = session_state.agent_name2agent_state.get(
            owner_agent.agent_name
        )
        if latest_owner_state is not None:

            # 先更新当前运行态，未完成任务后续 LLM 调用可直接使用压缩后的上下文。
            compression_result.merged = ContextCompressionProvider.merge_compacted_result(
                owner_agent_state=latest_owner_state,
                compression_result=compression_result,
            )

        # store 层负责读取最新 session_state 并尝试合并保存，覆盖运行态已结束的回调场景。
        await self._state_store.save_context_compression_result(
            session_state=session_state,
            agent_name=owner_agent.agent_name,
            compression_result=compression_result,
        )

    async def _load_agent_name2agent_state(
        self,
        session_state: SessionState,
        agent_name2agent: dict[str, BaseAgent],
    ) -> dict[str, BaseAgentState]:
        """根据 SessionState 快照内容恢复或初始化 BaseAgentState。

        参数:
            session_state: 当前待装配的 os 层会话状态。
            agent_name2agent: 当前 Runtime 可达 Agent 映射。

        返回:
            以 `agent_name` 为键的 BaseAgentState 映射表。
        """

        agent_name2state: dict[str, BaseAgentState] = {}
        for agent_name, runtime_agent in agent_name2agent.items():
            long_term_memory = await self._state_store.load_agent_long_term_memory(
                session_state=session_state,
                agent_name=agent_name,
            )
            loaded_state = session_state.agent_name2agent_state.get(agent_name)
            if loaded_state is None:
                loaded_state = AgentState(
                    agent_name=runtime_agent.agent_name,
                    system=runtime_agent.system,
                )
            # SystemDefinition 属于代码配置，每次恢复时都以当前运行时代码为准。
            loaded_state.system = runtime_agent.system

            # 长期记忆属于跨会话主数据，每次加载时以 StateStore 当前值为准。
            loaded_state.memory.long_term_memory = long_term_memory
            agent_name2state[agent_name] = loaded_state
        return agent_name2state

    @staticmethod
    def _filter_runtime_event_buffer_before_save(session_state: SessionState) -> None:
        """在保存 SessionState 前压缩 Runtime 事件缓冲。

        参数:
            session_state: 当前即将写入 StateStore 的会话状态。

        返回:
            无返回值；会原地更新每个 ProcessingTask 的 event_buffer。

        说明:
            同一 event_id 下如果已经存在 FINAL 事件，说明该逻辑事件已有稳定
            聚合结果。保存时只保留 FINAL，既能减少 buffer 体积，也能避免恢复
            重放时再次触发 STARTED -> END 聚合。
        """

        for task in OSService._iter_os_processing_tasks(session_state):
            task.event_buffer.events = OSService._filter_runtime_events_before_save(
                task.event_buffer.events
            )

    @staticmethod
    def _filter_runtime_events_before_save(
        events: list[RuntimeEvent],
    ) -> list[RuntimeEvent]:
        """按事件链折叠单个任务 buffer 中已形成的最终事件。

        参数:
            events: 当前 ProcessingTask 持有的事件序列。

        返回:
            每条已完成事件链仅保留 FINAL 事件的稳定事件序列。
        """

        event_id2events: dict[str, list[RuntimeEvent]] = {}
        event_ids: list[str] = []
        for event in events:
            if event.event_id not in event_id2events:
                event_ids.append(event.event_id)
            event_id2events.setdefault(event.event_id, []).append(event)

        filtered_events: list[RuntimeEvent] = []
        for event_id in event_ids:
            events = event_id2events[event_id]
            final_events = [
                event
                for event in events
                if event.event_phase == RuntimeEventPhase.FINAL
            ]

            # 有 FINAL 说明该事件链已稳定，只保留 FINAL 快照用于恢复展示。
            filtered_events.extend(final_events or events)

        return filtered_events

    @staticmethod
    def _iter_os_processing_tasks(session_state: SessionState) -> list[ProcessingTask]:
        """收集当前会话中可持有 os 事件 buffer 的任务。

        参数:
            session_state: 当前需要遍历的 os 会话状态。

        返回:
            所有已恢复为 os `ProcessingTask` 的任务，保留 Agent 与任务原始顺序。
        """

        processing_tasks: list[ProcessingTask] = []
        for agent_state in session_state.agent_name2agent_state.values():
            processing_tasks.extend(
                task
                for task in agent_state.processing_tasks
                if isinstance(task, ProcessingTask)
            )
        return processing_tasks

    def _sanitize_agent_frames(
        self,
        session_state: SessionState,
        root_agent: BaseAgent,
    ) -> None:
        """校正 os 会话控制权栈，保证进入 kernel 主循环时仍可推进。

        参数:
            session_state: 已完成 BaseAgentState 装配的会话状态对象。
            root_agent: 当前 Runtime 根 Agent，用于恢复干净 root frame。

        返回:
            无返回值；必要时会原地修复 session_state。

        说明:
            当前实现会先对缺失 BaseAgentState 做兜底创建，因此这里只继续校验：
            若某个中间 frame 对应的 BaseAgentState 已没有任何执行中任务，说明
            这条恢复链已经断裂，直接回退到干净 root 会话环境。
        """

        if not session_state.agent_frames:
            self._reset_to_clean_root_session(
                session_state=session_state,
                root_agent=root_agent,
            )
            return

        for index, frame in enumerate(session_state.agent_frames):
            agent_state = session_state.agent_name2agent_state.get(frame.agent_name)
            if agent_state is None:
                self._reset_to_clean_root_session(
                    session_state=session_state,
                    root_agent=root_agent,
                )
                return

            is_last_frame = index == len(session_state.agent_frames) - 1

            # 中间 frame 如果没有未完成任务，就说明控制权链已断，直接回退 root。
            if not is_last_frame and not agent_state.processing_tasks:
                self._reset_to_clean_root_session(
                    session_state=session_state,
                    root_agent=root_agent,
                )
                return

    @staticmethod
    def _reset_to_clean_root_session(
        session_state: BaseSessionState,
        root_agent: BaseAgent,
    ) -> None:
        """将损坏会话现场回退为干净的 root 会话环境。

        参数:
            session_state: 需要执行恢复清理的会话状态对象。
            root_agent: 当前 Runtime 根 Agent，用于确定 root frame。

        返回:
            无返回值；会原地修改 session_state。
        """

        # 控制权栈直接回退到 root frame，避免继续沿损坏链路恢复。
        session_state.agent_frames = [AgentFrame(agent_name=root_agent.agent_name)]

        # 所有未完成任务都视为不可信，统一清理，恢复到干净会话态。
        for agent_state in session_state.agent_name2agent_state.values():
            agent_state.processing_tasks = []

        # 未处理输入不继续保留，避免脏输入和脏任务一起进入恢复流程。
        session_state.input_queue = []

    def _get_tool_choice(self, tools: list[ExecutableTool]) -> LLMToolChoice | None:
        """根据当前可用工具列表推导 LLM tool_choice 参数。

        参数:
            tools: 当前轮允许暴露给模型的工具列表。

        返回:
            有工具时允许模型自动选择；无工具时显式禁止工具调用。
        """

        return "auto" if tools else "none"

    @staticmethod
    async def _save_session_state(
        session_state: SessionState,
        state_store: StateStoreProtocol,
        save_kind: SessionStateSaveKind,
    ):
        """保存 SessionState。

        参数:
            save_kind: 当前保存动作对应的保存语义。

        返回:
            无返回值。
        """

        OSService._filter_runtime_event_buffer_before_save(session_state)
        await state_store.save_session_state(session_state, save_kind=save_kind)
