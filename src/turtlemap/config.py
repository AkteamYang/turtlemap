#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 14:13
# @Author  : YaHaoo
# @File    : config.py

"""turtlemap 应用配置加载入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(slots=True)
class LLMConfig:
    """表示默认 LLM 客户端配置。

    说明:
        该配置承接 OpenAI 兼容接口的最小参数。环境变量读取时会优先使用
        `TURTLEMAP_LLM_*` 命名空间，并兼容现有 OpenAI 风格变量。
    """

    # 当前调用使用的模型名称。
    model: str

    # 当前模型服务的访问密钥。
    api_key: str

    # 当前模型服务的基础地址；为空时使用客户端默认地址。
    base_url: str | None = None

    # 当前请求超时时间，单位秒。
    timeout: float = 120.0


@dataclass(slots=True)
class ContextTokenBudget:
    """表示单个 Agent 的上下文 token 预算配置。

    说明:
        该配置由应用配置统一承载，用于指导上下文构建完成后的 token
        预算判断。当前阶段只承接阈值定义，不直接承担压缩策略。
    """

    # 当前模型允许使用的最大上下文窗口。
    max_context_tokens: int

    # 超过后当前轮不能直接发模型、必须先同步压缩的硬限制。
    hard_limit_tokens: int

    # 超过后允许本轮继续，但应触发后台压缩的软限制。
    soft_limit_tokens: int

    # 超过后本轮必须压缩，为空时不按轮次触发。
    hard_limit_rounds: int | None = None

    # 超过后允许本轮继续，但应触发后台压缩的历史轮次软限制；为空时不按轮次触发。
    soft_limit_rounds: int | None = None

    # 每轮压缩后仍保留原始形态的最近历史轮次数。
    keep_recent_history_rounds: int = 5

    @classmethod
    def default(cls) -> "ContextTokenBudget":
        """构建默认上下文 token 预算配置。

        返回:
            面向默认模型配置的保守 token 预算对象。
        """

        return cls(
            max_context_tokens=32_000,
            hard_limit_tokens=30_000,
            soft_limit_tokens=24_000,
        )

    def __post_init__(self) -> None:
        """校验 token 预算配置边界。

        说明:
            当前约束要求三个 token 阈值都为正整数，且满足
            `soft_limit_tokens <= hard_limit_tokens <= max_context_tokens`。
        """

        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens 必须为正整数")

        if self.hard_limit_tokens <= 0:
            raise ValueError("hard_limit_tokens 必须为正整数")

        if self.soft_limit_tokens <= 0:
            raise ValueError("soft_limit_tokens 必须为正整数")

        if self.soft_limit_rounds is not None and self.soft_limit_rounds < 1:
            raise ValueError("soft_limit_rounds 不能小于 0")

        if self.hard_limit_rounds is not None and self.hard_limit_rounds < 1:
            raise ValueError("hard_limit_rounds 不能小于 0")

        if self.keep_recent_history_rounds < 0:
            raise ValueError("keep_recent_history_rounds 不能小于 0")


class _TurtleMapEnvSettings(BaseSettings):
    """表示 SDK 从环境变量读取的扁平配置。

    说明:
        该类只作为 `TurtleMapConfig.from_env` 的解析边界，不向 SDK 用户暴露。
        对外仍返回轻量 dataclass，避免配置解析框架侵入 Runtime 普通对象。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # 当前调用使用的模型名称。
    llm_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("TURTLEMAP_LLM_MODEL", "TURTLEMAP_MODEL"),
    )

    # 当前模型服务的访问密钥。
    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("TURTLEMAP_LLM_API_KEY", "OPENAI_API_KEY"),
    )

    # 当前模型服务的基础地址；空字符串会归一化为 None。
    llm_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TURTLEMAP_LLM_BASE_URL", "OPENAI_BASE_URL"),
    )

    # 当前请求超时时间，单位秒。
    llm_timeout: float = Field(
        default=60.0,
        validation_alias=AliasChoices("TURTLEMAP_LLM_TIMEOUT"),
    )

    # 当前模型允许使用的最大上下文窗口。
    context_max_context_tokens: int = Field(
        default=32_000,
        validation_alias=AliasChoices("TURTLEMAP_CONTEXT_MAX_CONTEXT_TOKENS"),
    )

    # 超过后当前轮不能直接发模型、必须先同步压缩的硬限制。
    context_hard_limit_tokens: int = Field(
        default=30_000,
        validation_alias=AliasChoices("TURTLEMAP_CONTEXT_HARD_LIMIT_TOKENS"),
    )

    # 超过后允许本轮继续，但应触发后台压缩的软限制。
    context_soft_limit_tokens: int = Field(
        default=24_000,
        validation_alias=AliasChoices("TURTLEMAP_CONTEXT_SOFT_LIMIT_TOKENS"),
    )

    # 超过后允许本轮继续，但应触发后台压缩的历史轮次软限制。
    context_soft_limit_rounds: int | None = Field(
        default=None,
        validation_alias=AliasChoices("TURTLEMAP_CONTEXT_SOFT_LIMIT_ROUNDS"),
    )

    # 超过后必须压缩。
    context_hard_limit_rounds: int | None = Field(
        default=None,
        validation_alias=AliasChoices("TURTLEMAP_CONTEXT_HARD_LIMIT_ROUNDS"),
    )

    # 每轮压缩后仍保留原始形态的最近历史轮次数。
    context_keep_recent_history_rounds: int = Field(
        default=5,
        validation_alias=AliasChoices("TURTLEMAP_CONTEXT_KEEP_RECENT_HISTORY_ROUNDS"),
    )

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def _normalize_optional_string(cls, value: object) -> object:
        """归一化允许为空的字符串配置。

        参数:
            value: pydantic 校验前的原始配置值。

        返回:
            空字符串归一化为 None，其他值交给 pydantic 后续校验。
        """

        if value == "":
            return None
        return value


