# turtlemap kernel 原型接口设计

## 1. 文档目标

本文聚焦 `kernel` 原型落地所需的最小接口。

本文不重复展开完整架构设计，而是回答下面几个问题：

- `Agent` 至少需要暴露什么信息
- `Runtime` 需要依赖哪些外部协议才能跑通最小闭环
- 哪些接口属于 `kernel` 最小闭环，哪些应留给 `os`

目标是让后续原型编码时，不再依赖隐含假设。

## 2. 设计原则

- 只定义原型阻塞级接口
- 接口优先服务 `Runtime` 最小闭环
- 复杂治理、复杂策略、复杂存储优化留给 `os`
- 输入输出尽量结构化，避免运行时自由推断

补充边界：

- `os` 层可以参考现有 LLM 实现：`/Users/yahaoo/Desktop/Git Project/ecomind_py_lib/ecomind_lib/src/ecomind_lib/d_agent/infra/llm/`
- `kernel` 层的 `ModelClient` 接口直接参考 `/Users/yahaoo/Desktop/Git Project/ecomind_py_lib/ecomind_lib/src/ecomind_lib/d_agent/infra/agent_llm.py`
- `os` 层通过 `ModelClient` 子类，将 `/Users/yahaoo/Desktop/Git Project/ecomind_py_lib/ecomind_lib/src/ecomind_lib/d_agent/infra/llm/` 下的相关实现复制到当前项目后完成具体功能

## 3. Agent 最小接口

### 3.1 Agent 职责

`Agent` 是业务代码显式创建并传入 `Runtime` 的运行时对象。

它是全局运行时定义，不等同于会话内的 `BaseAgentState`。

第一阶段 `Agent` 至少需要承接：

- 稳定 `agent_name`
- `system`
- `tools`
- `handoffs`

### 3.2 Agent 最小字段

