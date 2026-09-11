# turtlemap kernel 数据结构设计

## 1. 文档目标

本文聚焦 `turtlemap kernel` 的核心数据结构设计。

本文不展开完整运行流程，也不讨论 `os` 层治理、编排和产品接口，而是回答下面几个问题：

- `kernel` 第一阶段需要稳定哪些核心数据结构
- 这些数据结构分别表达什么运行语义
- 哪些字段应参与持久化，哪些字段只属于运行时补全
- 枚举值如何收敛，避免在实现中散落裸字符串
- 后续 Python 模型实现时应遵守哪些边界

这份文档应作为后续 `turtlemap/kernel/` 代码模型的直接设计依据。

## 2. 设计原则

### 2.1 数据结构服务于 Runtime 闭环

`kernel` 的数据结构不是静态 DTO 集合，而是服务于 `Runtime` 的运行闭环。

因此每个核心结构都应至少回答一个问题：

- 输入如何进入当前轮次
- 当前会话现场如何保存
- 当前 Owner Agent 如何推导
- Agent 当前主观状态如何表达
- 未完成任务现场如何挂起与恢复
- 工具调用如何被描述、执行和回流
- 上下文构建需要从哪里读取信息

### 2.2 调度语义和业务语义分离

`kernel` 中的枚举优先表达运行时调度语义，而不是具体业务分类。

例如：

- `ObservableEvent.event_type` 表达 `Runtime` 如何处理事件
- 具体环境事件类型、业务消息类型、外部系统事件分类放入 `payload` 或由 `os` 层解释

这样可以避免 `kernel` 随业务场景变化而频繁扩展枚举。

### 2.3 持久化状态和运行时状态分离

`SessionState` 是会话级状态根，但并不是其中所有字段都应直接持久化。

当前阶段遵循以下原则：

- 会话连续性所需的最小信息应持久化
- 可由外部存储重新加载的运行时对象不直接持久化
- 面向 LLM 输入构建的近期记录进入 `History`
- 完整审计、展示流、调试 trace 由 `os` 层独立通道承接

### 2.4 第一阶段保持字段少而稳

第一阶段的数据结构优先保证闭环可跑通、状态可恢复、边界可解释。

暂不进入 `kernel` 基础模型的内容包括：

- 权限治理细节
- tracing 字段
- 复杂重试、熔断和限流策略
- 完整长期记忆本体
- 产品端展示结构
- 环境建模细节
- 通用 `metadata` 扩展字段

### 2.5 id 命名约定

系统内各类业务 id 当前阶段统一使用 `*_id` 字段命名，字段值采用 `业务前缀 + ":" + UUID` 的字符串格式。

其中：

- `xxxx` 表示业务前缀
- `:` 后为标准 `UUID` 字符串

例如：

- `session:238ab4c8-...`
- `agent:238ab4c8-...`
- `task:238ab4c8-...`

当前阶段建议至少统一以下 id 字段：

- `session_id`
- `task_id`
- `input_id`
- `event_id`
- `handoff_id`

约束：

- 字段名仍保持语义化命名，例如 `session_id`、`agent_name`、`task_id`
- 字段值统一遵守 `业务前缀 + ":" + UUID` 的格式
- 不同业务类型的 id 不共用前缀，避免跨类型混淆

## 3. 核心对象总览

第一阶段建议围绕以下核心对象建模：

```text
Input
└── events: list[ObservableEvent]

SessionState
├── agent_frames: list[AgentFrame]
├── input_queue: list[Input]
└── agent_name2agent_state: dict[str, BaseAgentState]

Agent
├── agent_name: str
├── system: SystemDefinition
├── tools: list[ExecutableTool | function]
└── handoffs: list[Agent]

BaseAgentState
├── system: SystemDefinition
├── memory: MemoryView
│   ├── long_term_memory: str
│   └── mid_term_memory: str
├── history: list[RuntimeArtifact]
└── processing_tasks: list[BaseProcessingTask]
    └── state: BaseTaskState
        ├── MessageState
        ├── ToolState
        ├── HandoffState
        └── AutoResponseState

ToolService
└── tools: dict[str, ExecutableTool]

ExecutableTool
├── tool_metadata: ToolMetadata
├── __call__(...)
├── before_execute(...)
├── validate_execute(...)
└── after_execute(...)

ToolState
└── execution_units: list[ExecutionUnit]

ContextBuildProvider
└── build(...)

ContextEngineering(os)
├── ContextBuildInput
└── ContextBuildResult
```

其中：

- `Agent` 表达业务代码显式创建的 Agent 运行时定义对象
- `SessionState` 表达一次会话的客观现场
- `BaseAgentState` 表达某个 Agent 在当前会话中的主观状态
- `BaseProcessingTask` 表达 Agent 当前未完成的任务现场
- `AgentFrame` 表达会话控制权如何在 Agent 之间切换
- `ObservableEvent` 表达已经进入 `kernel` 的可感知变化
- `ToolMetadata` 表达模型可理解、Runtime 可调用的工具描述
- `ContextBuildInput` 和 `ContextBuildResult` 属于 os 层上下文工程模型，不进入 kernel 核心数据结构

## 4. 枚举设计

所有 `kernel` 枚举在 Python 实现中都应继承 `str` 和 `Enum`。

建议统一写法如下：

```python
from enum import Enum


class EventType(str, Enum):
    """事件调度类型。"""

    USER_INPUT = "user_input"
```

约束：

- 枚举类必须继承 `str, Enum`，不能只继承 `Enum`
- 枚举值统一使用小写下划线字符串，保证序列化结果稳定、可读
- 业务代码中应使用枚举成员，不应散落裸字符串
- 对外序列化、持久化和日志展示时默认使用枚举值字符串

### 4.1 EventType

