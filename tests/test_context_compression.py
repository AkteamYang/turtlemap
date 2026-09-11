#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/09 22:12
# @Author  : YaHaoo
# @File    : test_context_compression.py

"""os 层上下文压缩主路径测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from turtlemap.config import LLMConfig, TurtleMapConfig
from turtlemap.kernel.models import MessageRole
from turtlemap.kernel.interfaces import ModelClientProviderProtocol
from turtlemap.kernel.models import (
    BaseAgentState,
    Message,
    MemoryView,
    SystemDefinition,
)
from turtlemap.os.context.models import ContextBuildInput, ContextBuildResult
from turtlemap.os.exceptions import NoCompressibleHistoryError
from turtlemap.os.agent import Agent
from turtlemap.os.context import (
    ContextBuildProvider,
    ContextCompressionProvider,
    ContextTokenBudget,
)
from turtlemap.os.context.prompt import MID_TERM_MEMORY_SUMMARY_TEMPLATE
from turtlemap.os.store import InMemoryStateStore
from turtlemap.os import SessionState


def _build_test_config(context_token_budget: ContextTokenBudget) -> TurtleMapConfig:
    """构造上下文压缩测试使用的 SDK 配置。

    参数:
        context_token_budget: 当前测试场景需要覆盖的上下文预算配置。

    返回:
        可传给 os Agent 的 SDK 配置对象。
    """

    return TurtleMapConfig(
        llm=LLMConfig(model="test-model", api_key="test-key"),
        context_token_budget=context_token_budget,
    )


@dataclass
class FakeTokenizer:
    """表示用于测试的最小 token 统计器。

    说明:
        当前实现直接按消息条数统计 token 数，避免测试依赖真实 tokenizer
        的具体编码细节，让压缩前后是否越界保持确定性。
    """

    def count_context(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, object]],
    ) -> int:
        """统计当前上下文的测试 token 数。

        参数:
            messages: 当前上下文消息列表。
            tool_schemas: 当前上下文工具 schema 列表。

        返回:
            基于消息条数和工具 schema 条数得到的测试 token 数。
        """

        return len(messages) + len(tool_schemas)


@dataclass
class QueueModelClient(ModelClientProviderProtocol):
    """表示按顺序返回固定响应的测试模型客户端。"""

    # 按调用顺序依次弹出的模型响应或异常列表。
    responses: list[Message | Exception]

    # 当前测试中收到的模型请求消息列表。
    requests: list[list[dict[str, object]]]

    async def generate(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]] | None = None,
    ) -> Message:
        """记录一次请求并返回预设响应。

        参数:
            messages: 当前轮传给模型的标准消息列表。
            tool_schemas: 当前轮可供模型选择的工具描述列表。

        返回:
            当前测试预设的下一条模型响应。
        """

        _ = tool_schemas
        self.requests.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@dataclass
class StaticModelClientProvider(ModelClientProviderProtocol):
    """表示始终返回同一个测试模型客户端的 provider。"""

    # 当前固定返回的测试模型客户端。
    client: ModelClientProviderProtocol

    async def generate(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]] | None = None,
    ) -> Message:
        """代理执行一次测试模型推理。

        参数:
            messages: 当前轮传给模型的标准消息列表。
            tool_schemas: 当前轮可供模型选择的工具描述列表。

        返回:
            当前固定测试客户端返回的 assistant 消息。
        """

        return await self.client.generate(messages=messages, tool_schemas=tool_schemas)


class SpyCompressionProvider(ContextCompressionProvider):
    """表示可记录后台压缩调度次数的测试压缩器。"""

    __slots__ = ("background_schedule_calls",)

    def __init__(
        self,
        model_client_provider: ModelClientProviderProtocol,
        keep_recent_history_rounds: int = 3,
    ) -> None:
        """初始化 SpyCompressionProvider。

        参数:
            model_client_provider: 当前压缩器复用的模型客户端提供器。
            keep_recent_history_rounds: 压缩时保留原始形态的最近历史轮次数。
        """

        super().__init__(
            model_client_provider=model_client_provider,
            keep_recent_history_rounds=keep_recent_history_rounds,
        )
        self.background_schedule_calls = 0

    async def schedule_background_compression_if_needed(
        self,
        build_input: ContextBuildInput,
        callback=None,
    ) -> None:
        """记录一次后台压缩调度。

        参数:
            build_input: 当前轮上下文构建输入。
            callback: 后台压缩结束后的结果回调。

        返回:
            无返回值。
        """

        _ = (build_input, callback)
        self.background_schedule_calls += 1


def test_context_build_provider_can_compress_history_for_hard_limit() -> None:
    """验证上下文超过硬限制时会触发一次同步 history 摘要压缩。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
        config=_build_test_config(ContextTokenBudget(
            max_context_tokens=10,
            hard_limit_tokens=4,
            soft_limit_tokens=3,
        )),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(role=MessageRole.USER, content="用户最早的问题"),
            Message(role=MessageRole.ASSISTANT, content="较早回复"),
            Message(role=MessageRole.USER, content="最近的问题"),
            Message(role=MessageRole.ASSISTANT, content="最近的回复"),
        ],
    )
    model_client = QueueModelClient(
        responses=[
            Message(
                role=MessageRole.ASSISTANT,
                content=(
                    "## Conversation Summary\n\n"
                    "### User Goal\n"
                    "- 解决用户最早的问题\n\n"
                    "### Constraints\n"
                    "- 无\n\n"
                    "### Completed\n"
                    "- 已给出较早回复\n\n"
                    "### Pending\n"
                    "- 跟进最近的问题\n\n"
                    "### Preferences\n"
                    "- 无\n\n"
                    "### Important Tool Results\n"
                    "- 无"
                ),
            )
        ],
        requests=[],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    context_build_provider = ContextBuildProvider(
        compression_provider=compression_provider,
        tokenizer=FakeTokenizer(),
        max_sync_compression_rounds=1,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    result = asyncio.run(context_build_provider.build(build_input))

    assert model_client.requests
    assert owner_state.memory.mid_term_memory.startswith("## Conversation Summary")
    assert "### User Goal" in owner_state.memory.mid_term_memory
    assert owner_state.history[0].content == "最近的问题"
    assert owner_state.history[1].content == "最近的回复"
    assert result.total_tokens == 4


def test_context_build_provider_wraps_markdown_memory_with_explicit_boundaries() -> (
    None
):
    """验证 Markdown 记忆会被边界标签包裹，而不是再嵌套 Markdown 标题。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
        config=_build_test_config(ContextTokenBudget(
            max_context_tokens=100,
            hard_limit_tokens=100,
            soft_limit_tokens=80,
        )),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        memory=MemoryView(
            long_term_memory=("## User Profile\n\n" "- 喜欢中文解释"),
            mid_term_memory=(
                "## Conversation Summary\n\n" "### Pending\n" "- 继续检查上下文拼装"
            ),
        ),
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(
            client=QueueModelClient(responses=[], requests=[]),
        ),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    context_build_provider = ContextBuildProvider(
        compression_provider=compression_provider,
        tokenizer=FakeTokenizer(),
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    result = asyncio.run(context_build_provider.build(build_input))

    memory_message = result.messages[1]
    assert "## Memory Context" in memory_message.content
    assert "Rules:" in memory_message.content
    assert "<memory_context>" not in memory_message.content
    assert "### Long-term Memory" in memory_message.content
    assert "Stable cross-session memory" in memory_message.content
    assert '<long_term_memory format="markdown">' in memory_message.content
    assert "</long_term_memory>" in memory_message.content
    assert "### Mid-term Memory" in memory_message.content
    assert "Rolling session summary" in memory_message.content
    assert '<mid_term_memory format="markdown">' in memory_message.content
    assert "</mid_term_memory>" in memory_message.content
    assert "## User Profile" in memory_message.content
    assert "## Conversation Summary" in memory_message.content


def test_context_build_provider_skips_compression_when_history_is_not_enough() -> None:
    """验证可压缩历史不足时不会触发同步摘要压缩。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
        config=_build_test_config(ContextTokenBudget(
            max_context_tokens=10,
            hard_limit_tokens=1,
            soft_limit_tokens=1,
        )),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(role=MessageRole.USER, content="唯一一条历史"),
        ],
    )
    model_client = QueueModelClient(
        responses=[],
        requests=[],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=0,
    )
    context_build_provider = ContextBuildProvider(
        compression_provider=compression_provider,
        tokenizer=FakeTokenizer(),
        max_sync_compression_rounds=1,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    with pytest.raises(NoCompressibleHistoryError):
        asyncio.run(context_build_provider.build(build_input))

    assert not model_client.requests
    assert owner_state.history[0].content == "唯一一条历史"
    assert owner_state.memory.mid_term_memory == ""


def test_context_build_provider_can_roll_mid_term_memory_forward() -> None:
    """验证已有 mid-term memory 会与新增较早历史一起被重写。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
        config=_build_test_config(ContextTokenBudget(
            max_context_tokens=10,
            hard_limit_tokens=4,
            soft_limit_tokens=3,
        )),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        memory=MemoryView(
            mid_term_memory=(
                "## Conversation Summary\n\n"
                "### User Goal\n"
                "- 处理最早一轮问题\n\n"
                "### Constraints\n"
                "- 无\n\n"
                "### Completed\n"
                "- 已完成第一阶段回复\n\n"
                "### Pending\n"
                "- 继续跟进后续问题\n\n"
                "### Preferences\n"
                "- 无\n\n"
                "### Important Tool Results\n"
                "- 无"
            ),
        ),
        history=[
            Message(role=MessageRole.USER, content="中间新增问题"),
            Message(role=MessageRole.ASSISTANT, content="中间新增回复"),
            Message(role=MessageRole.USER, content="最近的问题"),
            Message(role=MessageRole.ASSISTANT, content="最近的回复"),
        ],
    )
    model_client = QueueModelClient(
        responses=[
            Message(
                role=MessageRole.ASSISTANT,
                content=(
                    "## Conversation Summary\n\n"
                    "### User Goal\n"
                    "- 持续推进整体问题\n\n"
                    "### Constraints\n"
                    "- 无\n\n"
                    "### Completed\n"
                    "- 已吸收第一阶段与中间阶段信息\n\n"
                    "### Pending\n"
                    "- 处理最近的问题\n\n"
                    "### Preferences\n"
                    "- 无\n\n"
                    "### Important Tool Results\n"
                    "- 无"
                ),
            )
        ],
        requests=[],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    context_build_provider = ContextBuildProvider(
        compression_provider=compression_provider,
        tokenizer=FakeTokenizer(),
        max_sync_compression_rounds=1,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    result = asyncio.run(context_build_provider.build(build_input))

    assert model_client.requests
    first_request_messages = model_client.requests[0]
    system_prompt_content = first_request_messages[0]["content"]
    assert first_request_messages[0]["role"] == "system"
    assert "## Role\n\nAgent 会话历史摘要维护者" in system_prompt_content
    assert "## Objective\n\n" in system_prompt_content
    assert "将已有会话历史摘要与新增较早历史合并" in system_prompt_content
    assert "## Constraints\n\n" in system_prompt_content
    assert "不要只总结新增历史" in system_prompt_content
    assert "## Output Format\n\n" in system_prompt_content
    assert "<summary_output_format>" in system_prompt_content
    assert "</summary_output_format>" in system_prompt_content
    assert MID_TERM_MEMORY_SUMMARY_TEMPLATE in system_prompt_content
    assert any(
        message.get("content", "").startswith("以下是当前已有的会话历史摘要")
        for message in first_request_messages
    )
    assert any(
        message.get("content") == ("中间新增问题") for message in first_request_messages
    )
    assert owner_state.memory.mid_term_memory.startswith("## Conversation Summary")
    assert owner_state.history[0].content == "最近的问题"
    assert owner_state.history[1].content == "最近的回复"
    assert result.total_tokens == 4


def test_context_build_provider_schedule_background_compression_if_needed_can_schedule_background_compression_if_needed() -> (
    None
):
    """验证 history 写入后若超过软限制，会触发后台压缩调度。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
        config=_build_test_config(ContextTokenBudget(
            max_context_tokens=10,
            hard_limit_tokens=4,
            soft_limit_tokens=3,
        )),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(role=MessageRole.USER, content="较早问题"),
            Message(role=MessageRole.ASSISTANT, content="较早回复"),
            Message(role=MessageRole.USER, content="最近问题"),
            Message(role=MessageRole.ASSISTANT, content="最近回复"),
        ],
    )
    model_client = QueueModelClient(
        responses=[],
        requests=[],
    )
    compression_provider = SpyCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    context_build_provider = ContextBuildProvider(
        compression_provider=compression_provider,
        tokenizer=FakeTokenizer(),
        max_sync_compression_rounds=1,
    )

    asyncio.run(
        context_build_provider.schedule_background_compression_if_needed(
            owner_agent=owner_agent,
            owner_state=owner_state,
            session_state=SessionState(),
        )
    )

    assert compression_provider.background_schedule_calls == 1
    assert not model_client.requests
    assert owner_state.memory.mid_term_memory == ""
    assert owner_state.history[0].content == "较早问题"
    assert owner_state.history[1].content == "较早回复"


