#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/09/01 22:12
# @Author  : YaHaoo
# @File    : main.py

"""TurtleMap 示例 HTTP 服务端入口。"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_path in (PROJECT_ROOT, SRC_ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from examples.config import RedisConfig
from examples.mysql_store import MysqlConfig, close_mysql_pool, init_mysql
from examples.server.schemas import ApiResponse, CompletionRequest, CreateSessionRequest
from examples.server.service import ServerAppService
from examples.server.stream_store import RedisRunStreamStore
from turtlemap import TurtleMapConfig

config = TurtleMapConfig.from_env(PROJECT_ROOT / ".env")
mysql_config = MysqlConfig.from_env(PROJECT_ROOT / ".env")
redis_config = RedisConfig.from_env(PROJECT_ROOT / ".env")
app_service: ServerAppService | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """管理 FastAPI 示例服务生命周期。

    参数:
        _: 当前 FastAPI 应用实例，当前不需要读取。

    返回:
        异步上下文迭代器。
    """

    global app_service
    await init_mysql(mysql_config)
    stream_store = RedisRunStreamStore(
        host=redis_config.host,
        port=redis_config.port,
        username=redis_config.username,
        password=redis_config.password,
    )
    app_service = ServerAppService(
        config=config,
        stream_store=stream_store,
    )
    try:
        yield
    finally:
        app_service = None
        await close_mysql_pool()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/v1/sessions")
async def create_session(request: CreateSessionRequest) -> ApiResponse:
    """创建一个新的会话。

    参数:
        request: 创建会话请求体。

    返回:
        包含新会话 id 和标题的统一响应。
    """

    return await _get_app_service().create_session(title=request.title)


@app.get("/api/v1/info")
async def get_info(limit: int = 20) -> ApiResponse:
    """查询页面初始化基础信息。

    参数:
        limit: 最大返回会话条数。

    返回:
        包含用户信息、最近活动会话和会话列表的统一响应。
    """

    return await _get_app_service().get_info(limit=limit)


@app.delete("/api/v1/sessions/{session_id}")
async def delete_session(session_id: str) -> ApiResponse:
    """软删除指定会话。

    参数:
        session_id: 当前会话 id。

    返回:
        统一空响应。
    """

    return await _get_app_service().delete_session(session_id=session_id)


@app.get("/api/v1/sessions/{session_id}/history")
async def get_history(session_id: str) -> ApiResponse:
    """查询指定会话的稳定展示历史。

    参数:
        session_id: 当前会话 id。

    返回:
        包含历史消息列表的统一响应。
    """

    return await _get_app_service().get_history(session_id=session_id)


@app.post("/api/v1/sessions/{session_id}/completion")
async def completion(
    session_id: str,
    request: CompletionRequest,
) -> StreamingResponse:
    """处理 completion 输入并返回 SSE 流。

    参数:
        session_id: 当前会话 id。
        request: completion 请求体，支持用户输入或中断恢复响应。

    返回:
        SSE StreamingResponse。
    """

    return StreamingResponse(
        _get_app_service().stream_completion(session_id=session_id, request=request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"  # 禁用Nginx缓冲，确保实时输出
        }
    )


@app.post("/api/v1/sessions/{session_id}/completion/resume")
async def resume_completion(session_id: str) -> StreamingResponse:
    """处理页面首次加载恢复并返回 SSE 流。

    参数:
        session_id: 当前会话 id。

    返回:
        SSE StreamingResponse。
    """

    return StreamingResponse(
        _get_app_service().stream_resume_completion(session_id=session_id),
        media_type="text/event-stream",
    )


def _get_app_service() -> ServerAppService:
    """获取已初始化的应用服务。

    返回:
        ServerAppService 实例。
    """

    assert app_service is not None
    return app_service


if __name__ == "__main__":
    uvicorn.run("main:app", host='0.0.0.0', port=8000, workers=1)
