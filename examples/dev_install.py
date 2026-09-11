#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/04/02 09:54
# @Author  : YaHaoo
# @File    : dev_install.py

"""
turtlemap 项目安装脚本。

该脚本用于设置 Python 环境并安装所有依赖项。
"""

import os
import subprocess
import sys
from pathlib import Path


ENV_NAME = "turtlemap"

INSTALL_STEPS = [
    ("更新 pip", ["pip", "install", "--upgrade", "pip", "-i", "https://mirrors.aliyun.com/pypi/simple"]),
    ("安装 uv", ["pip", "install", "uv", "-i", "https://mirrors.aliyun.com/pypi/simple"]),
    ("安装基础依赖", ["uv", "pip", "install", "-r", "requirements/public_frozen_requirements.txt"]),
    ("安装可变依赖", ["uv", "pip", "install", "-r", "requirements/public_mutable_requirements.txt"]),
    ("安装二进制依赖", ["uv", "pip", "install", "-r", "requirements/public_binary_requirements.txt", "--only-binary=all"]),
    ("安装私有依赖", ["uv", "pip", "install", "-r", "requirements/private_requirements.txt"]),
]


def run_command(cmd: list[str], cwd: Path | None = None) -> None:
    """运行命令并检查错误。"""
    print(f"运行: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, check=False)
    if result.returncode != 0:
        print(f"错误: 命令执行失败，退出码 {result.returncode}")
        sys.exit(1)


def check_conda_installed() -> bool:
    """检查 conda 是否已安装。"""
    try:
        subprocess.run(
            ["conda", "--version"],
            capture_output=True,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_current_env() -> tuple[bool, str | None]:
    """检查当前是否在目标 conda 环境中。
    
    Returns:
        (是否在目标环境中, 当前环境名称)
    """
    conda_env = os.environ.get("CONDA_DEFAULT_ENV")
    conda_prefix = os.environ.get("CONDA_PREFIX")
    
    if not conda_env or not conda_prefix:
        return False, None
    
    return conda_env == ENV_NAME, conda_env


def is_expected_python_interpreter() -> bool:
    """检查当前运行解释器是否属于目标 conda 环境。"""
    current_python = Path(sys.executable).resolve()
    return ENV_NAME in str(current_python)


def check_env_exists() -> bool:
    """检查目标环境是否存在。"""
    try:
        result = subprocess.run(
            ["conda", "info", "--envs"],
            capture_output=True,
            text=True,
            check=True
        )
        return ENV_NAME in result.stdout
    except subprocess.CalledProcessError:
        return False


def print_activation_instructions(env_exists: bool) -> None:
    """打印环境激活说明。
    
    Args:
        env_exists: 环境是否已存在
    """
    print("\n" + "=" * 60)
    print("环境未激活，请先执行以下命令:")
    print("=" * 60)
    
    if not env_exists:
        print(f"  # 创建环境")
        print(f"  conda create -n {ENV_NAME} python=3.10 -y")
        print(f"  ")
    
    print(f"  # 激活环境")
    print(f"  conda activate {ENV_NAME}")
    print(f"  ")
    print(f"  # 然后再次运行安装脚本")
    print(f'  python "{__file__}"')
    print("=" * 60)


def print_interpreter_mismatch_warning() -> None:
    """打印解释器不匹配时的提醒。"""
    print("\n" + "=" * 60)
    print("当前运行解释器与目标 conda 环境不一致:")
    print("=" * 60)
    print(f"  当前解释器: {Path(sys.executable).resolve()}")
    print(f"  目标环境: {ENV_NAME}")
    print("  请将IDE换到目标环境对应的解释器后，再重新运行安装脚本。")
    print("=" * 60)


def install_dependencies() -> None:
    """安装所有项目依赖。"""
    python_path = sys.executable
    os.environ["UV_PYTHON"] = python_path
    
    for name, cmd in INSTALL_STEPS:
        print(f"{name}...")
        run_command(cmd)


def main() -> None:
    """主安装函数。"""
    print("=" * 60)
    print(f'{ENV_NAME} 安装启动')
    print("=" * 60)
    
    # 检查 conda 是否已安装
    if not check_conda_installed():
        print("\n错误: 未检测到 conda，请先安装 conda")
        sys.exit(1)
    
    # 检查当前环境
    in_target_env, current_env = check_current_env()
    
    if not in_target_env:
        # 不在目标环境中
        if current_env:
            print(f"当前环境: {current_env}")
        else:
            print("未检测到 conda 环境")
        
        # 检查目标环境是否存在
        env_exists = check_env_exists()
        
        # 打印激活说明（环境不存在时提示创建）
        print_activation_instructions(env_exists=env_exists)
        sys.exit(0)

    if not is_expected_python_interpreter():
        print(f"当前环境: {ENV_NAME}")
        print_interpreter_mismatch_warning()
        sys.exit(1)
    
    # 已在目标环境中，继续安装
    print(f"当前环境: {ENV_NAME}")
    install_dependencies()
    
    print("\n" + "=" * 60)
    print("安装完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