def test_background_compression_uses_message_id_to_merge_result() -> None:
    """验证后台压缩只根据稳定 message_id 前缀判断短期历史是否匹配。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
        config=_build_test_config(ContextTokenBudget(
            max_context_tokens=10,
            hard_limit_tokens=4,
            soft_limit_tokens=3,
        )),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(
                message_id=1, role=MessageRole.USER, content="后台较早问题"
            ),
            Message(
                message_id=2, role=MessageRole.ASSISTANT, content="后台较早回复"
            ),
            Message(
                message_id=3, role=MessageRole.USER, content="后台最近问题"
            ),
            Message(
                message_id=4, role=MessageRole.ASSISTANT, content="后台最近回复"
            ),
        ],
        history_ids=[1, 2, 3, 4],
    )
    model_client = QueueModelClient(
        responses=[
            Message(
                role=MessageRole.ASSISTANT,
                content=(
                    "## Conversation Summary\n\n"
                    "### User Goal\n"
                    "- 处理后台较早问题\n\n"
                    "### Constraints\n"
                    "- 无\n\n"
                    "### Completed\n"
                    "- 已完成后台较早回复\n\n"
                    "### Pending\n"
                    "- 跟进后台最近问题\n\n"
                    "### Preferences\n"
                    "- 无\n\n"
                    "### Important Tool Results\n"
                    "- 无"
                ),
            )
        ],
        requests=[],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    async def run_background_scenario() -> None:
        """执行一次可控的后台压缩测试场景。"""

        await compression_provider.schedule_background_compression_if_needed(
            build_input=build_input,
        )

        # 调度方法只创建后台任务，不应在返回前同步改写原始运行态。
        assert owner_state.memory.mid_term_memory == ""
        assert owner_state.history[0].content == "后台较早问题"

        # 运行期消息内容即使被本地对象改写，只要稳定 message_id 顺序不变，
        # 压缩结果仍然可以安全合并；消息本体一致性由持久化层维护。
        owner_state.history[0].content = "主流程已修改较早问题"
        owner_state.history.append(
            Message(
                message_id=5, role=MessageRole.USER, content="压缩期间新增问题"
            )
        )
        owner_state.history_ids.append(5)
        await compression_provider.wait_background_compressions()

    asyncio.run(run_background_scenario())

    assert model_client.requests
    assert owner_state.memory.mid_term_memory.startswith("## Conversation Summary")
    assert [message.message_id for message in owner_state.history] == [
        3,
        4,
        5,
    ]
    assert owner_state.history_ids == [3, 4, 5]
    assert owner_state.history[-1].content == "压缩期间新增问题"
    assert any(
        message.get("content") == "后台较早问题" for message in model_client.requests[0]
    )


def test_background_compression_can_merge_when_snapshot_is_unchanged() -> None:
    """验证后台压缩完成后会在原始短期历史未变化时合并结果。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(message_id=1, role=MessageRole.USER, content="较早问题"),
            Message(
                message_id=2, role=MessageRole.ASSISTANT, content="较早回复"
            ),
            Message(message_id=3, role=MessageRole.USER, content="最近问题"),
            Message(
                message_id=4, role=MessageRole.ASSISTANT, content="最近回复"
            ),
        ],
        history_ids=[1, 2, 3, 4],
    )
    model_client = QueueModelClient(
        responses=[
            Message(
                role=MessageRole.ASSISTANT,
                content=(
                    "## Conversation Summary\n\n"
                    "### User Goal\n"
                    "- 处理较早问题\n\n"
                    "### Constraints\n"
                    "- 无\n\n"
                    "### Completed\n"
                    "- 已完成较早回复\n\n"
                    "### Pending\n"
                    "- 跟进最近问题\n\n"
                    "### Preferences\n"
                    "- 无\n\n"
                    "### Important Tool Results\n"
                    "- 无"
                ),
            )
        ],
        requests=[],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    async def run_background_scenario() -> None:
        """执行一次原始状态未变化的后台压缩场景。"""

        await compression_provider.schedule_background_compression_if_needed(
            build_input=build_input,
        )
        await compression_provider.wait_background_compressions()

    asyncio.run(run_background_scenario())

    assert owner_state.memory.mid_term_memory.startswith("## Conversation Summary")
    assert [message.message_id for message in owner_state.history] == [3, 4]
    assert owner_state.history_ids == [3, 4]


def test_background_compression_skips_merge_when_message_id_prefix_is_changed() -> None:
    """验证后台压缩合并前会检查短期历史的 message_id 前缀。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(message_id=1, role=MessageRole.USER, content="较早问题"),
            Message(
                message_id=2, role=MessageRole.ASSISTANT, content="较早回复"
            ),
            Message(message_id=3, role=MessageRole.USER, content="最近问题"),
            Message(
                message_id=4, role=MessageRole.ASSISTANT, content="最近回复"
            ),
        ],
        history_ids=[1, 2, 3, 4],
    )
    model_client = QueueModelClient(
        responses=[
            Message(
                role=MessageRole.ASSISTANT,
                content=(
                    "## Conversation Summary\n\n"
                    "### User Goal\n"
                    "- 处理较早问题\n\n"
                    "### Constraints\n"
                    "- 无\n\n"
                    "### Completed\n"
                    "- 已完成较早回复\n\n"
                    "### Pending\n"
                    "- 跟进最近问题\n\n"
                    "### Preferences\n"
                    "- 无\n\n"
                    "### Important Tool Results\n"
                    "- 无"
                ),
            )
        ],
        requests=[],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    async def run_background_scenario() -> None:
        """执行一次 message_id 已变化的后台压缩场景。"""

        await compression_provider.schedule_background_compression_if_needed(
            build_input=build_input,
        )
        owner_state.history[0].message_id = 99
        await compression_provider.wait_background_compressions()

    asyncio.run(run_background_scenario())

    assert model_client.requests
    assert owner_state.memory.mid_term_memory == ""
    assert [message.message_id for message in owner_state.history] == [
        99,
        2,
        3,
        4,
    ]
    assert owner_state.history_ids == [1, 2, 3, 4]


def test_background_compression_handles_no_compressible_history_inside_task() -> None:
    """验证后台压缩由任务内部判断可压缩历史是否足够。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(role=MessageRole.USER, content="最近问题"),
            Message(role=MessageRole.ASSISTANT, content="最近回复"),
        ],
    )
    model_client = QueueModelClient(
        responses=[],
        requests=[],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    async def run_background_scenario() -> None:
        """执行一次没有可压缩 history 的后台压缩场景。"""

        await compression_provider.schedule_background_compression_if_needed(
            build_input=build_input,
        )
        await compression_provider.wait_background_compressions()

    asyncio.run(run_background_scenario())

    assert not model_client.requests
    assert owner_state.memory.mid_term_memory == ""
    assert owner_state.history[0].content == "最近问题"
    assert owner_state.history[1].content == "最近回复"


def test_background_compression_task_records_branch_id() -> None:
    """验证后台压缩任务记录会带上当前会话分支 id。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(role=MessageRole.USER, content="较早问题"),
            Message(role=MessageRole.ASSISTANT, content="较早回复"),
            Message(role=MessageRole.USER, content="最近问题"),
            Message(role=MessageRole.ASSISTANT, content="最近回复"),
        ],
    )
    session_state = SessionState(
        session_id="session_1",
        branch_id="branch_1001",
        version=3,
    )
    state_store = InMemoryStateStore(
        session_id2state={
            session_state.session_id: session_state,
        }
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(
            client=QueueModelClient(responses=[], requests=[]),
        ),
        state_store=state_store,
        keep_recent_history_rounds=2,
    )
    build_input = ContextBuildInput(
        session_state=session_state,
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    async def run_background_scenario() -> None:
        """执行一次仅检查压缩任务记录的后台调度场景。"""

        await compression_provider.schedule_background_compression_if_needed(
            build_input=build_input,
        )

    asyncio.run(run_background_scenario())

    assert state_store.context_compression_tasks[0].branch_id == "branch_1001"


def test_compact_history_raises_when_no_compressible_history() -> None:
    """验证压缩前置条件不满足时直接抛出异常，而不是返回布尔值。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(role=MessageRole.USER, content="最近问题"),
            Message(role=MessageRole.ASSISTANT, content="最近回复"),
        ],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(
            client=QueueModelClient(responses=[], requests=[]),
        ),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    with pytest.raises(NoCompressibleHistoryError):
        asyncio.run(
            compression_provider._compact_history_into_mid_term_memory(
                build_input=build_input,
            )
        )


