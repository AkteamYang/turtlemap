#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/07 10:23
# @Author  : YaHaoo
# @File    : models.py

"""kernel 层核心数据结构。"""

from __future__ import annotations

import time
from typing import Any, TypeAlias, Union

from pydantic import Field, SerializeAsAny, ValidationInfo, field_validator

from turtlemap.kernel.exceptions import KernelRuntimeError
from turtlemap.shared.ids import PREFIX_TASK_ID, generate_prefixed_id

from .enums import (
    EventSource,
    EventType,
    InterruptionRequestType,
    MessageRole,
    RuntimeArtifactType,
    TaskStateKind,
    TaskStatus,
    AgentFrameChangeType,
    AgentFrameChangeReason
)
from .polymorphic import BaseStateModel, PolymorphicStateModel, ValidateFieldModel


class BaseAgentFrameChange(BaseStateModel):
    """表示一次 Agent 控制权栈变更的稳定产物。

    说明:
        `type` 描述控制权栈的 push 或 pop 动作，`reason` 描述触发该动作的
        业务语义。该产物同时作为任务状态与 event bus 通知的共享载荷。
    """

    # 变更后的目标 Agent 名称。
    target: str

    # 发起本次控制权栈变更的来源 Agent 名称。
    source: str

    # 控制权栈变更动作。
    type: AgentFrameChangeType

    # 控制权栈变更的业务原因。
    reason: AgentFrameChangeReason


from ..tool import ExecutionUnit


# 事件载荷会逐步从裸字典收敛为一组显式类型对象。
# 当前先明确 user_input 的最小结构，其余事件类型暂时保留 dict 兜底。
ObservableEventPayload: TypeAlias = Union[
    "UserInputPayload",
    "InterruptionResponsePayload",
    dict[str, object],
]


class ObservableEvent(ValidateFieldModel):
    """表示一次进入 kernel 的标准化事件。"""

    # 当前事件的唯一标识。
    event_id: str

    # 当前事件的业务类型。
    event_type: EventType

    # 当前事件的来源。
    source: EventSource

    # 当前事件携带的结构化载荷。
    payload: SerializeAsAny[ObservableEventPayload]

    # 业务侧透传元信息，kernel 不解析其内部语义。
    metadata: dict[str, object] = Field(default_factory=dict)

    # 事件来源对象的可选标识。
    source_id: str | None = None

    # 当前事件的处理优先级，数值越小优先级越高。
    priority: int = 100

    @field_validator("payload", mode="before")
    @classmethod
    def _validate_payload(cls, value: object, info: ValidationInfo) -> object:
        """根据 ObservableEvent.event_type 恢复 payload 的真实模型类型。

        参数:
            value: Pydantic 解析前的 payload 原始值。
            info: 当前字段解析上下文，用于读取已解析的 event_type。

        返回:
            若当前事件类型已注册 payload 模型，则返回对应模型实例；
            否则返回原值，由后续字段校验继续处理。
        """

        return cls.validate_field(
            field_name="payload",
            type_name=info.data.get("event_type"),
            value=value,
        )


@ObservableEvent.register_field_type("payload", EventType.USER_INPUT)
class UserInputPayload(BaseStateModel):
    """表示用户输入事件的标准载荷。"""

    # 当前用户输入的标准文本内容。
    content: str = ""


InterruptionResponse: TypeAlias = Union[
    "InterruptionExceptionResponse",
    "InterruptionAsyncToolResponse",
]

@ObservableEvent.register_field_type("payload", EventType.INTERRUPTION_RESPONSE)
class InterruptionResponsePayload(ValidateFieldModel):
    """表示恢复暂停任务的标准 ObservableEvent 载荷。"""

    # 待响应的中断请求唯一标识。
    request_id: str

    # 中断请求类型，仅用于用户展示与日志诊断，不参与请求匹配。
    request_type: InterruptionRequestType

    # 外部系统提交的结构化响应。
    response: SerializeAsAny[InterruptionResponse]

    @field_validator("response", mode="before")
    @classmethod
    def _validate_response(cls, value: object, info: ValidationInfo) -> object:
        """根据 request_type 恢复响应的具体模型。

        参数:
            value: Pydantic 解析前的响应原始值。
            info: 当前字段解析上下文，用于读取 request_type。

        返回:
            与请求类型对应的结构化响应对象。
        """

        return cls.validate_field(
            field_name="response",
            type_name=info.data.get("request_type"),
            value=value,
        )


