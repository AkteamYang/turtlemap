#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/02 14:24
# @Author  : YaHaoo
# @File    : conftest.py

"""测试环境公共初始化。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _ensure_src_path() -> None:
    """将 `src` 目录加入 `sys.path`，保证测试可直接导入包。

    返回:
        无返回值。
    """

    project_root = Path(__file__).resolve().parents[1]
    src_path = project_root / "src"
    src_path_str = str(src_path)
    if src_path_str not in sys.path:
        sys.path.insert(0, src_path_str)


_ensure_src_path()

from turtlemap.config import LLMConfig


# 当前仓库本地联调使用的默认模型客户端配置。
TEST_MODEL_CLIENT_CONFIG = LLMConfig(
    model="/models/Qwen3.5-35B-A3B-FP8",
    api_key="sk-38c15694bf47d601e1b46bc416cb9516",
    base_url="http://139.224.74.144:7424/v1",
)


@pytest.fixture
def test_model_client_config() -> LLMConfig:
    """返回测试侧默认使用的模型客户端配置。

    返回:
        供联调测试直接复用的 `LLMConfig`。

    说明:
        该配置只用于 `tests` 目录内的本地联调，不应作为运行时代码的默认配置来源。
    """

    return TEST_MODEL_CLIENT_CONFIG
