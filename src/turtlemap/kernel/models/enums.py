#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/02 14:23
# @Author  : YaHaoo
# @File    : enums.py

"""kernel 层核心枚举定义。"""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    """事件调度类型。"""

    # 来自用户的新输入事件
    USER_INPUT = "user_input"

    # 来自 handoff 完成结果的回流事件
    HANDOFF_RESULT = "handoff_result"

    # 来自外部环境或系统观测的消息事件
    ENVIRONMENT_MESSAGE = "environment_message"

    # 用于向暂停任务提交结构化恢复响应。
    INTERRUPTION_RESPONSE = "interruption_response"


class EventSource(str, Enum):
    """事件来源类型。"""

    # 来自用户侧
    USER = "user"

    # 来自工具侧
    TOOL = "tool"

    # 来自 handoff 链路
    HANDOFF = "handoff"

    # 来自系统内部
    SYSTEM = "system"

    # 来自环境或外部系统
    ENVIRONMENT = "environment"


class TaskStateKind(str, Enum):
    """任务状态判别类型。"""

    # 表示消息生成任务
    MESSAGE = "message"

    # 表示工具循环任务
    TOOL = "tool"

    # 表示 handoff 等待任务
    HANDOFF = "handoff"

    # 表示自动响应任务
    AUTO_RESPONSE = "auto_response"


class TaskStatus(str, Enum):
    """任务生命周期状态。"""

    # 正在执行
    RUNNING = "running"

    # 已暂停，等待后续显式恢复
    PAUSED = "paused"

    # 已执行完成
    COMPLETED = "completed"


class ExecutionUnitStatus(str, Enum):
    """执行单元生命周期状态。"""

    # 等待
    PENDING = "pending"

    # 执行中
    RUNNING = "running"

    # 执行完成
    COMPLETED = "completed"

    # 执行失败
    FAILED = "failed"


class ToolExecutionStrategy(str, Enum):
    """工具执行等待策略。"""

    # 同步等待工具结果并继续推进当前任务。
    SYNC = "sync"

    # 异步提交工具任务，当前轮写入占位结果后暂停等待恢复。
    ASYNC = "async"


class ToolResultStatus(str, Enum):
    """工具函数执行结果状态。"""

    # 工具执行成功并已形成稳定结果
    SUCCESS = "success"

    # 工具执行失败并已形成稳定结果
    FAILED = "failed"

    # 工具已受理但尚未形成稳定结果
    PENDING = "pending"


class MessageRole(str, Enum):
    """LLM 与历史消息角色。"""

    # 系统消息
    SYSTEM = "system"

    # 开发者消息
    DEVELOPER = "developer"

    # 用户消息
    USER = "user"

    # assistant 消息
    ASSISTANT = "assistant"

    # 工具结果消息
    TOOL = "tool"


class RuntimeArtifactType(str, Enum):
    """Runtime 产物类型。"""

    # 用户输入产物，对应 payload 为 Input。
    INPUT = "input"

    # 普通 assistant 消息产物，对应 payload 为 LLMMessage。
    ASSISTANT_MESSAGE = "assistant_message"

    # assistant 触发工具调用的消息产物，对应 payload 为 LLMMessage。
    TOOL_CALL = "tool_call"

    # 工具调用执行单元产物，对应 payload 为 ToolCallExecutionUnit。
    TOOL_CALL_EXE = "tool_call_exe"

    # 工具执行后 continuation LLM 生成产物，对应 payload 为 LLMCallExecutionUnit。
    TOOL_LLM_RESPONSE = "tool_llm_response"

    # 任务暂停中断请求产物，对应 payload 为 InterruptionRequest。
    INTERRUPTION_REQUEST = "interruption_request"


class SessionStateSaveKind(str, Enum):
    """SessionState 保存语义类型。"""

    # 表示一次可恢复的运行期 checkpoint
    CHECKPOINT = "checkpoint"

    # 表示后台上下文压缩完成后的会话状态写回。
    BACKGROUND_CONTEXT_COMPRESSION = "background_context_compression"

    # 手动更新，通常用于规避redis缓存
    REFRESH = "refresh"


class TaskSwitchAction(str, Enum):
    """ExecutionUnitResult 触发的任务级切换动作。"""

    # 暂停当前任务并结束本次 run，等待中断 response 恢复。
    PAUSE = "pause"


class InterruptionRequestType(str, Enum):
    """表示 kernel 内置的任务中断请求类型。"""

    # Runtime 异常回退后等待显式恢复。
    EXCEPTION_RESUME = "_os_exception_resume"

    # 异步工具等待外部执行结果。
    ASYNC_TOOL_REQUEST = "_os_async_tool_request"
