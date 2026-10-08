#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/15 14:48
# @Author  : YaHaoo
# @File    : runtime.py

"""kernel 层最小 Runtime 原型实现。"""

from __future__ import annotations

import asyncio
from typing import Any

from turtlemap.kernel.models.enums import AgentFrameChangeType, EventSource, RuntimeArtifactType
from turtlemap.kernel.models.models import AgentFrame, BaseAgentFrameChange
from turtlemap.shared.typing import ensure_instance

from .agent import BaseAgent
from .models import (
    EventSource,
    EventType,
    ExecutionUnitStatus,
    TaskStateKind,
    TaskStatus,
    TaskSwitchAction,
)
from .exceptions import KernelRuntimeError, KernelUnsupportedOperationError
from .models import (
    BaseAgentState,
    BaseSessionState,
    Input,
    InterruptionRequest,
    InterruptionResponsePayload,
    MessageState,
    ObservableEvent,
    BaseProcessingTask,
    RuntimeArtifact,
    ToolState,
    UserInputPayload,
)
from .tool import (
    ExecutionUnit,
    ExecutionUnitResult,
    ToolExecutionContext,
)
from .tool.models import K_RAW_DATA_EVENT
from turtlemap.shared.ids import (
    PREFIX_INPUT_ID,
    PREFIX_OBSERVABLE_EVENT_ID,
    PREFIX_TASK_ID,
    generate_prefixed_id,
)
from turtlemap.shared.logger import Logger