`EventType` 表达事件对 `Runtime` 的调度语义。

| 枚举值 | 语义 |
| --- | --- |
| `user_input` | 用户主动输入，通常触发新任务、任务补充或任务恢复。 |
| `tool_result` | 工具执行结果回流，用于恢复和推进已有 `ToolState`。 |
| `handoff_result` | handoff 完成结果回流，用于回切 `AgentFrame`，并在需要时恢复原 Agent 的 `HandoffState`。 |
| `environment_message` | 环境变化进入 Agent 感知域，具体环境类型由 `os` 层和 `payload` 承接。 |

约束：

- 不增加 `agent_result`，子 Agent 被当作能力调用时应封装成 `tool_result`
- 中断、暂停、取消等系统控制不通过 `ObservableEvent` 表达，应走 `Runtime` 控制 API

### 4.2 EventSource

`EventSource` 表达事件来自哪里。

| 枚举值 | 语义 |
| --- | --- |
| `user` | 来自用户输入。 |
| `tool` | 来自工具执行结果。 |
| `handoff` | 来自 handoff 完成信号。 |
| `system` | 来自系统内部生成的事件。 |
| `environment` | 来自环境感知或外部系统变化。 |

约束：

- `source` 只表达来源类型
- 具体来源对象用 `source_id` 表达

### 4.3 TaskStateKind

`TaskStateKind` 是 `BaseProcessingTask.state` 的判别字段，用于序列化、反序列化和恢复分派。

| 枚举值 | 语义 |
| --- | --- |
| `message` | 正在生成或等待确认的一条 assistant message。 |
| `tool` | 正在推进的 tool loop。 |
| `handoff` | 原 Agent 正在等待 handoff 完成。 |
| `auto_response` | 事件触发型自动响应任务。 |

### 4.4 TaskStatus

`TaskStatus` 表达任务状态的通用生命周期。

| 枚举值 | 语义 |
| --- | --- |
| `running` | 正在执行。 |
| `paused` | 已暂停，等待后续显式恢复或任务跳转。 |
| `completed` | 已完成。 |

约束：

- 同一 Owner Agent 在一次调度中最多只能有一个 `running` 任务
- `paused` 表示任务现场被完整保留，但当前不会自动参与主循环执行
- 任务执行过程中的中间推进统一使用 `running`

### 4.5 ToolExecutionStrategy

`ToolExecutionStrategy` 表达 `Runtime` 调用工具后的等待策略。

| 枚举值 | 语义 |
| --- | --- |
| `sync` | 同步等待工具结果，并继续推进 tool loop。 |
| `async` | 异步提交工具任务，当前轮写入用户可见占位回复并暂停，等待后续响应恢复。 |

### 4.6 ToolResultStatus

`ToolResultStatus` 表达工具执行结果状态。

| 枚举值 | 语义 |
| --- | --- |
| `success` | 工具执行成功，`content` 可继续作为 LLM 的 tool message 内容。 |
| `failed` | 工具执行失败，`error` 应包含可解释错误信息。 |
| `pending` | 工具已受理但尚未完成，通常配合 `async` 策略使用。 |

### 4.7 ExecutionUnitStatus

`ExecutionUnitStatus` 表达执行单元的生命周期状态。

| 枚举值 | 语义 |
| --- | --- |
| `pending` | 尚未开始执行。 |
| `running` | 正在执行。 |
| `completed` | 已完成。 |
| `failed` | 已失败。 |

### 4.8 MessageRole

`MessageRole` 表达进入 `History` 或 LLM 上下文的消息角色。

| 枚举值 | 语义 |
| --- | --- |
| `system` | 系统消息，通常由 `SystemDefinition` 转换得到。 |
| `user` | 用户消息。 |
| `assistant` | assistant 消息，可以是自然语言回复，也可以包含 `tool_calls`。 |
| `tool` | 工具结果消息，需要通过 `tool_call_id` 对齐对应工具调用。 |

### 4.9 TaskSwitchAction

`TaskSwitchAction` 表达 `ExecutionUnitResult` 对 `Runtime` 发出的任务级切换指令。

| 枚举值 | 语义 |
| --- | --- |
| `pause` | 暂停当前任务并结束本次运行，等待后续 response 恢复。 |

约束：

- 该枚举只表达 `Runtime` 必须接管的任务级调度动作
- 不承载 `confirmed`、`rejected`、`clarify` 等 unit 内部业务语义
- 默认值应为 `None`，仅在确实需要触发任务切换时填写

## 5. 输入结构

### 5.1 Input

`Input` 表示一次进入 `Runtime` 输入队列、并可持久化到 `SessionState.input_queue` 的输入包。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `input_id` | `str` | 是 | 是 | 输入包 id，用于标识一次进入队列的输入。 |
| `events` | `list[ObservableEvent]` | 是 | 是 | 当前输入包中的事件集合。 |

约束：

- `Input` 进入 `SessionState.input_queue` 后，由 Runtime 出队并固定写入 `BaseProcessingTask.start_input`
- `Input.events` 可以包含多个事件，但第一阶段不要求实现复杂事件系统

### 5.2 ObservableEvent

`ObservableEvent` 是 `kernel` 统一消费外部输入的标准事件结构。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `event_id` | `str` | 是 | 是 | 事件 id，用于标识一次独立可感知变化。 |
| `event_type` | `EventType` | 是 | 是 | 事件调度类型，只表达 `Runtime` 处理差异。 |
| `source` | `EventSource` | 是 | 是 | 事件来源类型。 |
| `source_id` | `str | None` | 否 | 是 | 来源侧对象 id，例如 `tool_call_id`、`handoff_id`、`timer_id`。 |
| `priority` | `int` | 否 | 是 | 处理优先级，数值越小优先级越高。默认可为 `100`。 |
| `payload` | `dict[str, Any]` | 是 | 是 | 事件内容，由对应事件类型和 `os` 层共同约束。 |

