#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/12 15:26
# @Author  : YaHaoo
# @File    : retry.py

"""turtlemap 共享重试工具。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar, cast

from turtlemap.shared.logger import Logger

ParamT = ParamSpec("ParamT")
ReturnT = TypeVar("ReturnT")


def exponential_backoff_retry(
    *,
    retry_exceptions: tuple[type[BaseException], ...],
    max_attempts: int = 3,
    initial_delay_seconds: float = 0.2,
    max_delay_seconds: float = 5.0,
    backoff_factor: float = 2.0,
) -> Callable[[Callable[ParamT, ReturnT]], Callable[ParamT, ReturnT]]:
    """构造一个指数退避重试装饰器。
    attempt为调用轮次，首次为1
    delay_seconds = initial_delay_seconds * (backoff_factor ** (attempt - 1))

    参数:
        retry_exceptions: 允许触发重试的异常类型元组。
        max_attempts: 最大尝试次数，包含首次执行。
        initial_delay_seconds: 首次失败后的基础等待时长，单位秒。
        max_delay_seconds: 单次等待时长上限，单位秒。
        backoff_factor: 每次重试前等待时长的指数放大倍数。

    返回:
        一个可用于同步或异步函数的装饰器。

    异常:
        ValueError: 当重试配置不合法时抛出。

    说明:
        - 该装饰器同时支持同步函数和异步函数；
        - 若最终仍失败，则直接抛出最后一次捕获到的原始异常；
        - 当前实现默认不引入随机抖动，优先保证行为可预测、可测试。
    """

    if not retry_exceptions:
        raise ValueError("retry_exceptions 不能为空")

    if max_attempts <= 0:
        raise ValueError("max_attempts 必须为正整数")

    if initial_delay_seconds < 0:
        raise ValueError("initial_delay_seconds 不能小于 0")

    if max_delay_seconds < 0:
        raise ValueError("max_delay_seconds 不能小于 0")

    if backoff_factor < 1:
        raise ValueError("backoff_factor 不能小于 1")

    def decorator(func: Callable[ParamT, ReturnT]) -> Callable[ParamT, ReturnT]:
        """根据目标函数类型包装重试逻辑。"""

        if asyncio.iscoroutinefunction(func):
            wrapped_async = _build_async_retry_wrapper(
                func=func,
                retry_exceptions=retry_exceptions,
                max_attempts=max_attempts,
                initial_delay_seconds=initial_delay_seconds,
                max_delay_seconds=max_delay_seconds,
                backoff_factor=backoff_factor,
            )
            return cast(Callable[ParamT, ReturnT], wrapped_async)

        wrapped_sync = _build_sync_retry_wrapper(
            func=func,
            retry_exceptions=retry_exceptions,
            max_attempts=max_attempts,
            initial_delay_seconds=initial_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            backoff_factor=backoff_factor,
        )
        return wrapped_sync

    return decorator


def _build_async_retry_wrapper(
    *,
    func: Callable[ParamT, Awaitable[ReturnT]],
    retry_exceptions: tuple[type[BaseException], ...],
    max_attempts: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
    backoff_factor: float,
) -> Callable[ParamT, Awaitable[ReturnT]]:
    """构造异步函数的重试包装器。"""

    @wraps(func)
    async def wrapped(*args: ParamT.args, **kwargs: ParamT.kwargs) -> ReturnT:
        """执行带指数退避的异步重试调用。"""

        for attempt in range(1, max_attempts + 1):
            try:
                return await func(*args, **kwargs)
            except retry_exceptions as exc:
                if attempt >= max_attempts:
                    raise

                delay_seconds = _compute_retry_delay_seconds(
                    attempt=attempt,
                    initial_delay_seconds=initial_delay_seconds,
                    max_delay_seconds=max_delay_seconds,
                    backoff_factor=backoff_factor,
                )
                _log_retry_warning(
                    func_name=func.__qualname__,
                    exc=exc,
                    delay_seconds=delay_seconds,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                await asyncio.sleep(delay_seconds)

        raise RuntimeError("异步重试包装器进入了不可达分支")

    return wrapped


def _build_sync_retry_wrapper(
    *,
    func: Callable[ParamT, ReturnT],
    retry_exceptions: tuple[type[BaseException], ...],
    max_attempts: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
    backoff_factor: float,
) -> Callable[ParamT, ReturnT]:
    """构造同步函数的重试包装器。"""

    @wraps(func)
    def wrapped(*args: ParamT.args, **kwargs: ParamT.kwargs) -> ReturnT:
        """执行带指数退避的同步重试调用。"""

        for attempt in range(1, max_attempts + 1):
            try:
                return func(*args, **kwargs)
            except retry_exceptions as exc:
                if attempt >= max_attempts:
                    raise

                delay_seconds = _compute_retry_delay_seconds(
                    attempt=attempt,
                    initial_delay_seconds=initial_delay_seconds,
                    max_delay_seconds=max_delay_seconds,
                    backoff_factor=backoff_factor,
                )
                _log_retry_warning(
                    func_name=func.__qualname__,
                    exc=exc,
                    delay_seconds=delay_seconds,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                time.sleep(delay_seconds)

        raise RuntimeError("同步重试包装器进入了不可达分支")

    return wrapped


def _compute_retry_delay_seconds(
    *,
    attempt: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
    backoff_factor: float,
) -> float:
    """计算某次重试前应等待的时长。

    参数:
        attempt: 当前失败发生在第几次尝试，首次失败为 1。
        initial_delay_seconds: 首次失败后的基础等待时长。
        max_delay_seconds: 单次等待时长上限。
        backoff_factor: 指数放大倍数。

    返回:
        当前应等待的时长，单位秒。
    """

    delay_seconds = initial_delay_seconds * (backoff_factor ** (attempt - 1))
    return min(delay_seconds, max_delay_seconds)


def _log_retry_warning(
    *,
    func_name: str,
    exc: BaseException,
    delay_seconds: float,
    attempt: int,
    max_attempts: int,
) -> None:
    """打印重试前的 warning 日志。

    参数:
        func_name: 当前触发重试的函数名。
        exc: 当前捕获到的可重试异常。
        delay_seconds: 本次重试前等待时长。
        attempt: 当前已执行次数。
        max_attempts: 最大允许执行次数。

    返回:
        无返回值。
    """

    Logger.logger.warning(
        f"函数执行失败，准备重试，"
        f"func={func_name}, "
        f"exception={repr(exc)}"
        f"delay_seconds={delay_seconds:.3f}, "
        f"attempt={attempt}/{max_attempts}"
    )