@dataclass(slots=True)
class TurtleMapConfig:
    """表示 turtlemap 应用级配置集合。

    说明:
        SDK 配置只承载 Runtime 通用参数，例如 LLM 与上下文预算。
        具体业务基础设施配置应由业务库自行定义，避免 SDK 反向依赖示例存储实现。
    """

    # 默认 LLM 客户端配置。
    llm: LLMConfig

    # 默认上下文 token 预算配置。
    context_token_budget: ContextTokenBudget

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "TurtleMapConfig":
        """从环境变量和 `.env` 文件构建应用配置。

        参数:
            env_path: `.env` 文件路径；为空时读取当前工作目录下的 `.env`。

        返回:
            已解析完成的应用级配置对象。

        说明:
            若 `.env` 文件存在，则读取文件中的键值作为兜底配置，但不会写回
            或覆盖同名系统环境变量；系统环境变量始终优先，便于同时支持本地
            文件配置和容器、CI 等系统环境注入。
        """

        # 显式传入且文件存在时，才把它作为 `.env` 来源；不存在时直接读真实环境变量。
        if env_path is not None and Path(env_path).exists():
            settings = _TurtleMapEnvSettings(**cast(Any, {"_env_file": env_path}))
        else:
            settings = _TurtleMapEnvSettings()

        return cls(
            llm=LLMConfig(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
            ),
            context_token_budget=ContextTokenBudget(
                max_context_tokens=settings.context_max_context_tokens,
                hard_limit_tokens=settings.context_hard_limit_tokens,
                soft_limit_tokens=settings.context_soft_limit_tokens,
                hard_limit_rounds=settings.context_hard_limit_rounds,
                soft_limit_rounds=settings.context_soft_limit_rounds,
                keep_recent_history_rounds=settings.context_keep_recent_history_rounds,
            ),
        )