约束：

- `event_type` 不表达业务分类
- `source_id` 表达来源对象，不表达任务归属
- 是否需要回复、是否对用户可见等治理语义第一阶段不进入基础字段

## 6. 会话状态结构

### 6.1 SessionState

`SessionState` 是 `Runtime` 状态管理根，表示一次会话的客观现场。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `agent_frames` | `list[AgentFrame]` | 是 | 是 | Agent 调用栈，必须至少包含一个 root frame。 |
| `input_queue` | `list[Input]` | 否 | 是 | 尚未出队处理、但已经进入会话状态的待处理输入队列。 |
| `agent_name2agent_state` | `dict[str, BaseAgentState]` | 是 | 是 | 当前会话已加载的 AgentState 映射，随 SessionState 保存。 |

约束：

 - 当前 Owner Agent 由 `agent_frames[-1].agent_name` 推导
- `BaseSessionState` 不内置 `session_id`、`version`、`schema_version`；这些由 os 或业务层派生模型承载
- `BaseAgentState` 当前直接随 `agent_name2agent_state` 保存，不再由独立版本表引用
- 不额外保存 `owner_agent_name`，避免双写不一致
- `agent_frames[0]` 必须是 root frame
- `input_queue` 表示已持久化但尚未被当前轮接管的输入；当前任务起点输入由 `BaseProcessingTask.start_input` 固定保存
- `tool_result`、`handoff_result`、定时触发结果等若要进入下一轮循环，应先包装成 `Input` 写入 `input_queue`
- `os` 层若保存基于会话的派生记录，应由业务层 SessionState 子类或外部 persistence model 显式承载身份与版本
- 如果检查到某个 frame 的 `agent_name` 无法加载对应 `BaseAgentState`，应从该 frame 开始弹出后续 frame

### 6.2 AgentFrame

`AgentFrame` 表达会话控制权切换。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `agent_name` | `str` | 是 | 是 | 当前 frame 对应的 Agent 名称。 |
| `from_agent_name` | `str | None` | 否 | 是 | 由哪个 Agent 切换而来，root frame 为 `None`。 |
| `handoff_id` | `str | None` | 否 | 是 | 当前 handoff id，root frame 为 `None`。 |
| `handoff_objective` | `str | None` | 否 | 是 | handoff 的任务目标，root frame 为 `None`。 |
| `completion_tool` | `str | None` | 否 | 是 | Runtime 注入的 handoff 完成控制工具，第一阶段固定为 `complete_handoff`。 |
| `return_task_id` | `str | None` | 否 | 是 | handoff 完成后需要恢复的原 Agent 任务 id。 |

约束：

- root frame 的 `from_agent_name`、`handoff_id`、`handoff_objective`、`completion_tool`、`return_task_id` 都应为 `None`
- `return_task_id = None` 表示 handoff 完成后只回切 Owner Agent，不自动恢复任务
- `Runtime` 只根据 `completion_tool` 的结构化完成信号回切，不解析自然语言作为完成条件

### 6.3 BaseProcessingTask.start_input

`BaseProcessingTask.start_input` 表示启动当前任务时被 Runtime 消费的完整输入包。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `start_input` | `Input | None` | 否 | 是 | 启动当前任务时消费的输入包；任务生命周期内固定不变。 |

约束：

- `BaseSessionState` 不再保存当前输入现场。
- 未处理输入只保存在 `input_queue` 中。
- 新任务创建时从 `input_queue` 出队并写入 `BaseProcessingTask.start_input`。
- `start_input` 是任务起点，不随任务执行过程中的新增输入变化。
- 任务执行过程中需要等待或接收的新输入，应由具体 task state 自行扩展字段承载。

## 7. Agent 结构

### 7.1 Agent

`Agent` 表达业务代码显式创建并传入 `Runtime` 的运行时定义对象。

它不属于会话快照的一部分，而是 `Runtime` 在启动阶段解析和持有的运行时对象。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `agent_name` | `str` | 是 | 否 | Agent 稳定名称，是运行时查找主标识。 |
| `system` | `SystemDefinition` | 是 | 否 | 当前 Agent 的结构化系统定义。 |
| `tools` | `list[ExecutableTool | function]` | 否 | 否 | Agent 初始接收的工具集合。 |
| `handoffs` | `list[Agent]` | 否 | 否 | 允许切换到的下游 Agent。 |

约束：

- `Agent` 是全局运行时定义，不等同于会话内的 `BaseAgentState`
- `Agent` 不参与 `SessionState` 持久化；会话持久化 `AgentFrame`、`input_queue` 和 `agent_name2agent_state`
- `tools` 最终都应被装配成统一的 `ExecutableTool`
- `handoffs` 直接持有 `Agent` 引用，而不是字符串名称
- `Runtime` 解析 Agent 图时，应按 `agent_name` 去重
- os 层 Agent 持有统一 `TurtleMapConfig`，并从中读取 LLM 与上下文预算配置

### 7.2 BaseAgentState

`BaseAgentState` 表达某个 Agent 在当前会话中的主观状态。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `agent_name` | `str` | 是 | 是 | Agent 名称。 |
| `system` | `SystemDefinition` | 是 | 是 | 结构化系统定义。 |
| `memory` | `MemoryView` | 否 | 是 | 当前轮记忆上下文视图，承接长期记忆与中期记忆的最小可用表达。 |
| `history` | `list[RuntimeArtifact]` | 是 | 是 | 当前 Agent 可见的历史产物本体，供 os 层上下文构建使用。 |
| `processing_tasks` | `list[BaseProcessingTask]` | 是 | 是 | 当前未完成任务现场集合。 |

约束：