class RuntimeArtifact(ValidateFieldModel):
    """表示 Runtime 流程中由 os 层生成的稳定产物引用。

    说明:
        kernel 只依赖产物类型进行流程控制，不解析 `payload` 的内部结构。
        `payload` 由 os 层按具体模型、协议或多模态载荷自行定义和消费。
    """
    # 产物id
    id: str = Field(default_factory=lambda: generate_prefixed_id("artifact"))

    # 当前产物类型，kernel 只根据该字段区分必要流程。
    type: RuntimeArtifactType

    # 当前产物所属 Agent 的名称，用于按 Agent 视角投影会话上下文。
    owner_agent_name: str = ""

    # os 层产物载荷，kernel 不解析其内部结构。
    payload: SerializeAsAny[Any]

    def is_user_role_message(self) -> bool:
        return self.type == RuntimeArtifactType.INPUT

    @field_validator("payload", mode="before")
    @classmethod
    def _validate_payload(cls, value: object, info: ValidationInfo) -> object:
        """根据 RuntimeArtifact.type 恢复 payload 的真实模型类型。

        参数:
            value: Pydantic 解析前的 payload 原始值。
            info: 当前字段解析上下文，用于读取已解析的 type。

        返回:
            若当前产物类型已注册 payload 模型，则返回对应模型实例；
            否则返回原值，由后续字段校验继续处理。

        说明:
            kernel 仍不理解 payload 业务字段，只提供一个按产物类型恢复
            os payload 模型的注册点，避免各处手写反序列化分支。
        """

        return cls.validate_field(
            field_name="payload",
            type_name=info.data.get("type"),
            value=value,
        )


@RuntimeArtifact.register_field_type(
    "payload", RuntimeArtifactType.INTERRUPTION_REQUEST
)
class InterruptionRequest(BaseStateModel):
    """表示挂载在暂停任务上的可持久化中断请求。

    说明:
        kernel 只依赖 request_id 完成响应匹配。完整响应 payload 会原样挂载到
        request，具体响应内容由对应任务执行阶段解释。
    """

    # 中断请求唯一标识，作为后续 response 的恢复锚点。
    request_id: str

    # 当前请求所属任务标识。
    task_id: str

    # 中断请求类型，可理解为恢复处理函数的稳定名称。
    type: str

    # assistant 消息展示的文本内容。
    assistant_content: str = Field(default="")

    # 恢复打断提示词，教模型如何恢复任务
    resume_prompt: str = Field(default="")

    # 原本的状态
    origin_task_status: TaskStatus

    created_ts_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    # 当前请求需要外部系统理解的结构化参数。
    params: dict[str, object] = Field(default_factory=dict)

    # 已匹配的完整响应载荷；为空表示仍在等待。
    response: InterruptionResponsePayload | None = None


@InterruptionResponsePayload.register_field_type(
    field_name="response",
    type_name=InterruptionRequestType.EXCEPTION_RESUME
)
class InterruptionExceptionResponse(BaseStateModel):
    """表示 Runtime 异常中断后的无参数恢复响应。"""


@InterruptionResponsePayload.register_field_type(
    field_name="response",
    type_name=InterruptionRequestType.ASYNC_TOOL_REQUEST
)
class InterruptionAsyncToolResponse(BaseStateModel):
    """表示异步工具完成后返回的标准工具结果。"""
    data: dict


# 中断响应使用前向引用，所有具体响应类型注册完成后主动构建序列化器。
# 否则通过持久化恢复得到的模型直接 model_dump() 时会保留 MockValSer 占位对象。
InterruptionResponsePayload.model_rebuild()
InterruptionRequest.model_rebuild()
# ObservableEvent.model_rebuild()


@RuntimeArtifact.register_field_type("payload", RuntimeArtifactType.INPUT)
class Input(BaseStateModel):
    """表示一次进入 Runtime 输入队列的输入包。"""

    # 当前输入包的唯一标识。
    input_id: str

    # handoff 输入关联的来源输入包标识；普通输入保持为空。
    origin_input_id: str = ""

    # 当前输入包包含的事件列表。
    events: list[ObservableEvent]


class SystemDefinition(PolymorphicStateModel):
    """表示 Agent 的结构化系统定义。"""

    type_name: str = "system_definition"

    # Agent 的基础角色描述。
    role: str = ""

    # Agent 的核心目标描述。
    objective: str = ""

    # Agent 运行时必须遵守的约束说明。
    constraints: str = ""

    # 期望输入格式说明。
    input_format: str = ""

    # 期望输出格式说明。
    output_format: str = ""

    # 帮助模型理解系统定义的示例文本列表。
    examples: list[str] = Field(default_factory=list)


SystemDefinition.register_type(SystemDefinition)


class AgentFrame(BaseStateModel):
    """表示当前会话控制权栈帧。"""

    # 当前栈帧对应的 Agent 名称。
    agent_name: str

    # 当前栈帧的来源 Agent 名称。
    from_agent_name: str | None = None


