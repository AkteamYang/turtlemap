#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/03 08:28
# @Author  : YaHaoo
# @File    : ids.py

"""通用业务 id 生成工具。"""

from __future__ import annotations

from hashlib import sha256
from socket import gethostname
from uuid import uuid4

PREFIX_OBSERVABLE_EVENT_ID = "observable_event_id"
PREFIX_EVENT_ID = "event_id"
PREFIX_SESSION_ID = "session_id"
PREFIX_TASK_ID = "task_id"
PREFIX_EXCUTION_UNIT_ID = "excution_unit_id"
PREFIX_INPUT_ID = "input_id"
PREFIX_TOOL_ID = "tool_id"
PREFIX_TOOL_CALL_ID = "tool_call_id"
PREFIX_SUBSCRIBE_ID = "subscribe_id"
PREFIX_EVENT_BUS_ID = "event_bus_id"
PREFIX_RUN_ID = "run_id"
PREFIX_INTERRUPTION_REQUEST_ID = "interruption_request_id"


def build_executor_instance_id() -> str:
    """生成单次任务执行链路使用的执行令牌。

    返回:
        形如 `hostname:xxxxxxxxxxxx` 的执行标识。

    说明:
        该标识表示一次具体的任务执行尝试；首次触发和补偿重试都应重新生成，
        避免同一实例内旧执行和新执行共用同一个 owner。
    """

    return f"{gethostname()[:80]}:{uuid4().hex[:12]}"


def generate_prefixed_id(prefix: str) -> str:
    """生成统一格式的业务 id。

    参数:
        prefix: 业务 id 前缀，例如 `session`、`task`、`input`。

    返回:
        符合 `prefix_uuid` 格式的业务 id。
    """

    return f"{prefix}:{uuid4()}"


def generate_content_hash(content: str) -> str:
    """生成文本内容的稳定哈希值。

    参数:
        content: 当前待生成指纹的文本内容。

    返回:
        基于 UTF-8 编码内容生成的 sha256 十六进制字符串。
    """

    return sha256(content.encode("utf-8")).hexdigest()