- `BaseAgentState` 属于当前会话，不等同于全局 Agent 定义
- `processing_tasks` 只保存未完成或需要恢复的任务现场
- 已完成且已经对用户形成明确回复的内容应进入 `history`

### 7.3 SystemDefinition

`SystemDefinition` 是结构化系统级定义，不应退化成一段普通 prompt 字符串。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `role` | `str` | 是 | 是 | Agent 自我定位。 |
| `objective` | `str` | 是 | 是 | Agent 当前目标。 |
| `constraints` | `str` | 否 | 是 | 行为约束，可承载上下文使用约束和工具使用约束。 |
| `input_format` | `str | None` | 否 | 是 | 输入格式约束。 |
| `output_format` | `str | None` | 否 | 是 | 输出格式约束。 |
| `examples` | `list[dict[str, Any]]` | 否 | 是 | 示例集合。 |

约束：

- 顶层字段保持少而稳
- 复杂行为优先收敛到结构化 `constraints`
- `os` 层如需扩展系统定义，可通过子类或外部结构承接

### 7.4 MemoryView

`MemoryView` 是当前 Agent 面向当前会话和任务的分层记忆上下文视图。

当前阶段，`MemoryView` 更适合被理解为 Agent 的稳定记忆视图，而不是纯动态检索结果视图。

其中：

- `long_term_memory`：承接长期稳定偏好、身份信息、项目背景等长期有效内容
- `mid_term_memory`：承接会话历史摘要、阶段性任务总结、当前会话沉淀出的用户意图/约束等可滚动重写内容

需要注意的是，这两个字段虽然都出现在 `BaseAgentState.memory` 中，但运行时权威来源并不完全一致：

- `long_term_memory`：跨会话主数据，每次 Runtime 启动时应从 `StateStoreProtocol` 单独加载到当前运行态
- `mid_term_memory`：当前会话工作记忆，直接随 `BaseAgentState` 快照恢复

这两层都可以采用纯文本内容载体，但文本内部应保持结构化章节，便于 LLM 重写、合并和压缩。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `long_term_memory` | `str` | 否 | 是 | 长期记忆文本，承接长期稳定偏好、身份信息、项目背景等可长期复用的信息；运行时每轮以 `StateStoreProtocol` 单独加载结果为准。 |
| `mid_term_memory` | `str` | 否 | 是 | 中期记忆文本，承接会话摘要、阶段总结、当前会话沉淀出的用户意图/约束等可滚动重写内容。 |

约束：

- `MemoryView` 当前不等同于“动态检索结果集合”
- `long_term_memory` 和 `mid_term_memory` 都优先按结构化纯文本记忆理解
- `long_term_memory` 和 `mid_term_memory` 都可以直接承载一整段带章节结构的 markdown 文本
- 会话摘要如果需要进入当前上下文，应直接沉淀到 `mid_term_memory` 文本中
- `history` 应保留原始、稳定的最近消息窗口，不应混入运行时生成的摘要消息
- 若后续需要按当前回答动态补充外部知识，应单独建模为 `retrieved_context` 或等价概念，而不是混入 `MemoryView`
- 短期用户可见交互记录由 `History` 承接，不和 `MemoryView` 混放
- `mid_term_memory` 可以直接跟随 `BaseAgentState` 一起持久化并作为恢复来源
- `long_term_memory` 虽然也会随 `BaseAgentState` 一起持久化，但主要用于 checkpoint、回放与 observation 溯源；新的 Runtime 运行时仍应从 `StateStoreProtocol` 重新加载

### 7.6 RuntimeArtifact

`RuntimeArtifact` 表达 Runtime 流程中由 os 层生成的稳定产物引用。

kernel 只依赖产物类型进行流程控制，不解析 `payload` 的内部结构。
`payload` 由 os 层按具体模型、协议或多模态载荷自行定义和消费。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | `str` | 是 | 是 | 产物唯一标识。 |
| `type` | `RuntimeArtifactType` | 是 | 是 | 产物类型，kernel 只根据该字段做必要流程控制。 |
| `payload` | `Any` | 是 | 是 | os 层产物载荷，kernel 不解析。 |

约束：

- `RuntimeArtifact.type` 当前区分 `INPUT`、`ASSISTANT_MESSAGE`、`TOOL_CALL`、`TOOL_CALL_EXE`、`TOOL_LLM_RESPONSE`。
- `RuntimeArtifact.payload` 对 kernel 不透明；os 层按产物类型恢复为 `Input`、`LLMMessage`、`ToolCallExecutionUnit` 或 `LLMCallExecutionUnit` 等真实对象。
- kernel 不应读取或修改 `payload` 的具体字段。
- 未完成的内部执行过程不应提前进入 `history`。

## 8. 任务状态结构

### 8.1 BaseProcessingTask

`BaseProcessingTask` 表达某个 Agent 当前未完成的任务现场。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `task_id` | `str` | 是 | 是 | 任务 id，用于异步事件回流和恢复匹配。 |
| `state` | `BaseTaskState` | 是 | 是 | 当前任务状态，按 `kind` 分派到具体状态类型。 |
| `start_input` | `Input \| None` | 否 | 是 | 启动当前任务时消费的输入包；任务生命周期内固定不变。 |

约束：

- `BaseProcessingTask` 属于 `BaseAgentState`
- `BaseProcessingTask` 不表达 Agent 调用栈，Agent 切换由 `AgentFrame` 表达
- `start_input` 用于保留任务起点输入，后续状态推进和恢复应优先使用该现场，而不是重新拼接输入

### 8.2 BaseTaskState

`BaseTaskState` 是任务状态基类。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `kind` | `TaskStateKind` | 是 | 是 | 状态判别字段。 |
| `status` | `TaskStatus` | 是 | 是 | 通用生命周期状态。 |
| `error` | `str | None` | 否 | 是 | 失败时的可解释错误信息。 |