class MemoryView(BaseStateModel):
    """表示当前 Agent 面向当前会话和任务的分层记忆视图。

    说明:
        当前阶段长期记忆和中期记忆都采用结构化纯文本载体，
        由 `os` 层按业务约定维护章节结构，便于 LLM 重写、压缩与拼装。
    """

    # 长期稳定记忆文本，承接偏好、身份信息和项目背景等长期有效内容。
    long_term_memory: str = ""

    # 中期工作记忆文本，承接会话摘要、阶段总结与当前约束等可滚动重写内容。
    mid_term_memory: str = ""


class BaseTaskState(PolymorphicStateModel):
    """表示任务状态基类。

    说明:
        所有任务状态都必须显式包含任务类型与当前状态，
        便于 Runtime 在恢复时统一判断推进边界。
    """

    # 当前任务状态的类型标识。
    type_name: str

    # 当前任务的运行状态。
    status: TaskStatus = TaskStatus.RUNNING

    # 当前任务失败时的可解释错误信息。
    error: str | None = None

    @property
    def kind(self) -> TaskStateKind:
        """返回兼容运行期语义的任务状态类型。

        返回:
            当前任务状态的枚举类型。
        """

        return TaskStateKind(self.type_name)

    @property
    def is_running(self):
        return self.status != TaskStatus.PAUSED


@BaseTaskState.register_type
class MessageState(BaseTaskState):
    """表示 assistant 消息生成任务状态。"""

    # 当前任务状态固定为消息生成任务。
    type_name: str = Field(default=TaskStateKind.MESSAGE)

    # os 层生成并确认完成后的 Runtime 产物。
    message: RuntimeArtifact | None = None

    # 触发该 message 的输入 id。
    origin_input_id: str | None = None

