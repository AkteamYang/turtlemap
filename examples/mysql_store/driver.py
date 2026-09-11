#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 16:19
# @Author  : YaHaoo
# @File    : driver.py

"""examples 业务层 MySQL 连接池与 migration 管理工具。"""

from __future__ import annotations

from pathlib import Path

import aiomysql
from aiomysql.pool import Pool

from turtlemap.shared.logger import Logger

try:
    from ..config import MysqlConfig
except ImportError:
    # 示例脚本会把 examples 放到 sys.path 后以顶层 mysql_store 包导入。
    from config import MysqlConfig

_mysql_pool: Pool | None = None
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


async def init_mysql(
    mysql_config: MysqlConfig | None = None,
) -> Pool:
    """初始化 MySQL 持久化基础设施。

    参数:
        mysql_config: MySQL 连接配置；未传时使用 `MysqlConfig.from_env()`。

    返回:
        已初始化或已存在的进程级 aiomysql 连接池对象。

    说明:
        该方法是服务启动阶段唯一需要调用的 MySQL 初始化入口，会先确保
        连接池存在，再执行表结构 migration。若初始化失败，异常会直接向上
        抛出，避免 Runtime 层掩盖启动依赖问题。
    """

    pool = await _init_mysql_pool(mysql_config=mysql_config)
    await _init_mysql_tables()
    return pool


def get_mysql_pool() -> Pool:
    """获取当前进程级 MySQL 连接池。

    返回:
        已初始化的 aiomysql 异步连接池对象。

    异常:
        RuntimeError: 连接池尚未初始化时抛出。
    """

    if _mysql_pool is None:
        raise RuntimeError("MySQL 连接池尚未初始化，请先调用 init_mysql")

    return _mysql_pool


async def close_mysql_pool() -> None:
    """关闭当前进程级 MySQL 连接池。

    返回:
        无返回值。

    说明:
        该函数应在应用退出阶段调用；关闭后可再次通过 `init_mysql`
        初始化新的连接池。
    """

    global _mysql_pool
    if _mysql_pool is None:
        Logger.logger.info("MySQL 连接池关闭跳过，当前连接池未初始化")
        return

    # aiomysql 关闭分为 close 与 wait_closed 两步，必须串行执行。
    _mysql_pool.close()
    await _mysql_pool.wait_closed()
    _mysql_pool = None
    Logger.logger.info("MySQL 连接池关闭完成")


async def _create_mysql_pool(
    mysql_config: MysqlConfig | None = None,
) -> Pool:
    """创建 aiomysql 异步连接池。

    参数:
        mysql_config: MySQL 连接配置；未传时使用 `MysqlConfig.from_env()`。

    返回:
        aiomysql 异步连接池对象。

    """

    config = mysql_config or MysqlConfig.from_env()
    return await aiomysql.create_pool(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        db=config.database,
        minsize=1,
        maxsize=4,
        autocommit=False,
        charset=config.charset,
    )


async def _init_mysql_pool(
    mysql_config: MysqlConfig | None = None,
) -> Pool:
    """初始化进程级 MySQL 连接池。

    参数:
        mysql_config: MySQL 连接配置；未传时使用业务层 MySQL 配置。

    返回:
        已初始化或已存在的 aiomysql 连接池对象。
    """

    global _mysql_pool
    if _mysql_pool is None:
        # 连接池作为进程级单例，只在启动阶段创建一次。
        _mysql_pool = await _create_mysql_pool(
            mysql_config=mysql_config,
        )
    return _mysql_pool


async def _init_mysql_tables() -> None:
    """初始化当前服务依赖的 MySQL 表结构。

    返回:
        无返回值。

    说明:
        该函数设计为启动时幂等执行，会按文件名顺序执行目录下所有 `.sql`
        文件。每个文件会被拆分为多条 SQL 语句，并在同一个连接事务中提交。
    """

    effective_pool = get_mysql_pool()
    migration_paths = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not migration_paths:
        Logger.logger.warning(
            f"MySQL migrations 目录为空，path={_MIGRATIONS_DIR}"
        )
        return

    async with effective_pool.acquire() as connection:
        await connection.rollback()
        try:
            async with connection.cursor() as cursor:
                for migration_path in migration_paths:
                    sql_statements = _load_sql_statements(migration_path)
                    if not sql_statements:
                        continue

                    Logger.logger.info(
                        f"开始执行 MySQL migration，file={migration_path.name}"
                    )
                    for sql in sql_statements:
                        await cursor.execute(sql)
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    Logger.logger.info(f"MySQL 表结构初始化完成，migration_count={len(migration_paths)}")


def _load_sql_statements(migration_path: Path) -> list[str]:
    """从 SQL 文件中按语句粒度加载 migration。

    参数:
        migration_path: migration SQL 文件路径。

    返回:
        去除空语句后的 SQL 语句列表。
    """

    sql_content = migration_path.read_text(encoding="utf-8")
    return [
        statement.strip() for statement in sql_content.split(";") if statement.strip()
    ]