约束：

- 序列化和反序列化必须根据 `kind` 分派
- `status = failed` 时应尽量提供 `error`

### 8.3 MessageState

`MessageState` 继承 `BaseTaskState`，承接一次 assistant 产物生成任务。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `message` | `RuntimeArtifact | None` | 否 | 是 | 已稳定生成的 assistant 产物。 |
| `origin_input_id` | `str | None` | 否 | 是 | 触发该 message 的输入 id。 |

约束：

- 继承 `BaseTaskState.kind`，固定为 `message`
- 继承 `BaseTaskState.status` 和 `BaseTaskState.error`
- `MessageState` 不保存流式增量；过程输出由 os event bus 承接
- `message` 只保存稳定 `RuntimeArtifact`，其中 `payload` 对 kernel 不透明
- 恢复时基于最近 checkpoint 中保存的任务现场继续执行
- 完成且结果确认为 `tool_calls` 后，应切换为 `ToolState`

### 8.4 ToolState

`ToolState` 继承 `BaseTaskState`，承接整个 tool loop 的执行过程。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `tool_call_message` | `RuntimeArtifact` | 是 | 是 | 进入当前 tool loop 时的起始 assistant tool call 产物。 |
| `execution_units` | `list[ExecutionUnit]` | 是 | 是 | 按执行顺序展开的执行单元列表。 |
| `origin_input_id` | `str | None` | 否 | 是 | 触发该 tool loop 的输入 id。 |

约束：

- 继承 `BaseTaskState.kind`，固定为 `tool`
- 继承 `BaseTaskState.status` 和 `BaseTaskState.error`
- `MessageState -> ToolState` 切换时，应基于 `tool_calls` 一次性生成完整 `execution_units`
- 一个工具调用可展开为一个或多个执行单元，例如 `tool_call`、`user_confirm`、`llm_call`
- `Runtime` 恢复时不依赖额外 `phase` 字段，而是顺序扫描 `execution_units`，找到下一个未完成 unit 继续执行
- 所有 unit 均完成后，`ToolState` 才算完成
- `ToolState.tool_call_message` 只保存进入 tool loop 时的起始 assistant 产物；执行阶段产生的 tool / placeholder / continuation 消息应优先通过 `execution_units` 及其 `result` 恢复

### 8.5 ExecutionUnit

`ExecutionUnit` 表达 `ToolState` 中的一个可恢复执行单元基类。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `unit_id` | `str` | 是 | 是 | 执行单元 id。 |
| `unit_type` | `str` | 是 | 是 | 执行单元类型。 |
| `status` | `ExecutionUnitStatus` | 是 | 是 | 执行单元状态。 |
| `result` | `ExecutionUnitResult | None` | 否 | 是 | 执行单元执行结果。 |

约束：

- `execution_units` 的顺序就是 `Runtime` 的默认执行顺序
- `ExecutionUnit` 只描述可恢复执行现场，不负责具体执行实现
- `ExecutionUnit` 不单独保存 `error`，失败信息进入对应 `ExecutionUnitResult`
- `pending` 表示该 unit 尚未启动，`Runtime` 可直接执行
- 需要外部输入或异步结果时，任务应切换为 `paused` 并挂载 `InterruptionRequest`；对应 unit 保持 `pending`，收到 response 后再恢复任务执行
- `unit_type` 统一采用 `str`，`kernel` 只约定字段，不限定完整类型集合
- 序列化和反序列化应根据 `unit_type` 分派到 `os` 层定义的具体子类
- 具体执行计划、查询、重试、确认和继续推理由 `os` 层执行器承接

`ToolCallExecutionUnit` 是 os 层真实工具调用执行单元，当前字段包括：

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `tool_meta_id` | `str` | 是 | 是 | 被调用工具的 `ToolMetadata.id`。 |
| `tool_call` | `LLMCompletionToolCall` | 是 | 是 | 模型生成的完整工具调用信息。 |

约束：

- 工具调用参数不放在 `ExecutionUnit` 基类中。
- `ToolCallExecutionUnit` 执行时从 `tool_call.function.arguments` 解析真实参数。
- 无参工具使用 `EmptyToolInputModel` 作为输入模型，参数解析阶段可直接使用空对象。

补充说明：

- `kernel` 内建只需理解少量约定类型名，例如 `tool_call`、`user_confirm`、`llm_call`
- `os` 可以在此基础上继续扩展新的 `unit_type`
- 对于确认类 unit，确认、拒绝和补充信息等语义由 `InterruptionRequest` / response 的具体 payload 表达，`kernel` 不新增 unit 级等待状态

### 8.6 ExecutionUnitResult

`ExecutionUnitResult` 是 `ExecutionUnit` 的统一结果基类。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `unit_type` | `str` | 是 | 是 | 当前结果对应的执行单元类型。 |
| `next_unit_status` | `ExecutionUnitStatus` | 是 | 是 | 当前执行结果提交后，unit 应切换到的下一状态。 |
| `appended_execution_units` | `list[ExecutionUnit]` | 是 | 是 | 当前结果要求追加到执行链末尾的新执行单元列表。 |
| `task_switch_action` | `TaskSwitchAction \| None` | 否 | 是 | 当前结果要求 Runtime 执行的任务级切换动作。 |
| `raw_data` | `dict[str, Any] | None` | 否 | 是 | 执行结果的原始结构化数据。 |
| `error` | `str | None` | 否 | 是 | 执行失败时的错误信息。 |

约束：

