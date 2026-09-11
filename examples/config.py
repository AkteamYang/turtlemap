#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/03 18:31
# @Author  : YaHaoo
# @File    : config.py

"""examples 业务层基础设施配置。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MysqlConfig(BaseSettings):
    """表示 MySQL 连接配置。

    说明:
        该配置属于 examples 业务存储基础设施，不属于 turtlemap SDK。
        它只描述连接所需的基础参数，连接池大小与 migration 策略由 driver 管理。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # MySQL 服务地址。
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("TURTLEMAP_MYSQL_HOST", "MYSQL_HOST"),
    )

    # MySQL 服务端口。
    port: int = Field(
        default=3306,
        validation_alias=AliasChoices("TURTLEMAP_MYSQL_PORT", "MYSQL_PORT"),
    )

    # MySQL 登录用户名。
    user: str = Field(
        default="root",
        validation_alias=AliasChoices("TURTLEMAP_MYSQL_USER", "MYSQL_USER"),
    )

    # MySQL 登录密码。
    password: str = Field(
        default="",
        validation_alias=AliasChoices("TURTLEMAP_MYSQL_PASSWORD", "MYSQL_PASSWORD"),
    )

    # 当前使用的数据库名称。
    database: str = Field(
        default="turtlemap",
        validation_alias=AliasChoices("TURTLEMAP_MYSQL_DATABASE", "MYSQL_DATABASE"),
    )

    # MySQL 连接字符集。
    charset: str = Field(
        default="utf8mb4",
        validation_alias=AliasChoices("TURTLEMAP_MYSQL_CHARSET", "MYSQL_CHARSET"),
    )

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "MysqlConfig":
        """从环境变量和 `.env` 文件构建 MySQL 连接配置。

        参数:
            env_path: `.env` 文件路径；为空时读取当前工作目录下的 `.env`。

        返回:
            已解析完成的 MySQL 连接配置。
        """

        # 业务入口显式传入且文件存在时，才把它作为 `.env` 来源。
        if env_path is not None and Path(env_path).exists():
            return cls(**cast(Any, {"_env_file": env_path}))

        return cls()


class RedisConfig(BaseSettings):
    """表示 Redis 连接配置。

    说明:
        该配置属于 examples 业务存储基础设施，不属于 turtlemap SDK。
        它只描述短期运行流缓存连接所需的基础参数。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Redis 服务地址。
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("TURTLEMAP_REDIS_HOST", "REDIS_HOST"),
    )

    # Redis 服务端口。
    port: int = Field(
        default=6379,
        validation_alias=AliasChoices("TURTLEMAP_REDIS_PORT", "REDIS_PORT"),
    )

    # Redis 登录用户名；Redis 未启用 ACL 时可为空。
    username: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TURTLEMAP_REDIS_USERNAME", "REDIS_USERNAME"),
    )

    # Redis 登录密码；未配置密码时可为空。
    password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TURTLEMAP_REDIS_PASSWORD", "REDIS_PASSWORD"),
    )

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "RedisConfig":
        """从环境变量和 `.env` 文件构建 Redis 连接配置。

        参数:
            env_path: `.env` 文件路径；为空时读取当前工作目录下的 `.env`。

        返回:
            已解析完成的 Redis 连接配置。
        """

        # 业务入口显式传入且文件存在时，才把它作为 `.env` 来源。
        if env_path is not None and Path(env_path).exists():
            return cls(**cast(Any, {"_env_file": env_path}))

        return cls()
