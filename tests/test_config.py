#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/13 14:16
# @Author  : YaHaoo
# @File    : test_config.py

"""应用配置加载测试。"""

from __future__ import annotations

import os

from turtlemap.config import TurtleMapConfig


def test_turtlemap_config_can_load_env_file(tmp_path, monkeypatch) -> None:
    """验证 TurtleMapConfig 可以从指定 `.env` 文件加载 SDK 配置。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "TURTLEMAP_LLM_MODEL=test-model",
                "TURTLEMAP_LLM_API_KEY=test-key",
                "TURTLEMAP_LLM_BASE_URL=http://llm.example/v1",
                "TURTLEMAP_LLM_TIMEOUT=12.5",
                "TURTLEMAP_CONTEXT_MAX_CONTEXT_TOKENS=10000",
                "TURTLEMAP_CONTEXT_HARD_LIMIT_TOKENS=9000",
                "TURTLEMAP_CONTEXT_SOFT_LIMIT_TOKENS=8000",
                "TURTLEMAP_CONTEXT_SOFT_LIMIT_ROUNDS=4",
                "TURTLEMAP_CONTEXT_KEEP_RECENT_HISTORY_ROUNDS=6",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("TURTLEMAP_LLM_MODEL", raising=False)
    monkeypatch.delenv("TURTLEMAP_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TURTLEMAP_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("TURTLEMAP_LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("TURTLEMAP_CONTEXT_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("TURTLEMAP_CONTEXT_HARD_LIMIT_TOKENS", raising=False)
    monkeypatch.delenv("TURTLEMAP_CONTEXT_SOFT_LIMIT_TOKENS", raising=False)
    monkeypatch.delenv("TURTLEMAP_CONTEXT_SOFT_LIMIT_ROUNDS", raising=False)
    monkeypatch.delenv("TURTLEMAP_CONTEXT_KEEP_RECENT_HISTORY_ROUNDS", raising=False)

    config = TurtleMapConfig.from_env(env_path=env_path)

    assert config.llm.model == "test-model"
    assert config.llm.api_key == "test-key"
    assert config.llm.base_url == "http://llm.example/v1"
    assert config.llm.timeout == 12.5
    assert config.context_token_budget.max_context_tokens == 10000
    assert config.context_token_budget.hard_limit_tokens == 9000
    assert config.context_token_budget.soft_limit_tokens == 8000
    assert config.context_token_budget.soft_limit_rounds == 4
    assert config.context_token_budget.keep_recent_history_rounds == 6


def test_turtlemap_config_prefers_real_environment_over_env_file(
    tmp_path, monkeypatch
) -> None:
    """验证系统环境变量优先级高于 `.env` 文件。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "TURTLEMAP_LLM_MODEL=file-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TURTLEMAP_LLM_MODEL", "env-model")

    config = TurtleMapConfig.from_env(env_path=env_path)

    assert config.llm.model == "env-model"


def test_turtlemap_config_loads_env_file_without_overwriting_os_environ(
    tmp_path, monkeypatch
) -> None:
    """验证 `.env` 文件只作为配置来源，不会覆盖或写回系统环境变量。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "TURTLEMAP_LLM_MODEL=file-model\n"
        "TURTLEMAP_LLM_API_KEY=file-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TURTLEMAP_LLM_MODEL", "env-model")
    monkeypatch.delenv("TURTLEMAP_LLM_API_KEY", raising=False)

    config = TurtleMapConfig.from_env(env_path=env_path)

    assert config.llm.model == "env-model"
    assert config.llm.api_key == "file-key"
    assert os.environ["TURTLEMAP_LLM_MODEL"] == "env-model"
    assert "TURTLEMAP_LLM_API_KEY" not in os.environ


def test_turtlemap_config_reads_environment_when_env_file_missing(
    tmp_path, monkeypatch
) -> None:
    """验证 `.env` 文件不存在时仍会读取真实系统环境变量。"""

    missing_env_path = tmp_path / ".env.missing"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TURTLEMAP_LLM_MODEL", "env-only-model")
    monkeypatch.setenv("TURTLEMAP_LLM_API_KEY", "env-only-key")

    config = TurtleMapConfig.from_env(env_path=missing_env_path)

    assert config.llm.model == "env-only-model"
    assert config.llm.api_key == "env-only-key"


def test_turtlemap_config_normalizes_empty_optional_base_url(
    tmp_path, monkeypatch
) -> None:
    """验证空字符串 base_url 会归一化为 None。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "TURTLEMAP_LLM_MODEL=file-model\n"
        "TURTLEMAP_LLM_API_KEY=file-key\n"
        "TURTLEMAP_LLM_BASE_URL=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TURTLEMAP_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    config = TurtleMapConfig.from_env(env_path=env_path)

    assert config.llm.base_url is None


def test_turtlemap_config_can_build_unified_llm_config(tmp_path, monkeypatch) -> None:
    """验证统一应用配置会直接构建 LLMConfig。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "TURTLEMAP_LLM_MODEL=model-from-file\n"
        "TURTLEMAP_LLM_API_KEY=key-from-file\n"
        "TURTLEMAP_LLM_BASE_URL=http://from-file/v1\n"
        "TURTLEMAP_LLM_TIMEOUT=33\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TURTLEMAP_LLM_MODEL", raising=False)
    monkeypatch.delenv("TURTLEMAP_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TURTLEMAP_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("TURTLEMAP_LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("TURTLEMAP_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    config = TurtleMapConfig.from_env().llm

    assert config.model == "model-from-file"
    assert config.api_key == "key-from-file"
    assert config.base_url == "http://from-file/v1"
    assert config.timeout == 33.0