- `ExecutionUnit.result` 固定为 `ExecutionUnitResult` 或其子类
- `Runtime` 在收到 `ExecutionUnitResult` 后，应直接按 `next_unit_status` 更新对应 `ExecutionUnit.status`
- `Runtime` 在执行阶段只负责回填 `result`、更新 `status`、追加 `appended_execution_units` 并建立 checkpoint，不直接写入 `History`
- 任务进入 `completed` 后的后续推进只消费既有“任务完成 checkpoint”，负责写入 `History`、切换下一状态或清理任务现场，不再为这类收尾动作重复建立 checkpoint
- `task_switch_action` 只承接任务级调度信号；是否确认、是否拒绝、是否需要澄清等 unit 内部业务语义，不进入该基类字段
- 不同 `os` 层 `ExecutionUnit` 子类应返回对应的 `ExecutionUnitResult` 子类
- 例如：`ToolCallExecutionUnit -> ToolCallExecutionResult`，`LLMCallExecutionUnit -> LLMCallExecutionResult`
- 需要等待外部响应时，os 层执行器通过 `task_switch_action = pause` 和中断 request 显式暂停任务；response 到达后由 Runtime 恢复原 `pending` unit
- `switch_topic` 场景下，`os` 层结果子类应通过 `task_switch_action` 明确要求 Runtime 暂停当前任务并创建新任务
- 序列化和反序列化应根据 `unit_type` 分派到 `os` 层定义的具体结果子类

### 8.7 HandoffState

`HandoffState` 继承 `BaseTaskState`，表达原 Agent 等待 handoff 完成的任务现场。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `handoff_id` | `str` | 是 | 是 | handoff id，用于匹配 `handoff_result`。 |
| `target_agent_name` | `str` | 是 | 是 | 接管会话控制权的目标 Agent 名称。 |
| `objective` | `str` | 是 | 是 | handoff 目标。 |
| `origin_input_id` | `str | None` | 否 | 是 | 触发 handoff 的输入 id。 |

约束：

- 继承 `BaseTaskState.kind`，固定为 `handoff`
- 继承 `BaseTaskState.status` 和 `BaseTaskState.error`，通常处于 `running`
- `HandoffState` 不主动生成回复
- handoff 是否完成由目标 Agent 调用 `complete_handoff` 决定
- 会话控制权切换由 `AgentFrame` 表达，不由 `HandoffState` 表达

### 8.8 AutoResponseState

`AutoResponseState` 继承 `BaseTaskState`，表达事件触发型自动响应任务。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `trigger_event_type` | `EventType | None` | 否 | 是 | 期待触发的事件类型。 |
| `trigger_source_id` | `str | None` | 否 | 是 | 期待触发的来源对象 id。 |
| `payload` | `dict[str, Any] | None` | 否 | 是 | 自动响应任务运行所需的结构化负载，建议使用带 `type` 判别字段的 `TypedDict union`。 |
| `objective` | `str` | 是 | 是 | 自动响应目标。 |

约束：

- 继承 `BaseTaskState.kind`，固定为 `auto_response`
- 继承 `BaseTaskState.status` 和 `BaseTaskState.error`，通常处于 `running`
- 恢复时通常继续等待匹配事件
- 对 `trigger_event_type = tool_result` 的场景，`payload` 需要保存异步 unit 跟踪信息与原始恢复材料
- 具体触发规则第一阶段可以保持简单，由后续状态机文档继续细化

## 9. 工具结构

### 9.1 ToolService

`ToolService` 是 `kernel` 的 Agent 维度工具服务视图，用于承接当前可用工具对象。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `tools` | `dict[str, ExecutableTool]` | 是 | 否 | 运行时工具定义表，key 为 `ToolMetadata.id`。 |

约束：

- `ToolService` 默认由 `os` 提供具体实现，`kernel` 只依赖最小调用约定
- `ToolService` 是运行时工具管理视图，默认不作为 `SessionState` 的一部分持久化
- `Runtime` 通过 `ToolService` 查找工具定义，再调用工具执行入口

### 9.2 ExecutableTool

`ExecutableTool` 表达一个完整工具对象，由可序列化元信息、运行时可调用对象和可重写钩子方法组成。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `tool_metadata` | `ToolMetadata` | 是 | 是 | 工具结构化元信息。 |
| `input_model` | `type[Any]` | 是 | 否 | 工具输入模型，作为参数校验和 `json_schema` 生成的唯一真相源。 |
| `execution_strategy` | `ToolExecutionStrategy` | 是 | 否 | 工具执行等待策略。 |

| 方法 | 返回值 | 必须实现 | 说明 |
| --- | --- | --- | --- |
| `__call__(...)` | `ToolResult` | 是 | 工具统一执行入口。 |
| `before_execute(...)` | `None` | 否 | 执行前生命周期回调，不负责控制是否允许执行。 |
| `validate_execute(...)` | `ValidateExecuteResult` | 否 | 执行前校验方法，只负责同步、确定性的本地校验。 |
| `after_execute(...)` | `None` | 否 | 执行后生命周期回调，不负责改写主执行结果。 |

约束：

- `Runtime` 统一通过 `ExecutableTool.__call__(...)` 调用工具
- `input_model` 是工具参数结构的唯一真相源；缺失时应直接报错
- `ToolMetadata.input_schema` 应由 `input_model` 自动生成
- 自动生成的 `input_schema` 应以 `input_model` 字段为顶层参数，不额外包一层对象字段名
- 工具语义描述的唯一真相源应是 `ToolDescriptor`
- 工具对模型暴露的说明应由 `ToolDescriptor.role`、`ToolDescriptor.objective` 等结构化字段按固定模板整合生成
- `before_execute(...)` 是运行时钩子方法，不是可序列化字段
- `before_execute(...)` 是 `os` 扩展治理能力的挂载点
- `execution_strategy = async` 时由 Runtime 通过中断 request 保存等待现场
- 简单式注册只用于“函数 + 基础 metadata”的声明式工具，不提供 `before_execute(...)` 等扩展入口
- 简单式注册仍需显式提供 `ToolDescriptor`
- `tool` 装饰器第一阶段只支持普通函数；若装饰对象不是普通函数，应直接拒绝注册
- 一旦需要钩子、占位回复或业务上下文封装，应改用 `ExecutableTool` 子类实例
- `ExecutableTool` 内部的方法不支持再用 `tool` 装饰器装饰
- 不做函数签名推导，不做 docstring 参数解析
- 权限、重试、熔断、审计等策略可由 `os` 层通过扩展 `ToolMetadata` 和重写 `before_execute(...)` 承接

