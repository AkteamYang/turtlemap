#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved 
#
# @Time    : 2026/09/10 15:09
# @Author  : YaHaoo
# @File    : enums.py


from enum import Enum


class AsyncToolType(str, Enum):

    # Runtime 异常回退后等待显式恢复。
    HITL = "async_hitl"

    # 异步工具等待外部执行结果。
    NORMAL = "async_normal"


class BuiltinToolCapabilityCategory(str, Enum):
    """表示 OS 内置工具在能力目录中的稳定分类。"""

    # 会话记忆与历史信息检索能力。
    MEMORY = "memory"

    # Runtime 任务恢复等运行期管理能力。
    RUNTIME = "runtime"

    # 用户确认、审核等人工交互能力。
    HUMAN_INTERACTION = "human_interaction"