`Agent` 至少具备以下属性：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_name` | `str` | Agent 稳定名称，是运行时查找主标识。 |
| `system` | `SystemDefinition` | 当前 Agent 的结构化系统定义。 |
| `tools` | `list[ExecutableTool | function]` | Agent 初始接收的工具集合。 |
| `handoffs` | `list[Agent]` | 允许切换到的下游 Agent。 |

约束：

- `Agent` 不参与 `SessionState` 持久化
- `tools` 最终都应被装配成统一的 `ExecutableTool`
- `handoffs` 直接持有 `Agent` 引用，而不是字符串名称
- `Runtime` 解析 Agent 图时，应按 `agent_name` 去重
- `Agent` 第一阶段不持有 `ModelClient`

### 3.3 ModelClient 注入边界

第一阶段：

- `Agent` 不持有 `ModelClient`
- `Runtime` 通过 `OSService` 间接触达模型调用能力
- `ModelClientProtocol`、模型配置与实例创建放在 `os` 层

这样可以避免把运行时依赖混入 `Agent` 定义本身。

补充实现：

- `kernel.BaseAgent` 只保留最小结构与 `build_tool_provider(...)` 这个扩展点
- `os` 层提供 `Agent(BaseAgent)` 作为默认业务接入入口
- `Agent` 默认返回 `ToolService`
- 这样业务侧无需额外手动注入工具提供器，也能接上 `os` 层默认的工具管理与 `ExecutionUnit` 构造逻辑

## 4. 模型调用接口

### 4.1 OSService / ModelClientProtocol

当前模型调用能力由 os 层 `OSService` 持有，并通过 `ModelClientProtocol` 暴露给上下文压缩、assistant 生成等 os 事务。

当前阶段不需要复杂路由逻辑，只提供最小推理接口：

| 方法 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| `generate_message(...)` | `messages`、`tool_schemas` | `LLMMessage` | 返回当前轮 assistant 消息。 |
| `stream_generate_message(...)` | `messages`、`tool_schemas`、`tool_choice` | `AsyncIterable[LLMCompletionChunk]` | 返回当前轮流式响应 chunk。 |

约束：

- 第一阶段允许 `OSService` 始终持有同一个默认 `ModelClientProtocol` 实现
- 更复杂的模型选择、路由、降级策略留给 `os`
- `Runtime` 不直接创建或获取底层模型客户端，只依赖 `OSService`

### 4.2 ModelClient

`Runtime` 要跑通 `MessageState` 和 `ToolState` continuation，必须依赖统一模型调用接口。

当前阶段不应在 `kernel` 设计文档中重新展开一套独立的 LLM 配置与客户端细节。

补充说明：

- `kernel` 层的 `ModelClient` 接口直接参考 `/Users/yahaoo/Desktop/Git Project/ecomind_py_lib/ecomind_lib/src/ecomind_lib/d_agent/infra/agent_llm.py`
- `os` 层子类，参考现有 `/Users/yahaoo/Desktop/Git Project/ecomind_py_lib/ecomind_lib/src/ecomind_lib/d_agent/infra/llm/` 下的实现LLM接口调用，最终需复制到当前项目中落地
- `kernel` 不关心具体是 OpenAI、Gemini 还是其他供应商
- 具体模型配置字段、适配器选择、流式统计和供应商差异处理，直接参考现有代码，不在本文重复设计
- `ModelClient` 的配置读取、实例创建和注入都放在 `os` 层
- `Runtime` 只通过 `OSService` 间接触达模型调用能力

对 `Runtime` 而言，只需要抽象出如下能力即可：

| 方法 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| `generate(...)` | `messages`、`tool_schemas` | `ModelResponse` | 执行一次普通或带工具的模型推理。 |
| `stream_generate(...)` | `messages`、`tool_schemas` | `Iterator[str]` | 执行一次流式推理，返回文本分片。 |

### 4.3 模型响应标准化

当前模型响应在 os 层先标准化为 `LLMMessage`，再包装为 `RuntimeArtifact` 交给 kernel。
kernel 不直接解析 `LLMMessage` 的具体字段，只根据 `RuntimeArtifact.type` 做必要流程控制。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `message` | `LLMMessage` | 本次模型输出的标准消息，由 os 层消费。 |
| `artifact` | `RuntimeArtifact` | 交给 kernel 推进流程的不透明产物。 |
| `finish_reason` | `str | None` | 停止原因，例如普通结束或工具调用。 |

约束：

- 如果模型决定调用工具，os 层将产物类型标记为 `RuntimeArtifactType.TOOL_CALL`
- 普通 assistant 回复标记为 `RuntimeArtifactType.ASSISTANT_MESSAGE`
- `Runtime` 不依赖原始供应商响应格式，也不读取 `LLMMessage.tool_calls` 细节

### 4.4 ToolCallResponse

参考 `agent_llm.py` 中 `invoke_with_tools(...)` 的职责，第一阶段应显式支持“带工具 schema 的模型调用”。

对 `Runtime` 来说，不需要单独暴露另一套复杂接口，但至少要保证：

- `generate(...)` 可以接收 `tool_schemas`
- 返回结果能够明确区分“普通 assistant 回复”和“assistant 决定调用工具”

如果实现上希望单独建模，增加：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `message` | `LLMMessage` | assistant 输出消息。 |
| `tool_calls` | `list[dict[str, Any]]` | 模型决定发起的工具调用。 |
| `finish_reason` | `str | None` | 停止原因。 |

约束：

- 第一阶段不要求 `Runtime` 识别不同供应商的原始 tool calling 协议
- 进入 `Runtime` 的结果必须先被标准化为统一消息结构

## 5. 状态存储接口

### 5.1 StateStoreProtocol

当前状态存储接口已经收敛到 os 层 `StateStoreProtocol`。kernel 不直接依赖
存储协议，os Runtime 通过 `OSService` 间接提交 checkpoint。

当前最小接口包括：

| 方法 | 说明 |
| --- | --- |
| `save_session_state(session_state, save_kind) -> None` | 保存完整 SessionState 快照。 |
| `load_agent_long_term_memory(session_state, agent_name) -> str` | 加载指定 Agent 当前生效的长期记忆文本。 |
| `try_create_context_compression_task(session_state, agent_name) -> ContextCompressionTaskRecord | None` | 原子创建或抢占后台压缩任务。 |
| `finish_context_compression_task(compression_task) -> None` | 使用任务 id 和 lease_owner 完成后台压缩任务。 |
| `save_context_compression_result(session_state, agent_name, compression_result) -> None` | 读取最新状态并尝试合并后台压缩结果。 |

约束：

- `SessionState` 是唯一生效锚点
- `BaseAgentState` 当前随 `SessionState.agent_name2agent_state` 整体保存
- `Runtime.run(...)` 接收业务层已加载或创建的 SessionState，不再按 `session_id` 直接加载
- MySQL 等具体持久化实现属于业务层示例，不进入 SDK 默认实现

## 6. background 工具执行接口

### 6.1 BackgroundToolExecutor

为了统一承接 `background` 工具结果回流，除了 `ExecutableTool` 外，还需要一个最小后台执行器接口。

至少定义：

| 方法 | 说明 |
| --- | --- |
| `submit(auto_response_task_id, unit_id, tool, tool_input) -> None` | 提交后台任务，并绑定异步回流所需的 `auto_response_task_id` 与 `unit_id`。 |

约束：

- 通用 `ExecutionUnit` 的执行入口由 `ToolService.execute_unit(...)` 统一承接
- `submit(...)` 只保留给 `background` 工具结果回流
- 后台任务形成稳定结果后，应统一包装成 `tool_result` 事件并重新写入 `input_queue`
- `Runtime` 消费 `tool_result` 时，不直接恢复原 `ToolState`，而是优先匹配对应 `AutoResponseState`

## 7. complete_handoff 最小接口

### 7.1 输入 schema

第一阶段中，`complete_handoff` 至少接收：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `handoff_id` | `str` | 当前 handoff 标识。 |
| `content` | `str` | handoff 完成后的结果摘要。 |

### 7.2 输出结果

统一返回：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `ToolResultStatus` | 是否成功完成。 |
| `content` | `str` | 默认作为后续 `handoff_result` 的可消费内容。 |

约束：

- `Runtime` 只根据结构化调用结果触发 frame 回切
- 不依赖自然语言猜测 handoff 是否完成

## 8. 工具执行生命周期与前置校验接口

### 8.1 接口签名

第一阶段：

```python
before_execute(
    tool_input: BaseModel,
) -> None