### 9.3 ToolDescriptor

`ToolDescriptor` 表达工具创建侧的结构化语义描述对象。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | `str` | 是 | 否 | 工具名称。 |
| `role` | `str` | 是 | 否 | 工具在任务协作中的角色定位。 |
| `objective` | `str` | 是 | 否 | 工具用于解决的核心问题。 |
| `use_cases` | `list[str]` | 否 | 否 | 适用场景。 |
| `anti_use_cases` | `list[str]` | 否 | 否 | 不适用场景。 |
| `examples` | `list[dict[str, Any]]` | 否 | 否 | 调用示例。 |
| `tags` | `list[str]` | 否 | 否 | 标签。 |

约束：

- `ToolDescriptor` 只服务创建侧和装配侧，不直接作为 `kernel` 运行时元信息使用
- `ToolMetadata` 应由 `ToolDescriptor` 标准化生成
- `execution_strategy` 不属于 `ToolDescriptor`，应放在 `ExecutableTool` 或装配配置侧
### 9.4 ValidateExecuteResult

`ValidateExecuteResult` 表达工具执行前校验的最小返回结果。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `allowed` | `bool` | 是 | 否 | 是否允许继续执行。 |
| `reason` | `str | None` | 否 | 否 | 不允许执行或需要提示时的原因。 |

约束：

- 第一阶段只需要表达是否允许执行
- 更复杂的权限、审计和风险判断结果由 `os` 层扩展

### 9.5 ToolMetadata

`ToolMetadata` 是 `kernel` 对外部可调用能力的统一描述。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | `str` | 是 | 是 | 工具唯一 id。 |
| `name` | `str` | 是 | 是 | 工具名称。 |
| `version` | `str` | 否 | 是 | 工具版本。 |
| `namespace` | `str | None` | 否 | 是 | 工具命名空间。 |
| `execution_strategy` | `ToolExecutionStrategy` | 是 | 是 | 工具执行等待策略。 |
| `input_schema` | `dict[str, Any]` | 是 | 是 | 由 `input_model` 自动生成的工具输入 schema。 |

约束：

- 表示工具调用的 `ExecutionUnit` 中，其 `tool_meta_id` 应指向 `ToolMetadata.id`
- `kernel` 只理解基础字段、`ExecutableTool.__call__(...)`、`before_execute(...)`、`validate_execute(...)` 和 `after_execute(...)` 的调用约定
- `ToolMetadata` 只保存 `kernel` 运行时需要的最小结果，不重复保留 `ToolDescriptor` 的结构化字段
- 无参工具应显式使用 `EmptyToolInputModel`，以便 os 层仍能统一绑定工具输入上下文
- 权限、重试、熔断、审计等复杂治理字段由 `os` 通过子类或扩展结构承接
- `execution_strategy = async` 时由 Runtime 通过中断 request 保存等待现场

### 9.6 ToolResult

`ToolResult` 是 `kernel` 层统一工具函数返回结果，不属于 `ExecutionUnitResult` 体系。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `status` | `ToolResultStatus` | 是 | 是 | 工具执行结果状态。 |
| `content` | `str` | 是 | 是 | 默认作为后续传给 LLM 的 tool message 内容。 |
| `raw_data` | `dict[str, Any] | None` | 否 | 是 | 程序侧透传数据，`kernel` 不统一解释。 |
| `error` | `str | None` | 否 | 是 | 失败时的可解释错误信息。 |

约束：

- `content` 应面向 LLM 消费，而不是简单透传原始对象
- `raw_data` 可以保留完整结构化结果，供 `os` 或调用方使用
- `ExecutableTool.__call__(...)` 固定返回 `ToolResult`
- `ToolCallExecutionUnit` 的执行结果应由 `ToolCallExecutionResult` 承接，并在其中包裹对应 `ToolResult`

## 10. 上下文工程结构

### 10.1 ContextBuildProvider

`ContextBuildProvider` 是 `kernel` 的上下文构建标准接口，用于统一承接上下文工程实现。

| 方法 | 返回值 | 必须实现 | 说明 |
| --- | --- | --- | --- |
| `build(...)` | `ContextBuildResult` | 是 | 根据标准输入构建当前轮上下文结果。 |

约束：

- `ContextBuildProvider` 默认由 `os` 提供具体子类实现
- `kernel` 只依赖最小调用约定，不内置具体上下文构建策略

### 10.2 ContextBuildInput

`ContextBuildInput` 表达 os 层一次上下文构建的最小输入，不属于 kernel 核心模型。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `session_state` | `SessionState` | 是 | 否 | 当前会话状态根。 |
| `owner_agent` | `Agent` | 是 | 否 | 当前轮已解析出的 Owner Agent 运行时对象。 |
| `owner_agent_state` | `BaseAgentState` | 是 | 否 | 当前 Owner Agent 在本会话中的状态对象。 |
| `task` | `BaseProcessingTask | None` | 否 | 否 | 当前正在构建上下文的任务现场；后台治理类构建允许为空。 |
| `available_tools` | `list[ExecutableTool]` | 否 | 否 | 当前可供模型使用的工具对象。 |

约束：

