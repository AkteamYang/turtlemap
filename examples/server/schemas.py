#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/02 18:26
# @Author  : YaHaoo
# @File    : schemas.py

"""示例服务端 API 与 SSE 协议模型。"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from turtlemap.kernel.models.enums import EventType, InterruptionRequestType
from turtlemap.kernel.models.models import InterruptionResponsePayload
from turtlemap.os.context.models import ContextCompressionLevel

JsonDict = dict[str, Any]


class ApiCode(IntEnum):
    """表示服务端统一响应 code。

    约束:
        枚举值对外序列化为整数；SSE start 事件也复用本枚举表达前端操作语义。
    """

    # 请求成功。
    OK = 0

    # 已落历史或无法续接，前端刷新页面。
    RELOAD_PAGE = 20003

    # 请求参数不合法。
    INVALID_ARGUMENT = 40000

    # 会话不存在或已删除。
    SESSION_NOT_FOUND = 40001

    # Runtime 执行失败。
    RUNTIME_ERROR = 50001

    # 流超时
    STREAM_TIMEOUT = 60001

    # 流超不完整
    STREAM_INCOMPLETE = 60002


class SseEventType(str, Enum):
    """表示服务端对外发送的 SSE event 类型。

    约束:
        枚举值必须与前端 EventSource 监听的 event 名称保持一致。
    """

    # 连接建立后的首个控制事件，data 使用 ApiResponse 结构。
    START = "start"

    # 实时 Runtime 投影事件。
    CHUNK = "chunk"

    # 从短期 stream 中恢复补发的历史流事件。
    STREAM_CHUNK = "stream_chunk"

    # 空闲保活事件。
    HEARTBEAT = "heartbeat"

    # 当前 SSE 流正常结束事件。
    END = "end"

    # 当前 SSE 流错误事件。
    ERROR = "error"


class ServerMessageEventType(str, Enum):
    """表示前端消费的业务消息事件类型。

    约束:
        该枚举用于 `event: chunk` / `event: stream_chunk` 的 data.type 字段，
        枚举值必须与前端渲染分支保持一致。
    """

    # 服务端确认收到用户输入。
    INPUT = "input"

    # 通用 loading 提示。
    ACTIVITY_INDICATOR = "activity_indicator"

    # assistant 文本增量。
    MESSAGE_DELTA = "message_delta"

    # assistant 稳定最终文本。
    MESSAGE_FINAL = "message_final"

    # 模型稳定选择工具。
    TOOL_CALL = "tool_call"

    # 工具开始执行。
    TOOL_RESULT_START = "tool_result_start"

    # 工具执行稳定结果。
    TOOL_RESULT = "tool_result"

    # 上下文压缩开始事件。
    CONTEXT_COMPRESSION_START = "context_compression_start"

    # 上下文压缩最终结果事件。
    CONTEXT_COMPRESSION_FINAL = "context_compression_final"

    # 任务暂停并等待外部响应。
    INTERRUPTED = "interrupted"

    # 本次 agent 生成业务完成。
    COMPLETE = "complete"


class InputEventData(BaseModel):
    """表示 input 业务事件载荷。"""

    # Runtime 分配的输入 id。
    input_id: str

    # 输入包首个 ObservableEvent 的业务类型。
    event_type: EventType

    # 用户输入文本。
    user_input: str

    # 中断恢复输入携带的结构化响应；普通用户输入时为空。
    interruption_response: InterruptionResponsePayload | None = None

    # 前端幂等 id。
    client_event_id: str | None = None


class ActivityIndicatorData(BaseModel):
    """表示 activity_indicator 业务事件载荷。"""

    # 前端展示图标。
    icon: str

    # loading 主标题。
    title: str

    # loading 副标题。
    subtitle: str | None = None


class MessageDeltaData(BaseModel):
    """表示 message_delta 业务事件载荷。"""

    # assistant 文本增量。
    delta_content: str


class MessageFinalData(BaseModel):
    """表示 message_final 业务事件载荷。"""

    # assistant 消息生成耗时，单位毫秒。
    duration_ms: int = 0

    # assistant 稳定最终文本。
    content: str

    # 模型返回的推理文本；模型不支持时为空。
    reasoning_content: str | None = None

    # 模型完成原因。
    finish_reason: str | None = None

    # 模型调用用量。
    usage: JsonDict | None = None


class ToolCallData(BaseModel):
    """表示 tool_call 业务事件载荷。"""

    # LLM 工具调用 id。
    id: str

    # 工具名称。
    name: str | None = None

    # JSON 结构化工具参数；解析失败时为空。
    parameters: JsonDict | None = None

    # 原始工具参数文本；仅在 JSON 解析失败时返回。
    parameters_text: str | None = None


class ToolResultData(BaseModel):
    """表示 tool_result 业务事件载荷。"""

    # 工具执行耗时，单位毫秒。
    duration_ms: int = 0

    # LLM 工具调用 id。
    tool_call_id: str

    # 工具执行状态。
    status: str

    # 工具返回给模型的文本内容。
    content: str

    # 工具原始结果，SDK 技术展示项目直接返回给前端。
    raw_data: Any = None


class ToolResultStartData(ToolCallData):
    """表示 tool_result_start 业务事件载荷。

    约束:
        工具启动阶段尚未产生稳定结果，载荷结构与 tool_call 一致，用于前端展示
        工具名称和参数。
    """


class ContextCompressionData(BaseModel):
    """表示 context_compression 业务事件载荷。"""

    # 上下文压缩耗时，单位毫秒。
    duration_ms: int = 0

    # 压缩模式。
    mode: str

    # 是否已经合并压缩结果。
    merged: bool

    success: bool

    error: str

    # 压缩等级，用于观测同步硬限制兜底采取了哪一级策略。
    level: ContextCompressionLevel | None

    # 本次压缩涉及的历史条数。
    compressed_history_count: int

    compressed_mid_term_memory: str = ""


class InterruptedEventData(BaseModel):
    """表示 interrupted 业务事件载荷。"""

    # 等待外部响应的中断请求 id。
    request_id: str

    # 被暂停任务的 id。
    task_id: str

    # 中断请求类型。
    interruption_type: InterruptionRequestType

    # 中断原因，供前端展示与排障使用。
    reason: str

    # 外部系统处理当前中断所需的结构化参数。
    params: JsonDict


class CompleteData(BaseModel):
    """表示 complete 业务事件载荷。

    约束:
        complete 仅用于确认本次 agent 生成结束，除耗时字段外不携带额外字段。
    """

    # 本次 agent run 总耗时，单位毫秒。
    duration_ms: int = 0


ServerMessageEventData = (
    InputEventData
    | ActivityIndicatorData
    | MessageDeltaData
    | MessageFinalData
    | ToolCallData
    | ToolResultStartData
    | ToolResultData
    | ContextCompressionData
    | InterruptedEventData
    | CompleteData
)


class ApiResponse(BaseModel):
    """表示非 SSE 接口和 SSE start 控制事件的统一响应结构。"""

    # 业务状态码或 SSE 控制操作码。
    code: ApiCode = ApiCode.OK

    # 可读状态说明。
    message: str = "ok"

    # 当前请求或控制事件是否成功。
    success: bool = True

    # 具体响应数据。
    data: Any = None


class CompletionRequest(BaseModel):
    """表示 completion 请求体。

    参数:
        event_type: 本次输入的业务类型，当前支持用户输入与中断响应。
        query: 用户输入文本；仅 `user_input` 类型需要。
        interruption_response: 中断恢复响应；仅 `interruption_response` 类型需要。
        last_event_id: 客户端已接收的最后一个 SSE id；为空表示从起点续接。
        client_event_id: 前端生成的幂等 id，重试时必须保持不变。
    """

    # 本次输入的业务类型，默认普通用户输入。
    event_type: EventType = EventType.USER_INPUT

    # 用户输入文本；中断恢复输入时为空。
    query: str | None = None

    # 中断恢复的结构化响应；普通用户输入时为空。
    interruption_response: InterruptionResponsePayload | None = None

    # 客户端已收到的最后一个 SSE id。
    last_event_id: str | None = None

    # 前端生成的幂等 id；所有 completion 输入类型都需要。
    client_event_id: str

    @model_validator(mode="after")
    def _validate_input_payload(self) -> "CompletionRequest":
        """校验不同 completion 输入类型对应的请求字段。

        返回:
            已通过输入类型约束校验的请求对象。

        异常:
            ValueError: 输入类型不受支持或其必要字段缺失时抛出。
        """

        if self.event_type == EventType.USER_INPUT:
            if not self.query or not self.query.strip():
                raise ValueError("user_input 类型的 query 不能为空")
            if not self.client_event_id:
                raise ValueError("user_input 类型的 client_event_id 不能为空")
            if self.interruption_response is not None:
                raise ValueError("user_input 类型不能携带 interruption_response")
            return self

        if self.event_type == EventType.INTERRUPTION_RESPONSE:
            if self.interruption_response is None:
                raise ValueError(
                    "interruption_response 类型必须携带 interruption_response"
                )
            if self.query is not None:
                raise ValueError("interruption_response 类型的 query 必须为 null")
            if not self.client_event_id:
                raise ValueError("interruption_response 类型的 client_event_id 不能为空")
            return self

        raise ValueError(f"completion 不支持输入类型：{self.event_type.value}")


class CreateSessionRequest(BaseModel):
    """表示创建会话请求体。"""

    # 会话展示标题。
    title: str = "新的会话"


class ServerMessageEvent(BaseModel):
    """表示前端消费的标准业务事件。"""

    # 前端消费的事件类型。
    type: ServerMessageEventType

    # 当前会话 id。
    session_id: str

    # 标识一次完整 agent 运行。
    run_id: str

    # 当前任务 id。
    task_id: str | None = None

    # Runtime 逻辑事件链 id。
    event_id: str

    # 父级事件链 id。
    parent_event_id: str | None = None

    # 当前事件投影时间，毫秒时间戳。
    start_ts_ms: int

    # 是否为 Runtime 恢复时补发的历史事件。
    is_history_event: bool = False

    # 事件载荷。
    data: ServerMessageEventData = Field(default_factory=CompleteData)


class SessionResponse(BaseModel):
    """表示会话接口响应数据。"""

    # 会话唯一标识。
    session_id: str

    # 会话展示标题。
    title: str

    # 创建时间毫秒时间戳。
    created_at: int | None = None

    # 更新时间毫秒时间戳。
    updated_at: int | None = None


class UserInfoResponse(BaseModel):
    """表示示例服务端写死的用户展示信息。"""

    # 用户头像地址或资源标识。
    avatar: str

    # 用户展示名称。
    name: str


class SessionInfoResponse(BaseModel):
    """表示页面初始化需要的会话聚合信息。"""

    # 最近活动的会话；没有会话时为空。
    active_session: SessionResponse | None = None

    # 会话列表，按创建时间排序。
    sessions: list[SessionResponse]


class AppInfoResponse(BaseModel):
    """表示页面初始化接口响应数据。"""

    # 当前示例项目的用户信息。
    user_info: UserInfoResponse

    # 当前示例项目的会话信息。
    session_info: SessionInfoResponse