validate_execute(
    tool_input: BaseModel,
) -> ValidateExecuteResult

after_execute(
    tool_input: BaseModel,
    tool_result: ToolResult,
) -> None
```

### 8.2 设计理由

- `before_execute(...)` 只承载生命周期语义，例如埋点、观测、准备动作
- `validate_execute(...)` 只负责执行前同步校验与拒绝原因
- `after_execute(...)` 只承载执行后生命周期语义，例如收尾、统计与补充观测

第一阶段 `ValidateExecuteResult` 只需要表达：

- `allowed`
- `reason`

## 9. 第一阶段最小闭环

### 9.1 RuntimeDependencies

为了让 `kernel` 在启动阶段统一接收依赖，第一阶段使用一个 `RuntimeDependencies` 聚合对象，而不是单独设计全局 provider 注册中心。

`RuntimeDependencies` 至少承接：

- `model_client_provider`
- `context_build_provider`
- `session_state_store`
- `agent_state_store`

如果第一阶段要支持 `background`，再额外接收：

- `executor`

约束：

- `kernel` 不负责自行发现 provider
- 所有依赖都由外部在启动阶段显式组装
- 第一阶段不应引入全局注册中心或 service locator
- `Runtime` 只消费已经组装好的 `RuntimeDependencies`

### 9.2 Runtime 注入方式

第一阶段：

```python
runtime = Runtime(
    root_agent=root_agent,
    dependencies=runtime_dependencies,
)
```

其中：

- `root_agent` 表达业务侧显式提供的 Agent 图入口
- `runtime_dependencies` 表达运行期依赖集合

这样可以避免：

- `Runtime.__init__(...)` 参数无限增长
- `kernel` 内部引入隐式依赖查找逻辑

### 9.3 TMRuntime 外层封装

为了封装 `os` 层复杂度，第一阶段采用在 `kernel.Runtime` 外层增加一层 `TMRuntime` 或等价包装对象，而不是默认通过继承大量重写 `Runtime`。

边界：

- `kernel.Runtime` 保持最小、稳定、可复用
- `os` 负责创建 `RuntimeDependencies`
- `os` 负责构造 `Runtime`
- 如有需要，由 `TMRuntime` 进一步封装启动参数、配置读取和依赖组装

### 9.4 Runtime 可选扩展边界

如果后续确实需要让 `os` 影响 `Runtime` 行为，只开放少量稳定钩子，而不是允许子类重写整个主循环。

当前阶段可以接受的扩展方式是：

- 组合优先
- 钩子式扩展次之
- 大量继承和重写主循环最后再考虑

如果要先把 `kernel` 原型跑起来，最少实现：

1. `Agent`
2. `OSService`
3. `ModelClientProtocol`
4. `StateStoreProtocol`
5. `ContextBuildProvider`
6. `ToolService`
8. `ExecutableTool`

如果第一阶段就要支持 `background`，再补：

9. `BackgroundToolExecutor`

如果要让业务侧更顺手接入，`os` 层额外提供：

10. `Agent`
11. `ToolService`

其中：

- `Agent` 负责默认接上 `ToolService`
- `ToolService` 负责提供默认的 `ExecutionUnit` 构造策略

## 10. 当前版本结论

当前版本的核心判断是：

- `Agent`、模型调用、状态存储、后台执行器，是 `kernel` 原型真正的阻塞级接口
- 这些接口一旦稳定，`Runtime` 就可以围绕现有数据结构跑通最小闭环
- 更复杂的治理、排序、压缩、权限和产品逻辑，仍应继续留在 `os`