- `owner_agent` 和 `owner_agent_state` 应由 `Runtime` 先完成解析再传入
- `available_tools` 持有运行时工具对象，具体 schema 转换由 os 层 ContextBuildProvider 完成

### 10.3 ContextBuildResult

`ContextBuildResult` 表达上下文工程构建后的最小输出。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `messages` | `list[LLMMessage]` | 是 | 否 | 构建后可直接传给 LLM 的多轮消息。 |
| `tool_schemas` | `list[dict[str, Any]]` | 否 | 否 | 由 `ToolMetadata` 转换出的模型工具 schema。 |
| `total_tokens` | `int` | 否 | 否 | 当前构建结果在本轮 tokenizer 规则下统计出的总 token 数。 |

约束：

- `tool_schemas` 只承载模型调用所需 schema，不承载工具运行时对象
- `total_tokens` 表达的是当前构建完成后、预算判断阶段使用的总 token 数
- 后续若进入压缩流程，应优先复用该统计结果，避免重复全量计算

### 10.4 LLMMessage

`LLMMessage` 表达 os 层传给 LLM 的单条消息。

| 字段 | 类型 | 必填 | 持久化 | 说明 |
| --- | --- | --- | --- | --- |
| `role` | `MessageRole` | 是 | 否 | LLM 消息角色，例如 `system`、`user`、`assistant`、`tool`。 |
| `content` | `str | None` | 否 | 否 | 消息内容。 |
| `tool_call_id` | `str | None` | 否 | 否 | tool message 对应的工具调用 id。 |
| `tool_calls` | `list[LLMCompletionToolCall]` | 否 | 否 | assistant message 中的完整工具调用结构。 |

约束：

- `LLMMessage` 属于 os 层模型协议对象，不进入 kernel 核心流程控制
- `reasoning_content` 不作为历史进入 LLM，也不进入上下文预算字符串
- `role = tool` 时应提供 `tool_call_id`
- `role = assistant` 且需要发起工具调用时应提供 `tool_calls`

## 11. 持久化边界

### 11.1 应持久化

第一阶段建议持久化以下内容：

- `SessionState.session_id`
- `SessionState.agent_frames`
- `SessionState.input_queue`
- `SessionState.agent_name2agent_state`
- `BaseAgentState.system`
- `BaseAgentState.history`
- `BaseAgentState.processing_tasks`
- `BaseProcessingTask.state`
- `ToolState.tool_call_message`
- `ToolState.execution_units`
- `ToolResult`
- `ToolMetadata`

这些内容共同保证会话现场、Owner Agent 推导、任务恢复和 tool loop 恢复。

### 11.2 不应直接持久化到 SessionState

第一阶段不建议直接持久化到 `SessionState` 的内容：

- `SessionState.agent_name2agent_state`
- `MemoryView`
- `ToolService`
- `ExecutableTool.__call__(...)`
- `ExecutableTool.before_execute(...)`
- `ContextBuildInput`
- `ContextBuildResult`
- 外部长期记忆本体
- 完整审计日志
- 前端展示流式片段
- tracing 细节
- 权限治理执行记录

这些内容应由运行时补全，或由 `os` 层独立存储通道承接。

## 12. 第一阶段实现建议

第一阶段可以先按下面顺序落代码模型：

1. 枚举：`EventType`、`EventSource`、`TaskStateKind`、`TaskStatus`、`ToolExecutionStrategy`、`ToolResultStatus`、`MessageRole`
2. 输入模型：`Input`、`ObservableEvent`
3. 会话模型：`SessionState`、`AgentFrame`
4. Agent 模型：`BaseAgentState`、`SystemDefinition`、`RuntimeArtifact`
5. 任务模型：`BaseProcessingTask`、`BaseTaskState` 及四类具体状态
6. 工具模型：`ToolService`、`ExecutableTool`、`ToolMetadata`、`ToolResult`、`ExecutionUnit`
7. os 上下文模型：`ContextBuildInput`、`ContextBuildResult`

实现时建议遵守：

- 先使用明确枚举，不在运行逻辑中散落裸字符串
- 所有枚举类统一继承 `str, Enum`，保证可比较、可序列化和可持久化
- `kind` 字段作为任务状态反序列化的唯一分派入口
- `owner_agent_name` 只通过 `agent_frames[-1].agent_name` 推导
- `agent_name2agent_state` 作为运行时字段，不进入 `SessionState` 序列化结果
- `MemoryView` 第一阶段直接跟随 `BaseAgentState` 一起持久化
- `ExecutableTool.__call__(...)`、`before_execute(...)` 和上下文构建结果属于运行时对象，不进入会话状态持久化

## 13. 待后续细化的问题

以下问题不阻塞第一阶段数据结构落地，但需要在后续 `Runtime` 状态流转文档中继续明确：

- `Input.events` 多事件时的排序和批处理策略
- `MessageState` 切换到 `ToolState` 的精确条件
- `ToolState` 中 LLM continuation 的消息保存格式
- `AutoResponseState` 的事件匹配规则
- `ExecutableTool.before_execute(...)` 的协议签名
- `complete_handoff` 控制工具的输入输出 schema

## 14. 当前版本结论

当前版本的数据结构设计重点是先立住 `kernel` 的最小可恢复现场：

- 用 `ObservableEvent` 承接已经进入 `kernel` 的外部变化
- 用 `SessionState` 承接会话客观现场
- 用 `AgentFrame` 推导当前 Owner Agent
- 用 `BaseAgentState` 承接 Agent 主观状态
- 用 `BaseProcessingTask` 承接未完成任务现场
- 用 `ToolMetadata` 和 `ToolResult` 统一工具描述与结果回流

这些结构稳定后，`Runtime` 才能在不引入复杂 `os` 能力的情况下跑通最小闭环，并为后续上下文工程、工具执行、handoff 和中断恢复提供清晰落点。