class BaseRuntime:
    """提供最小可跑的 kernel Runtime 主循环。"""

    def __init__(
        self,
        root_agent: BaseAgent,
        session_state: BaseSessionState,
    ):
        """初始化 BaseRuntime。

        参数:
            root_agent: 业务侧显式提供的根 Agent。
            session_state: 当前 Runtime 绑定的会话状态。
        """

        # 根 Agent，控制整个可达 Agent 图的入口。
        self.root_agent = root_agent

        # 当前 root agent 可解析到的全部 Agent 映射。
        self.agent_name2agent = root_agent.resolve_reachable_agents()

        # 运行时内存队列，负责控制异步生产消费流程。
        self.runtime_input_queue: asyncio.Queue[Input] = asyncio.Queue()

        # 当前 Runtime 绑定的会话状态；一个 Runtime 实例不在运行中切换会话。
        self.session_state = session_state

    async def run(
        self,
        input_events: list[ObservableEvent],
        **kwargs
    ) -> Any:
        """执行一次最小 Runtime 主循环。

        参数:
            input_events: 本轮进入 Runtime 的标准化事件。

        返回:
            当前最新的 SessionState。
        """
        # 先根据持久化队列恢复运行时内存队列，保证中断前遗留输入优先处理。
        await self._restore_runtime_input_queue()

        if input_events:
            await self._push_input(
                Input(
                    input_id=generate_prefixed_id(PREFIX_INPUT_ID),
                    events=input_events,
                )
            )

        # 运行前状态保存
        # 保存run_id、input等输入信息
        await self._override_service_commit_runtime_checkpoint()

        # Runtime loop
        while True:
            owner_agent = self.resolve_owner_agent()
            owner_state = self.session_state.agent_name2agent_state[owner_agent.agent_name]

            # 恢复时先处理“执行已完成但推进未完成”的任务，保证状态转移优先闭合。
            completed_task = self._pick_completed_task(owner_state)
            if completed_task is not None:
                await self._advance_completed_task(
                    owner_agent=owner_agent,
                    owner_state=owner_state,
                    task=completed_task,
                )
                continue

            # 可执行的任务
            running_task = self._pick_available_running_task(owner_state)
            if running_task is not None:
                await self._advance_running_task(
                    owner_agent=owner_agent,
                    owner_state=owner_state,
                    task=running_task,
                )
                continue

            if self.runtime_input_queue.empty():
                break

            # 只有当前没有可推进任务时，才从输入队列中接管一个新输入创建任务。
            await self._process_new_input(
                owner_state=owner_state
            )

    def resolve_owner_agent(self) -> BaseAgent:
        """根据当前 `SessionState` 推导当前 Owner Agent。

        返回:
            当前位于控制权栈顶的 Agent 运行时对象。
        """

        owner_agent_name = self.session_state.agent_frames[-1].agent_name
        return self.agent_name2agent[owner_agent_name]

    async def _process_new_input(self, owner_state: BaseAgentState) -> None:
        """按输入队列顺序处理一个新输入包。

        参数:
            owner_state: 当前 Owner Agent 对应状态。

        返回:
            无返回值；方法会根据输入类型创建任务或消费中断响应。

        说明:
            中断响应必须等到对应输入包真正出队时再应用，避免新请求提前唤醒
            暂停任务，破坏持久化 input queue 恢复后的先后顺序。
        """

        start_input = await self._pop_input()
        if start_input.events[0].event_type == EventType.USER_INPUT:
            await self._start_new_task(
                owner_state=owner_state,
                start_input=start_input
            )
        elif start_input.events[0].event_type == EventType.INTERRUPTION_RESPONSE:
            await self._resume_runtime(start_input)
        else:
            event = start_input.events[0]
            raise KernelUnsupportedOperationError(
                "当前 Runtime 暂不支持该输入事件类型："
                f"input_id={start_input.input_id}, "
                f"event_id={event.event_id}, "
                f"event_type={event.event_type}"
            )

    async def _resume_runtime(
            self,
            response_input: Input,
        ) -> None:
        await self._apply_interruption_response(response_input)
                    
        # 任务创建完成进行状态保存
        await self._override_service_commit_runtime_checkpoint()

    async def _apply_interruption_response(
        self,
        response_input: Input,
    ) -> None:
        """消费中断响应事件，并返回仍需进入普通输入队列的事件。

        参数:
            response_input: 当前从输入队列取出的中断 response 输入包。

        返回:
            无返回值。

        说明:
            response 只负责写回 request 并唤醒任务，不直接解释具体业务结果。
            若同一任务仍有其他未响应请求，任务会继续保持暂停状态。
        """
        input_events = response_input.events
        event = input_events[0]
        if event.event_type != EventType.INTERRUPTION_RESPONSE:
            raise KernelRuntimeError(
                "中断响应输入包中包含非 interruption_response 事件，"
                "Runtime 无法在保持顺序的前提下混合处理："
                f"input_id={response_input.input_id}, "
                f"event={event.model_dump_json()}"
            )

        payload = ensure_instance(
            event.payload,
            InterruptionResponsePayload,
            "中断恢复事件载荷",
        )
        matched_request = self._find_interruption_request(payload.request_id)
        if matched_request is None:
            Logger.logger.warning(
                "未找到中断响应对应的暂停任务，忽略本次响应："
                f"event_id={event.event_id}, request_id={payload.request_id}, "
                f"request_type={payload.request_type}"
            )
            return

        task, request, agent_state = matched_request
        if request.response is not None:
            Logger.logger.warning(
                "中断请求已经收到响应，不能重复提交："
                f"request_id={payload.request_id}, "
                f"request_type={payload.request_type}, task_id={task.task_id}"
            )
            return

        # 单个中断请求被响应后，暂停任务即可重新进入主调度流程。
        request.response = payload
        task.state.status = request.origin_task_status

        # 调用完成 putlish input 事件
        await self._override_service_input_did_process(
            task=task,
            agent_name=agent_state.agent_name,
            processed_input=response_input,
        )

    def _find_interruption_request(
        self,
        request_id: str,
    ) -> tuple[BaseProcessingTask, InterruptionRequest, BaseAgentState] | None:
        """根据 request_id 查找暂停任务及其中断请求。

        参数:
            request_id: 外部 response 携带的中断请求唯一标识。

        返回:
            匹配到的任务与中断请求；未找到时返回 `None`。

        说明:
            只允许恢复暂停任务，避免迟到 response 意外改写已经继续推进的任务。
        """

        for agent_state in self.session_state.agent_name2agent_state.values():
            for task in agent_state.processing_tasks:
                if task.state.status != TaskStatus.PAUSED:
                    continue
                request = task.interruption_request
                if request is not None and request.request_id == request_id:
                    return task, request, agent_state

        return None

    async def _override_service_commit_runtime_checkpoint(self) -> None:
        """提交一次 Runtime checkpoint。

        说明:
            该方法会同时提交 `SessionState` 与全部 `BaseAgentState` 的新快照，
            并在提交成功后回写当前内存态版本号。
        """
        raise NotImplementedError("CheckPoint 需要子类实现")

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
            no_tool_call: 是否禁用当前轮工具调用；具体行为由 os 层实现决定。

        返回:
            子类生成的 Runtime 产物。
        """
        raise NotImplementedError("需要子类实现的服务")

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
        raise NotImplementedError("子类实现")

    async def _override_service_task_did_complete(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
    ) -> None:
        """在任务真正完成后由子类补充完成结果。

        参数:
            owner_agent: 当前完成任务的 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 已完成且已写入 history、尚未移除的任务现场。

        返回:
            无返回值。

        说明:
            OS 层可在此根据最终 LLM 消息或工具结果写入
            `task.agent_frame_change`。kernel 只消费该稳定状态并执行对应的
            Agent 控制权栈变更，不理解具体运行参数或业务控制标记。
        """

        _ = (owner_agent, owner_state, task)

    async def _override_service_input_did_process(
        self,
        task: BaseProcessingTask,
        agent_name: str,
        processed_input: Input,
    ) -> None:
        """在输入已被任务消费后执行 os 层通知。

        参数:
            task: 消费当前输入的新建任务，或已恢复并继续执行的暂停任务。
            agent_name: 当前任务所属 Agent 名称。
            processed_input: 已被当前任务消费的标准输入包。

        返回:
            无返回值。

        说明:
            新任务创建与中断响应恢复都会触发该钩子。kernel 只负责将输入
            消费到任务现场；os 层可据此发送 InputEvent，使本轮稳定输出能按
            task_id 归属到对应输入。
        """

        ...

    def _override_service_create_task_from_input(self, input: Input) -> BaseProcessingTask:
        raise NotImplementedError("子类实现")

    async def _override_service_agent_frame_did_change(
        self,
        owner_agent: BaseAgent,
        source_task: BaseProcessingTask,
        frame_change: BaseAgentFrameChange,
    ) -> None:
        """通知子类当前任务已切换到 handoff 状态。

        参数:
            source_task: 发起 handoff 的源任务。
            handoff_state: 包含目标 Agent 与 handoff 产物的状态。
        """

        raise NotImplementedError("子类实现")

    def _overrideable_pop_task(self, owner_state: BaseAgentState, task: BaseProcessingTask):
        """从当前 BaseAgentState 中移除指定任务。

        参数:
            owner_state: 当前 Owner Agent 对应状态。
            task: 需要移除的任务。

        说明:
            当前实现按 `task_id` 过滤任务列表；输入归属由任务状态自行持有，
            因此移除任务时不再清理会话级 current input。
        """

        owner_state.processing_tasks = [
            processing_task
            for processing_task in owner_state.processing_tasks
            if processing_task.task_id != task.task_id
        ]

    async def _restore_runtime_input_queue(self) -> None:
        """根据 `SessionState.input_queue` 重建运行时内存队列。

        说明:
            该步骤只恢复运行时消费队列，不改变持久化层中的输入顺序与内容。
        """

        self.runtime_input_queue = asyncio.Queue()
        for input_data in self.session_state.input_queue:
            await self.runtime_input_queue.put(input_data)

    async def _push_input(self, input_data: Input) -> None:
        """向运行时队列和持久化队列同时追加输入。

        参数:
            input_data: 需要进入 Runtime 的输入包。
        """

        self.runtime_input_queue.put_nowait(input_data)
        self.session_state.input_queue.append(input_data)

    async def _pop_input(self) -> Input:
        """从运行时队列和持久化队列同时弹出一个输入。

        返回:
            当前应被 Runtime 接管处理的输入包。
        """

        if self.runtime_input_queue.empty():
            raise KernelRuntimeError("当前 input queue 为空")
            
        input_data = await self.runtime_input_queue.get()
        for index, queued_input in enumerate(self.session_state.input_queue):
            if queued_input.input_id != input_data.input_id:
                continue

            # 按 input_id 对齐删除，避免运行时队列与持久化队列错位。
            self.session_state.input_queue.pop(index)
            break
        return input_data

    def _pick_completed_task(self, owner_state: BaseAgentState) -> BaseProcessingTask | None:
        """从当前 Owner Agent 中选择一个待推进完成态任务。

        参数:
            owner_state: 当前 Owner Agent 对应的主观状态。

        返回:
            第一个可继续推进的 completed 任务；若不存在则返回 `None`。
        """
        task = next((t for t in owner_state.processing_tasks if t.state.status == TaskStatus.COMPLETED), None)
        return task

    def _pick_available_running_task(self, owner_state: BaseAgentState) -> BaseProcessingTask | None:
        """从当前 Owner Agent 中选择一个当前可恢复执行的 running 任务。

        参数:
            owner_state: 当前 Owner Agent 对应的主观状态。

        返回:
            当前可立即继续执行的 running 任务；若不存在则返回 `None`。

        说明:
            当前阶段只从 `ToolState` 中挑选可执行任务。需要外部输入的任务
            会通过 `paused` 和中断 request 暂停，不在这里直接消费用户输入。
        """

        for processing_task in owner_state.processing_tasks:
            # 若没有待推进完成态任务，再继续处理仍在执行中的任务现场。
            if processing_task.state.status != TaskStatus.RUNNING:
                continue
            
            match processing_task.state.kind:
                case TaskStateKind.MESSAGE:
                    return processing_task
                
                # 当前阶段 running 恢复只面向 ToolState
                case TaskStateKind.TOOL:
                    if not isinstance(processing_task.state, ToolState):
                        raise KernelRuntimeError(
                            "任务状态 kind 为 tool，但实际状态对象不是 ToolState，"
                            f"task_id={processing_task.task_id}"
                        )

                    next_unit = self._pick_next_execution_unit(processing_task.state)
                    if next_unit:
                        return processing_task

        return None

    def _create_task_from_input(self, owner_state: BaseAgentState, input: Input) -> BaseProcessingTask:
        """根据当前输入包创建新的任务现场。

        参数:
            owner_state: 当前 Owner Agent 对应的主观状态。
            input: 当前从输入队列中取出的输入包。

        返回:
            成功创建的任务对象。

        说明:
            当前阶段只检查当前 `input.events[0]`。
            仅当首个事件为 `user_input` 时，才创建一个 `MessageState` 任务；
            空输入和其他事件类型都会抛出异常，避免 Runtime 静默跳过输入。
        """
        if not input.events:
            raise KernelRuntimeError(
                "根据输入包创建任务时输入事件列表为空："
                f"input_id={input.input_id}"
            )

        event = input.events[0]
        if event.event_type != EventType.USER_INPUT:
            raise KernelUnsupportedOperationError(
                "当前 Runtime 暂不支持从非 user_input 事件创建任务："
                f"input_id={input.input_id}, "
                f"event_id={event.event_id}, "
                f"event_type={event.event_type}"
            )

        # 目前仅支持创建 message 任务
        task = self._override_service_create_task_from_input(input=input)
        return task

    async def _advance_completed_task(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
    ) -> None:
        """推进已完成任务的后续状态转移并提交推进完成 checkpoint。

        参数:
            owner_agent: 当前 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 已完成核心执行、等待继续推进后续状态转移的任务现场。

        返回:
            无返回值；当前方法通过更新会话状态与任务现场完成推进。

        说明:
            当前方法只处理 `status = completed` 的任务，负责把任务结果继续推进为
            最终状态转移，例如写入 `History`、切换到下一种任务状态或移除任务现场。
            若任务类型当前未被 `BaseRuntime` 支持，则会抛出内核异常。
            所有推进逻辑完成后，统一提交一次“推进完成” checkpoint。
        """
        if task.state.kind == TaskStateKind.MESSAGE:
            await self._advance_completed_message_task(
                owner_agent=owner_agent,
                owner_state=owner_state,
                task=task,
            )

        # ToolState 的完成态只负责把已闭合的 tool loop 整理后写入 history。
        elif task.state.kind == TaskStateKind.TOOL:
            await self._advance_completed_tool_task(
                owner_agent=owner_agent,
                owner_state=owner_state,
                task=task,
            )

        else:
            raise KernelUnsupportedOperationError(
                f"当前 BaseRuntime 暂不支持推进该已完成任务类型：{task.state.kind}"
            )

        # MessageState 转为 ToolState 后会回到 running，只有仍保持 completed 的任务才进入收尾。
        if task.state.status == TaskStatus.COMPLETED:

            # OS 层在任务终态集中解释最终消息和工具结果，决定是否需要切换 Agent frame。
            await self._override_service_task_did_complete(
                owner_agent=owner_agent,
                owner_state=owner_state,
                task=task,
            )
            if task.agent_frame_change is not None:
                await self._change_agent_frame(
                    owner_agent=owner_agent,
                    task=task,
                    frame_change=task.agent_frame_change,
                )

            # 控制权切换通知仍归属于源任务，因此必须在移除任务前完成。
            self._overrideable_pop_task(owner_state=owner_state, task=task)

        # 任务已运行，清理旧的中断请求
        self._clear_interruption_request(task)

        # 任务完成保存状态
        await self._override_service_commit_runtime_checkpoint()

    async def _advance_completed_message_task(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
    ) -> None:
        """推进已完成 MessageState 的后续状态转移。

        说明:
            MessageState 在首轮执行结束后，只先把最终结果稳定落入任务现场并保存
            “任务完成 checkpoint”。真正的后续推进，例如写入 History 或切换到
            ToolState，统一在下一轮 loop 处理，和其他任务保持一致。是否进入任务收尾
            由调用方根据推进后的 `task.state.status` 统一判断。
        """
        message_state = ensure_instance(task.state, MessageState)
        if not message_state.message:
            raise KernelRuntimeError(
                "推进已完成 MessageState 时，message_state.message 不存在，"
                f"task_id={task.task_id}"
            )

        # tool_calls 分支属于推进阶段：把已完成的 MessageState 切换成 ToolState。
        # 此处不额外 checkpoint；若中断，可从既有 completed checkpoint 重新推进。
        if message_state.message.type == RuntimeArtifactType.TOOL_CALL:
            execution_units = await owner_agent.tool_provider.build_tool_execution_units(
                agent=owner_agent,
                task=task,
                message=message_state.message,
            )
            task.state = ToolState(
                tool_call_message=message_state.message,
                execution_units=execution_units,
                origin_input_id=(
                    task.start_input.input_id
                    if task.start_input is not None
                    else None
                ),
            )
            return

        # 由子类实现会话历史的生成和持久化
        await self._override_service_save_task_history(
            owner_agent=owner_agent,
            owner_state=owner_state,
            task=task,
        )

    async def _advance_completed_tool_task(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
    ) -> None:
        """推进已闭合 ToolState 的最终落历史动作。

        参数:
            owner_agent: 当前 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 当前已闭合、待收尾的工具循环任务。

        说明:
            ToolState 在所有 execution unit 闭合时已经保存“任务完成 checkpoint”。
            这里仅负责把稳定结果整理进 history，不再重复 checkpoint。任务完成后的
            Agent 控制权处理与任务移除由上层统一执行。
        """
        # 由子类实现会话历史的生成和持久化
        await self._override_service_save_task_history(
            owner_agent=owner_agent,
            owner_state=owner_state,
            task=task,
        )

    async def _advance_running_task(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
    ) -> None:
        """推进 running 任务。

        说明:
            当前阶段 running 恢复只面向 ToolState。
            MessageState 在执行过程中不建立 checkpoint，因此不会留下可恢复的
            running 中间现场；若中断，应依赖 input_queue 重新启动，而不是从
            半截 MessageState 继续执行。
        """
        match task.state.kind:
            case TaskStateKind.MESSAGE:
                await self._advance_running_message_task(
                    owner_agent=owner_agent,
                    task=task,
                )
            case TaskStateKind.TOOL:
                await self._advance_running_tool_task(
                    owner_agent=owner_agent,
                    owner_state=owner_state,
                    task=task,
                )
            case _:
                # 当前阶段除 ToolState 外，其他 running 状态都不应进入恢复执行路径。
                raise KernelUnsupportedOperationError(
                    f"当前 BaseRuntime 暂不支持恢复执行该任务类型：{task.state.kind}"
                )
        
        # 任务已运行，清理旧的中断请求
        self._clear_interruption_request(task)
        
        # 部分或者全部执行完成，checkpoint 保存状态
        await self._override_service_commit_runtime_checkpoint()

    async def _advance_running_message_task(
        self,
        owner_agent: BaseAgent,
        task: BaseProcessingTask,
    ) -> None:
        """启动一个新建的 `MessageState` 任务。

        参数:
            owner_agent: 当前 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 当前待启动的消息任务现场。
        """
        message_state = ensure_instance(task.state, MessageState, f"执行 MessageState 时，任务状态对象不是 MessageState，task_id={task.task_id}")
        start_input = task.start_input
        if not start_input:
            raise KernelRuntimeError(
                "执行 MessageState 时，start_input 不存在，"
                f"task_id={task.task_id}"
            )
        
        # 调用 LLM 生成
        assistant_message = await self._override_service_generate_assistant_message(
            owner_agent=owner_agent,
            task=task,
        )
        message_state.message = assistant_message

        # 完成状态，由 Runtime 推进后续流程
        message_state.status = TaskStatus.COMPLETED

    async def _advance_running_tool_task(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
    ) -> None:
        """继续执行一个处于 running 的 `ToolState` 任务。

        参数:
            owner_agent: 当前 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 当前待恢复执行的工具循环任务。

        说明:
            该方法会根据下一个待执行 unit 的状态，决定直接执行、先接管新输入，
            或将整个 `ToolState` 收敛到 completed。
        """
        tool_state = ensure_instance(task.state, ToolState)
        next_unit = self._pick_next_execution_unit(tool_state)
        if next_unit is None:
            raise KernelRuntimeError(
                "执行 ToolState 时未找到下一个 execution unit，"
                f"task_id={task.task_id}, "
                f"input_id={task.start_input.input_id if task.start_input else None}"
            )
        if next_unit.status == ExecutionUnitStatus.PENDING:
            next_unit.status = ExecutionUnitStatus.RUNNING

        # 执行 unit 
        execution_result = await owner_agent.tool_provider.execute_unit(
            context=ToolExecutionContext(
                agent=owner_agent,
                agent_name2agent=self.agent_name2agent,
                owner_state=owner_state,
                session_state=self.session_state,
                task=task,
            ),
            execution_unit=next_unit,
        )
        next_unit.result = execution_result

        # Runtime 更新 unit 状态
        next_unit.status = execution_result.next_unit_status
        tool_state.execution_units.extend(execution_result.appended_execution_units)

        # 检查任务是否完成
        all_complete = all(u.is_finished for u in tool_state.execution_units)
        if all_complete:
            task.state.status = TaskStatus.COMPLETED

        # 交互过程中出现了切换任务的请求
        # 如：tool 申请用户权限，但是用户输入进入其他任务，此时当前任务进入暂停，并切换到新任务
        if execution_result.task_switch_action is not None:
            await self._handle_task_switch_action(
                owner_agent=owner_agent,
                owner_state=owner_state,
                task=task,
                execution_result=execution_result,
            )

    async def _start_new_task(
        self,
        owner_state: BaseAgentState,
        start_input: Input | None = None,
    ) -> None:
        """创建一个刚由新输入触发的任务，后续执行由 Runtime 驱动

        参数:
            owner_agent: 当前 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。

        说明:
            当前阶段新输入只会先进入 MessageState。
            也就是说，该方法负责的是“从输入创建任务并开始首轮执行”，
            这里不会直接启动一个已经处于 ToolState 等中间现场的任务。
        """
        current_input = start_input or await self._pop_input()
        task = self._create_task_from_input(owner_state, current_input)
        owner_state.processing_tasks.append(task)

        # 调用完成
        await self._override_service_input_did_process(
            task=task,
            agent_name=owner_state.agent_name,
            processed_input=current_input,
        )

        # 任务创建完成进行状态保存
        await self._override_service_commit_runtime_checkpoint()

    async def _handle_task_switch_action(
        self,
        owner_agent: BaseAgent,
        owner_state: BaseAgentState,
        task: BaseProcessingTask,
        execution_result: ExecutionUnitResult,
    ) -> None:
        """处理执行结果要求 Runtime 接管的任务级切换动作。

        参数:
            owner_agent: 当前 Owner Agent。
            owner_state: 当前 Owner Agent 对应状态。
            task: 当前触发切换的任务现场。
            execution_result: 当前执行单元结果；其中的任务切换动作与原始数据共同
                决定 Runtime 的后续处理。

        返回:
            无返回值。

        异常:
            KernelUnsupportedOperationError: 当动作不是当前唯一支持的暂停语义时抛出。
        """

        _ = (owner_agent, owner_state)
        task_switch_action = ensure_instance(
            execution_result.task_switch_action,
            TaskSwitchAction,
            "执行结果中的任务切换动作",
        )
        match task_switch_action:
            case TaskSwitchAction.PAUSE:
                task.state.status = TaskStatus.PAUSED

                # 由子类实现会话历史的生成
                await self._override_service_save_task_history(
                    owner_agent=owner_agent,
                    owner_state=owner_state,
                    task=task,
                )
            case TaskSwitchAction.PUSH_EVENT:
                event = ensure_instance(
                    (execution_result.raw_data or {}).get(K_RAW_DATA_EVENT),
                    ObservableEvent,
                    "工具执行结果中的待推送事件",
                )

                # 经 Runtime 统一入队，确保内存队列与持久化 input_queue 保持一致。
                await self._push_input(
                    Input(
                        input_id=generate_prefixed_id(PREFIX_INPUT_ID),
                        events=[event],
                    )
                )
            case _:
                raise KernelUnsupportedOperationError(
                    f"当前 BaseRuntime 暂不支持该任务切换动作：{task_switch_action}"
                )

    async def _change_agent_frame(
        self,
        owner_agent: BaseAgent,
        task: BaseProcessingTask,
        frame_change: BaseAgentFrameChange,
    ) -> None:
        """执行 Agent frame 的 push 或 pop，并在 push 后唤起目标 Agent。

        参数:
            owner_agent: 发起本次控制权变更的当前 Agent。
            task: 触发控制权变更的已完成任务。
            frame_change: 包含来源、目标与变更类型的稳定 frame 变更产物。

        返回:
            无返回值。

        异常:
            KernelRuntimeError: 当前控制权栈顶与 frame 变更来源不一致，或变更类型不支持时抛出。

        说明:
            handoff 不会向目标 Agent 重复投递原始用户文本。目标 Agent 接收空 user input，
            由上下文投影将来源 Agent 已形成的历史收敛为 Group Input 注入当前任务。
        """

        if self.session_state.agent_frames[-1].agent_name != frame_change.source:
            raise KernelRuntimeError(
                "当前 agent frame 不匹配："
                f"source={frame_change.source}, "
                f"top_frame={self.session_state.agent_frames[-1].agent_name}"
            )

        if frame_change.type == AgentFrameChangeType.PUSH:
            self.session_state.agent_frames.append(
                AgentFrame(
                    agent_name=frame_change.target,
                    from_agent_name=self.session_state.agent_frames[-1].agent_name,
                )
            )

            # handoff 只重新触发目标 Agent，不重复投递用户原始文本。
            source_input = task.start_input
            if source_input:
                await self._push_input(
                    Input(
                        input_id=generate_prefixed_id(PREFIX_INPUT_ID),
                        origin_input_id=source_input.input_id,
                        events=[
                            ObservableEvent(
                                event_id=generate_prefixed_id(
                                    PREFIX_OBSERVABLE_EVENT_ID
                                ),
                                event_type=EventType.USER_INPUT,
                                source=EventSource.HANDOFF,
                                payload=UserInputPayload(),
                            )
                        ],
                    )
                )
        elif frame_change.type == AgentFrameChangeType.POP:
            self.session_state.agent_frames.pop()
        else:
            raise KernelRuntimeError(f"不支持的 frame_change.type：{frame_change.type}")

        # 通知 frame change 完成
        await self._override_service_agent_frame_did_change(
            owner_agent=owner_agent,
            source_task=task,
            frame_change=frame_change,
        )

    def _pop_agent_frame(self):
        self.session_state.agent_frames.pop()

    def _pick_next_execution_unit(self, tool_state: ToolState) -> ExecutionUnit | None:
        """从 `ToolState` 中选出下一个可推进的执行单元。

        参数:
            tool_state: 当前工具循环任务状态。

        返回:
            第一个处于 `pending` 的执行单元；若不存在则返回 `None`。
        """

        for execution_unit in tool_state.execution_units:

            # 等待执行的 unit
            if execution_unit.status == ExecutionUnitStatus.PENDING:
                return execution_unit
            
        return None

    def _clear_interruption_request(self, task: BaseProcessingTask):
        if task.state.status != TaskStatus.PAUSED:
            task.interruption_request = None