def test_rewrite_mid_term_memory_can_retry_after_transient_error() -> None:
    """验证中期工作记忆重写遇到瞬时异常时会按指数退避自动重试。"""

    owner_agent = Agent(
        agent_name="root_agent",
        system=SystemDefinition(
            role="assistant",
            objective="help user",
        ),
    )
    owner_state = BaseAgentState(
        agent_name="root_agent",
        system=owner_agent.system,
        history=[
            Message(role=MessageRole.USER, content="较早问题"),
            Message(role=MessageRole.ASSISTANT, content="较早回复"),
            Message(role=MessageRole.USER, content="最近问题"),
            Message(role=MessageRole.ASSISTANT, content="最近回复"),
        ],
    )
    model_client = QueueModelClient(
        responses=[
            RuntimeError("transient failure"),
            Message(
                role=MessageRole.ASSISTANT,
                content=(
                    "## Conversation Summary\n\n"
                    "### User Goal\n"
                    "- 处理较早问题\n\n"
                    "### Constraints\n"
                    "- 无\n\n"
                    "### Completed\n"
                    "- 已完成较早回复\n\n"
                    "### Pending\n"
                    "- 跟进最近问题\n\n"
                    "### Preferences\n"
                    "- 无\n\n"
                    "### Important Tool Results\n"
                    "- 无"
                ),
            ),
        ],
        requests=[],
    )
    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(client=model_client),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=2,
    )
    build_input = ContextBuildInput(
        session_state=SessionState(),
        owner_agent=owner_agent,
        owner_agent_state=owner_state,
        available_tools=[],
    )

    summary_content = asyncio.run(
        compression_provider._rewrite_mid_term_memory(
            build_input=build_input,
            history_messages=owner_state.history[:2],
        )
    )

    assert summary_content.startswith("## Conversation Summary")
    assert len(model_client.requests) == 2


def test_keep_recent_history_rounds_keeps_configured_value() -> None:
    """验证保留最近 history 轮次数不会按消息条数做偶数归一化。"""

    compression_provider = ContextCompressionProvider(
        model_client_provider=StaticModelClientProvider(
            client=QueueModelClient(responses=[], requests=[]),
        ),
        state_store=InMemoryStateStore(),
        keep_recent_history_rounds=3,
    )

    assert compression_provider.keep_recent_history_rounds == 3