@BaseTaskState.register_type
class ToolState(BaseTaskState):
    """表示工具循环任务状态。"""

    # 当前任务状态固定为工具循环任务。
    type_name: str = Field(default=TaskStateKind.TOOL)

    # 进入当前 tool loop 时的起始 assistant tool call 消息。
    tool_call_message: RuntimeArtifact

    # 当前 tool loop 中累计的执行单元列表。
    execution_units: list[SerializeAsAny[ExecutionUnit]] = Field(default_factory=list)

    # 触发该 tool loop 的输入 id。
    origin_input_id: str | None = None

    @field_validator("execution_units", mode="before")
    @classmethod
    def _validate_execution_units(cls, value: object) -> object:
        """恢复工具任务中累计的多态执行单元列表。

        参数:
            value: pydantic 校验前的原始执行单元列表。

        返回:
            已按 `type_name` 恢复的执行单元列表，或原值。
        """

        if isinstance(value, list):
            return [
                ExecutionUnit.validate_polymorphic(item)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        return value


@BaseTaskState.register_type
class AutoResponseState(BaseTaskState):
    """表示自动响应等待状态。"""

    # 当前任务状态固定为自动响应等待任务。
    type_name: str = Field(default=TaskStateKind.AUTO_RESPONSE)

    # 期待触发的事件类型。
    trigger_event_type: EventType | None = None

    # 期待触发的来源对象 id。
    trigger_source_id: str | None = None

    # 自动响应目标。
    objective: str = ""

    # 自动响应任务默认暂停，等待匹配的外部事件显式恢复。
    status: TaskStatus = TaskStatus.PAUSED


class BaseProcessingTask(PolymorphicStateModel):
    """表示某个 Agent 当前未完成的任务现场。

    说明:
        任务现场除了持有当前状态外，也会保留触发该任务进入处理流程的原始输入，
        便于后续状态推进、恢复和工具链路继续读取任务起点上下文。
    """
    type_name: str = Field(default="base_processing_task")

    # 当前任务的唯一标识。
    task_id: str = Field(default_factory=lambda: generate_prefixed_id(PREFIX_TASK_ID))

    # 当前任务绑定的具体状态对象。
    state: SerializeAsAny[BaseTaskState]

    # 启动当前任务时消费的输入包；任务生命周期内固定不变。
    start_input: Input | None = None

    # 当前任务挂载的中断请求；一次暂停只对应一个请求，具体请求内容由业务层表达。
    interruption_request: InterruptionRequest | None = None

    # 任务完成后需要执行的 Agent 控制权栈变更；为空表示保持当前控制权。
    agent_frame_change: BaseAgentFrameChange | None = None

    @field_validator("state", mode="before")
    @classmethod
    def _validate_state(cls, value: object) -> object:
        """恢复任务现场中的多态任务状态。

        参数:
            value: pydantic 校验前的原始任务状态。

        返回:
            已按 `type_name` 恢复的任务状态，或原值。
        """

        if value is None or isinstance(value, BaseTaskState):
            return value
        if isinstance(value, dict):
            return BaseTaskState.validate_polymorphic(value)
        return value


BaseProcessingTask.register_type(BaseProcessingTask)


class BaseAgentState(PolymorphicStateModel):
    """表示某个 Agent 在当前会话中的主观状态。

    说明:
        BaseAgentState 支持多态恢复。os 层或插件层可以声明自己的 AgentState
        子类并注册到该基类，SessionState 反序列化时会按 `type_name`
        恢复真实状态类型。
    """

    # 当前 AgentState 多态类型名，用于 SessionState 恢复真实子类。
    type_name: str = Field(default="base_agent_state")

    # 当前主观状态对应的 Agent 名称。
    agent_name: str

    # 当前 Agent 的系统定义快照。
    system: SerializeAsAny[SystemDefinition]

    # 当前 Agent 尚未完成的任务列表。
    # 待用户确认，但是却切换到其他话题
    processing_tasks: list[SerializeAsAny[BaseProcessingTask]] = Field(default_factory=list)

    @field_validator("system", mode="before")
    @classmethod
    def _validate_system(cls, value: object) -> object:
        """恢复 AgentState 中保存的多态系统定义。

        参数:
            value: pydantic 校验前的 system 原始值。

        返回:
            已按 `type_name` 恢复真实子类的 SystemDefinition，或原始值。
        """

        if value is None or isinstance(value, SystemDefinition):
            return value
        if isinstance(value, dict):
            # system 可能是 os 层扩展的 SystemInstruction，需要按注册类型恢复。
            return SystemDefinition.validate_polymorphic(value)
        return value

    @field_validator("processing_tasks", mode="before")
    @classmethod
    def _validate_processing_tasks(cls, value: object) -> object:
        """恢复 AgentState 中保存的多态任务现场列表。

        参数:
            value: pydantic 校验前的任务列表原始值。

        返回:
            列表中的任务字典会按 `type_name` 恢复为具体任务子类，其他输入
            保持原值交给 pydantic 后续校验。
        """

        if value is None:
            return value
        if isinstance(value, list):
            # 任务可能是 os 层扩展的 ProcessingTask，需要按注册类型恢复。
            return [
                BaseProcessingTask.validate_polymorphic(item)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        return value


class BaseSessionState(BaseStateModel):
    """表示 kernel 层最小会话客观现场。

    说明:
        该模型只承接 runtime 闭环恢复所需的最小字段，不直接吸收明显偏业务层、
        回放层或产品层的会话扩展参数。若 os 层需要额外会话语义，应优先通过
        派生模型扩展，而不是持续膨胀 kernel 基础模型。历史产物与记忆视图
        归属于会话。
    """

    # 当前会话的 Agent 控制权栈。
    agent_frames: list[AgentFrame] = Field(default_factory=list)

    # 尚未被 Runtime 接管处理的输入队列。
    input_queue: list[Input] = Field(default_factory=list)

    # 当前会话按时间顺序保存的稳定 Runtime 产物，用于所有 Agent 的上下文投影。
    history: list[RuntimeArtifact] = Field(default_factory=list)

    # 当前会话的记忆视图，承接长期记忆运行视图与中期会话摘要。
    memory: MemoryView = Field(default_factory=MemoryView)

    # 当前会话已加载的 BaseAgentState 映射，作为 Runtime 恢复所需完整现场随 SessionState 保存。
    agent_name2agent_state: dict[str, SerializeAsAny[BaseAgentState]] = Field(default_factory=dict)

    @field_validator("agent_name2agent_state", mode="before")
    @classmethod
    def _validate_agent_name2agent_state(cls, value: object) -> object:
        """恢复 SessionState 中随会话保存的多态 AgentState 映射。

        参数:
            value: pydantic 校验前的 agent_name 到 BaseAgentState 原始映射。

        返回:
            已按 `type_name` 恢复真实子类的映射，或原始空值。
        """

        if value is None:
            return {}
        if isinstance(value, dict):
            return {
                agent_name: agent_state
                if isinstance(agent_state, BaseAgentState)
                else BaseAgentState.validate_polymorphic(agent_state)
                for agent_name, agent_state in value.items()
            }
        return value

    def top_agent_state(self) -> BaseAgentState:
        frame = self.agent_frames[-1]
        if frame.agent_name not in self.agent_name2agent_state:
            raise KernelRuntimeError(f"未找到 agent_state, agent_name {frame.agent_name}")
        
        state = self.agent_name2agent_state[frame.agent_name]
        return state

    def clean_state(self):
        self.input_queue = []
        top_agent_state = self.top_agent_state()
        top_agent_state.processing_tasks = [
            task
            for task in top_agent_state.processing_tasks
            if not task.state.is_running
        ]


# BaseAgentState 本身也作为可恢复类型注册，兼容只使用 kernel 基类的状态快照。
BaseAgentState.register_type(BaseAgentState)
