#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/02 18:26
# @Author  : YaHaoo
# @File    : constants.py

"""示例服务端协议常量。"""

SCHEMA_VERSION = 3

SSE_TIMEOUT = 2*6

# SSE 空闲心跳兜底间隔，避免长时间无 token 输出时链路被中间层关闭。
SSE_HEARTBEAT_INTERVAL_SECONDS = 15

# 当前活跃任务过期时间（s）
STREAM_STORE_ACTIVE_RUN_EXPIRE = 10*60

# SSE 事件流(执行中)过期时间（s）
STREAM_STORE_STREAM_RUNNING_EXPIRE = 5*60

# SSE 事件流(完成)过期时间（s）
STREAM_STORE_STREAM_FINISH_EXPIRE = 15

# SSE 事件流每次读取个数
STREAM_STORE_READ_LIMIT = int(1e4)

# SSE 事件流读取超时（ms）
STREAM_STORE_READ_BLOCK_TIME = 30*1000

REDIS_EXPIRE_EXTEND = 15

REDIS_STREAM_MAX_LEN = 100_000
