#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 16:19
# @Author  : YaHaoo
# @File    : __init__.py

"""examples 业务层 MySQL 持久化实现包。"""

from .driver import (
    close_mysql_pool,
    get_mysql_pool,
    init_mysql,
    _load_sql_statements,
)
try:
    from ..config import MysqlConfig
except ImportError:
    # 兼容 `from mysql_store import ...` 的示例脚本导入方式。
    from config import MysqlConfig
from .models import (
    AgentLongTermMemoryRecord,
    MessageProjectionRecord,
    MessageProjectionRunRecord,
    SessionMetaRecord,
    SessionStateRecord,
)
from .repository import MySQLStateStoreRepository
from .serializer import StateSerializer
from .state_store import MySQLSessionState, MySQLStateStore

__all__ = [
    "AgentLongTermMemoryRecord",
    "close_mysql_pool",
    "get_mysql_pool",
    "init_mysql",
    "MessageProjectionRecord",
    "MessageProjectionRunRecord",
    "MysqlConfig",
    "MySQLSessionState",
    "MySQLStateStore",
    "MySQLStateStoreRepository",
    "SessionMetaRecord",
    "SessionStateRecord",
    "StateSerializer",
    "_load_sql_statements",
]
