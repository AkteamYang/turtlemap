# turtlemap Runtime 前置流程设计

## 1. 文档目标

本文聚焦 `Runtime` 正式进入主循环前必须明确的前置流程。

这份文档不重复展开 `Runtime` 内部状态流转，而是回答下面几个问题：

- `Tool` 如何注册、发现和装配
- `ToolService` 如何组织运行时可调用能力
- `ContextBuildProvider` 如何承接上下文构建实现
- 哪些工具是业务工具，哪些是 `Runtime` 注入的系统工具
- 新会话或缓存缺失时，默认 `SessionState` 如何初始化，以及 root Agent 如何接入
- 这些前置流程哪些属于 `kernel`，哪些应留在 `os`

本文目标是给后续 `Runtime` 原型实现提供启动前置约束。

## 2. 设计原则

### 2.0 id 命名约定

前置流程涉及的各类业务 id，在实现中统一采用 `*_id` 字段命名，字段值统一采用 `业务前缀 + ":" + UUID` 的格式。

例如：

- `session_id = "session:238ab4c8-..."`
- `task_id = "task:238ab4c8-..."`

本文后续若出现 `session_id`、`task_id` 等写法，均按上述统一规则理解；`Agent` 主标识则统一采用 `agent_name`。

### 2.1 前置流程服务于 Runtime 闭环

前置流程的目标不是抽象更多对象，而是保证 `Runtime` 启动时已经拿到：

- 可用的 `SessionState`
- 可加载的 `BaseAgentState`
- 可用的 `OSService`
- 明确的系统注入工具

也就是说，前置流程的职责是“把运行原料准备好”，而不是提前替 `Runtime` 做状态推进。

### 2.2 Tool 注册和 Tool 暴露分离

需要区分两件事：

- 工具已经注册到系统中
- 某个 Agent / 某个会话当前轮可以使用这个工具

前者是能力发现问题，后者是上下文暴露问题。

因此：

- `os` 负责工具管理与装配，`Runtime` 只接收统一工具视图
- `Runtime` 和 `ContextEngineering` 决定当前轮向模型暴露哪些工具

### 2.3 默认结构优先保可运行

新会话、缓存缺失、局部状态损坏时，前置流程应优先补出默认结构，保证 `Runtime` 能继续运行。

当前阶段遵守：

- 缺 `SessionState` 时创建默认 `SessionState`
- `root_agent` 由业务代码显式创建并传入 `Runtime`
- 缺外部工具视图时至少保证系统工具可用

### 2.4 Runtime 采用实例模型

当前阶段更推荐将 `Runtime` 设计为实例，而不是只提供类方法入口。

原因是一次运行过程中，`Runtime` 天然会持有一组需要反复访问的过程态对象，例如：

- `session_state`
- `agent_name -> agent object`
- `agent_name -> agent_state`
- `os_service`
- 当前运行配置和恢复策略

如果改成纯类方法，这些状态最终仍要被塞进一个临时上下文对象里四处传递，复杂度并不会更低。

因此更合理的边界是：

- `Runtime.__init__(...)` 负责接收稳定依赖，例如 `root_agent`
- os 层 `Runtime.run(...)` 负责接收本次运行输入事件和业务层已加载或创建的 `session_state`
- 运行过程中解析出的 Agent 对象、状态映射和工具视图保存在实例上，供后续 handoff、恢复和状态推进统一通过 `self` 访问

## 3. 前置流程总览

`Runtime` 进入主循环前，前置流程建议按如下顺序组织：

1. 创建 `Runtime` 实例，并显式传入 `root_agent`
2. 业务层加载或初始化默认 `SessionState`
3. 调用 os 层 `Runtime.run(...)`，传入外部输入与 `session_state`
4. 从 `root_agent` 出发解析当前可达的 Agent 集合
5. 基于当前 Agent 图补齐 `SessionState.agent_name2agent_state`
6. 初始化或接收 `os` 提供的 `OSService`
7. 由 `OSService` 聚合 LLM、状态存储、上下文构建、压缩和后台执行器等 os 能力
8. 注入当前轮所需系统工具
9. 将当前可用工具裁剪为会话 / Agent 可见视图
10. 再进入 `Runtime` 主循环

这里的关键判断是：

- `ToolService` 更适合作为 `Runtime` 侧的工具管理视图，而不是用户侧必须显式经历的注册过程
- 当前轮真正可见的工具集合，是在 `os` 提供的工具管理结果基础上裁剪出来的视图
- Agent 不需要额外的 Provider；`Runtime` 只要拿到 `root_agent`，就应能顺藤摸瓜解析出所有支持 handoff 的 Agent
- 运行时依赖更适合先由 `os` 组装成 `RuntimeDependencies`，再统一注入 `Runtime`

### 3.1 Runtime 推荐接口形态

当前阶段建议：

```python
runtime = Runtime(
    root_agent=root_agent,
    state_store=state_store,
)
runtime.run(
    input_events=input_events,
    session_state=session_state,
)
```

约束：

- `root_agent` 属于 `Runtime` 的稳定依赖，适合在实例创建时传入
- `state_store` 属于可选业务存储依赖，适合在实例创建时显式传入
- `session_state` 和 `input_events` 属于单次运行输入，适合在 os 层 `run(...)` 时传入
- `Runtime` 实例应保留解析后的 `agent_name2agent`、`agent_name2state` 和当前 `session_state`
- `Runtime` 不建议只暴露一个“类方法即跑完”的纯静态入口

### 3.3 RuntimeDependencies

当前阶段建议由 `os` 先完成依赖组装，再把依赖聚合成一个 `RuntimeDependencies` 传给 `Runtime`。

建议至少包含：

- `model_client_provider`
- `context_build_provider`
- `session_state_store`
- `agent_state_store`

如果第一阶段要支持 `background`，再额外包含：

- `executor`

这样可以避免：

- `Runtime.__init__(...)` 参数不断膨胀
- `kernel` 内部再引入一层全局注册中心

### 3.4 TMRuntime 外层封装

为了封装 `os` 层复杂度，第一阶段更推荐增加一层 `TMRuntime` 或等价包装对象，而不是默认通过继承大量重写 `kernel.Runtime`。

推荐做法：

- `os` 读取配置
- `os` 创建各类 provider / store / executor
- `os` 组装 `RuntimeDependencies`
- `os` 构造 `Runtime`

这样 `Runtime` 主循环可以保持最小、稳定、可复用。

### 3.5 Runtime 扩展边界

如果后续确实需要让 `os` 扩展 `Runtime`，建议优先采用：

1. 组合
2. 少量稳定钩子

不建议默认通过继承直接重写主循环主体。

### 3.2 Agent 创建约定

当前阶段建议 `Agent` 在业务代码中显式创建，并直接接收：

- `tools`
- `handoffs`

其中：

- `tools` 支持两种输入形态：`tool` 装饰的普通函数，或 `ExecutableTool` 对象
- `handoffs` 直接接收可切换到的下游 `Agent`

当前实现建议进一步收敛为：

- 业务侧默认优先创建 `Agent`
- `Agent` 继承自 `kernel.BaseAgent`
- `Agent` 内部默认返回 `ToolService`

这样做的好处是：

- Agent 的能力边界在创建时就已经确定
- `Runtime` 不需要额外的 AgentProvider 或注册中心
- `Runtime` 只要拿到 `root_agent`，就可以沿 `handoffs` 递归遍历出完整 agent graph
- 业务侧不需要手动注入 `ToolService`，默认即可接入 `os` 层工具管理与 `ExecutionUnit` 构造逻辑

约束：

- 同一个 `Agent` 的 `tools` 最终都应收敛为统一的 `ExecutableTool` 视图
- `handoffs` 形成的是显式 Agent 引用关系，而不是名称字符串配置
- `Runtime` 解析 Agent 集合时，应按 `agent_name` 去重，避免重复遍历和循环引用造成死循环
- `kernel.BaseAgent` 只定义最小扩展点；具体 `ToolService` 实现由 `os` 层子类接入

## 4. Tool 注册流程

### 4.1 注册目标

`Tool` 注册的目标是把一个可调用能力统一装配成 `ExecutableTool`，并纳入 `ToolService`。

最小装配结果包括：

- `ToolDescriptor`
- `input_model`
- `ExecutableTool.__call__(...)`

如果工具需要更复杂封装，还可以通过子类实例提供：

- `before_execute(...)`
- `validate_execute(...)`
- `after_execute(...)`

当前阶段关于输入结构和描述信息只保留一条路径：

- 所有工具都必须提供 `input_model`
- 无参工具应显式使用 `EmptyToolInputModel`
- 缺 `input_model` 直接报错
- `json_schema` 统一由 `input_model` 自动生成
- 导出的 `json_schema` 以 `input_model` 的字段为顶层参数，不额外包一层对象字段名
- 所有工具都必须提供结构化 `ToolDescriptor`
- 缺 `ToolDescriptor` 直接报错
- tool schema 中的 `description` 由 `ToolDescriptor.role`、`ToolDescriptor.objective` 等结构化信息按固定模板动态整合生成

### 4.2 Agent 接收的工具形态

对于 Agent 而言，当前阶段建议支持接收两种工具形态：

1. `tool` 装饰的普通函数
2. `ExecutableTool` 对象

对应地，需要区分两种装配形态：

1. 简单式注册
2. `ExecutableTool` 实例注册

约束：

- 简单式注册只用于“函数 + 基础 metadata”的声明式工具
- 简单式注册仍需显式提供 `ToolDescriptor`
- 简单式注册不提供 `before_execute(...)`、`validate_execute(...)`、`after_execute(...)` 等扩展入口
- 需要钩子或业务上下文封装时，改用 `ExecutableTool` 实例
- `tool` 装饰器只支持普通函数
- 若装饰对象不是普通函数，应在注册阶段直接校验失败并拒绝注册
- `ExecutableTool` 内部的方法不支持再用 `tool` 装饰器装饰
- 无论输入形态如何，最终都应收敛成一致的 `ExecutableTool` 对象

### 4.3 tool_id 规则

`ToolMetadata.id` 是 `ToolService` 中的唯一键。

当前阶段建议：

- `tool_id` 在同一个 `ToolService` 内必须唯一
- 重复注册相同 `tool_id` 时默认视为覆盖错误，而不是自动替换
- 系统工具应使用稳定、保留的 id，例如 `complete_handoff`

### 4.4 装配结果

工具装配完成后，`ToolService.tools` 至少满足：

- key 是 `ToolMetadata.id`
- value 是完整的 `ExecutableTool`

也就是说，`ToolService` 不只保存 schema，也不只保存普通函数，而是保存 `Runtime` 实际需要的完整运行时工具对象视图。

装配时建议分三层：

- `ToolDescriptor`：创建侧结构化描述对象
- `ToolMetadata`：运行时最小元信息结果
- `ExecutableTool`：最终可执行工具对象

其中：

- `ToolDescriptor` 负责结构化语义描述
- `ToolMetadata` 只保留运行时真正需要的最小结果
- `execution_strategy` 不放在 `ToolDescriptor` 中，而应放在 `ExecutableTool` 或装配配置侧

## 5. ToolService 设计

### 5.1 核心职责

`ToolService` 更适合作为 Agent 维度的工具管理视图。

其职责只包括：

- 保存当前可用工具对象
- 提供按 `tool_id` 查询能力
- 提供枚举当前可用工具元信息的能力
- 提供 `ExecutionUnit` 构造入口

它不负责：

- 工具源码扫描
- 装饰器装配
- 业务侧工具管理流程
- 权限控制
- 多租户隔离
- 会话级治理
- 动态风控

这些能力留给 `os`。

当前阶段建议：

- `ToolService` 默认由 `os` 传入具体实现
- `kernel` 只依赖其最小调用约定
- 不同 Agent 可以持有不同的 `ToolService` 实例，即使其中包含同功能工具
- 默认实现建议放在 `ToolService`

补充边界：

- 工具排序对模型选择有一定影响，但只应视为弱偏好信号
- 模型通常能理解“优先低成本工具”这类自然语言偏好，但不适合作为严格优先级调度器
- 如果需要硬约束的优先级控制，应由 `os` 在工具过滤、选择和排序阶段完成，而不是只依赖模型自行判断

### 5.2 运行时视图

需要区分两个层次：

1. Agent 当前持有的 `ToolService`
2. 当前轮可见工具视图

建议做法：

- `os` 先完成工具管理与装配
- `ToolService` 保存当前 Agent 侧可管理的工具对象
- `Runtime` 根据当前 Owner Agent、会话状态和系统策略，拿到一个“当前轮可见工具列表”
- `ContextEngineering` 只把这个可见列表转成 `tool_schemas`

这样可以避免：

- 为每个会话复制一份完整 registry
- 把“工具发现”和“工具暴露”混成一层逻辑

### 5.3 最小查询接口

当前阶段建议 `ToolService` 至少支持以下能力：

- `get(tool_id) -> ExecutableTool | None`
- `list_tool_metadata() -> list[ToolMetadata]`
- `has(tool_id) -> bool`
- `build_tool_execution_units(agent, task, message) -> list[ExecutionUnit]`
- `execute_unit(context, execution_unit) -> ExecutionUnitResult`

如果后续需要工具过滤、选择、排序或更复杂索引，应优先由 `os` 扩展，而不是提前塞进 `kernel`。

补充说明：

- `kernel.BaseToolService` 只定义执行单元构造与执行入口，不内置默认实现
- 基类默认抛出能力边界异常，避免把 `os` 层执行策略硬编码到 `kernel`
- `ToolService` 负责把 assistant message 中的 `tool_calls` 转换成 `ExecutionUnit` 列表
- `ToolCallExecutionUnit` 保存 `tool_meta_id` 与完整 `LLMCompletionToolCall`，具体参数由执行阶段从 `tool_call.function.arguments` 解析
- `Runtime` 只通过 `ToolService` 获取并执行执行单元，不再自己拼装或分发具体 `ExecutionUnit` 子类
- 已完成任务如何整理成最终 `History` 消息，应由 `ContextBuildProvider` 的统一入口承接，而不是作为 `ToolState` 专属能力绑定在 `ToolService`
- 工具注册、归一化和重复 id 校验失败时，应抛出工具配置类异常；按名称或 id 查找工具失败时，应抛出工具查找类异常

### 5.4 ExecutableTool 运行模型

`Runtime` 对工具的统一调用入口应是 `ExecutableTool.__call__(...)`。

这意味着：

- `Runtime` 不直接依赖 `call` 属性
- 简单式注册的函数最终也应被包装成可调用的 `ExecutableTool`
- 复杂业务工具可以直接通过继承 `ExecutableTool` 并重写 `__call__(...)` 来封装业务逻辑

建议边界：

- 轻量工具优先走简单式注册
- 复杂工具优先走 `ExecutableTool` 子类实例注册
- `before_execute(...)`、`validate_execute(...)`、`after_execute(...)` 等扩展行为只放在 `ExecutableTool` 实例模型中
- 参数结构只认 `input_model`，不做函数签名推导
- 参数描述只认 `input_model` 字段描述，不做 docstring 参数解析

### 5.5 上下文构建基础流程

`Runtime` 不直接拼装 LLM 上下文，而是通过 `OSService.context_build_provider` 统一获取。

当前阶段只约定最小流程：

1. `Runtime` 在进入某次模型调用前，先确定当前 `session_state`
2. `Runtime` 解析当前 `owner_agent` 和对应的 `owner_agent_state`
3. `Runtime` 基于当前轮可见工具列表生成 `available_tools`
4. `Runtime` 组装 `ContextBuildInput(session_state, owner_agent, owner_agent_state, task, available_tools)`
5. `Runtime` 调用 `OSService.context_build_provider.build_llm_context(...)`
6. `Runtime` 取得 `ContextBuildResult(messages, tool_schemas)`
7. `Runtime` 再将 `messages` 和 `tool_schemas` 传给模型

约束：

- `Runtime` 只负责准备标准输入和消费标准输出
- `Runtime` 应先完成当前 Owner Agent 的解析与状态绑定，再进入上下文构建
- `Runtime` 不负责在内部手写 system、history、task、input 的拼装细节
- `available_tools` 只包含当前轮实际对模型可见的工具元信息
- `tool_schemas` 只作为模型调用描述使用，不承载运行时工具对象
- 若当前轮没有可见工具，`available_tools` 和 `tool_schemas` 可以为空

## 6. 系统工具注入

### 6.1 为什么需要系统工具

有些工具不是业务能力，而是 `Runtime` 为控制流程必须注入的系统工具。

当前阶段最典型的是：

- `complete_handoff`

它的职责不是回答业务问题，而是向 `Runtime` 提交结构化完成信号。

### 6.2 注入时机

系统工具不建议全局永久暴露给所有 Agent。

建议：

- 在当前轮真正需要时，由 `Runtime` 注入
- 注入结果进入当前轮可见工具视图
- 不要求写回 `SessionState` 持久化为长期能力

例如：

- handoff 开始后，目标 Agent 当前轮获得 `complete_handoff`
- handoff 完成并回切后，默认不再继续暴露该工具

### 6.3 系统工具和业务工具的边界

系统工具与业务工具都以 `ExecutableTool` 形式进入当前轮，但语义不同：

- 业务工具：面向外部能力调用
- 系统工具：面向 `Runtime` 控制流

约束：

- 系统工具也必须走统一 `ExecutableTool` 结构
- `Runtime` 只根据结构化返回结果处理系统工具，不依赖自然语言猜测

## 7. 默认 SessionState / root Agent 接入

### 7.1 默认 SessionState

当外部未提供可恢复会话，或业务层判断会话不存在时，应由业务层初始化默认 `SessionState`。

最小默认结构建议包括：

- `session_id`
- `agent_frames = [root_frame]`
- `input_queue = []`
- `agent_name2agent_state = {}`

### 7.2 root Agent 接入

当前阶段不建议由 `Runtime` 在缺失时动态创建 root Agent。

更合理的边界是：

- root Agent 由业务代码显式创建
- `Runtime` 实例在初始化时显式接收 `root_agent`
- `Runtime` 再从 `root_agent` 出发解析所有可达 Agent

约束：

- `root_agent` 必须具备稳定 `agent_name`
- 若业务代码升级导致旧 SessionState 无法兼容，应由业务层升级 `schema_version` 或创建新的会话状态
- `Runtime` 负责处理“缓存缺失”与“状态修复”，不负责凭空生成业务 Agent 定义

### 7.3 初始化责任边界

当前阶段建议：

- 默认结构的“形状”由 `kernel` 定义
- 默认内容的“具体值”可由 `os` 提供

例如：

- `kernel` 规定必须有 root Agent
- `os` / 业务代码负责实际构造 root Agent 对象

这样可以兼顾：

- `kernel` 的可运行性
- `os` 的产品化灵活性

## 8. 前置流程与 Runtime 的关系

前置流程完成后，`Runtime` 才开始处理：

- 输入出队
- 任务匹配
- 任务执行
- 状态推进
- checkpoint 提交

这意味着：

- `Tool` 注册不属于 `Runtime` 主循环内部步骤
- 默认结构初始化也不属于任务状态推进
- 前置流程只负责准备“可运行起点”

## 9. kernel / os 边界

### 属于 kernel

- `ExecutableTool` / `ToolService` 的最小结构定义
- 系统工具也走统一 `ExecutableTool` 的约定
- 默认 `SessionState` / root Agent 所需最小字段约束
- `Runtime` 注入系统工具的调用约定

### 属于 os

- 工具注册源码扫描、装饰器装配细节
- 按 Agent 语义做工具过滤、选择和排序
- 复杂权限控制与工具治理
- 哪些工具在某个场景下可见的高级策略
- 默认 root Agent 的具体 system 内容
- 业务工具和外部执行器的真实接入实现

## 10. 第一阶段实现建议

第一阶段建议先做最小闭环：

1. 定义 `ToolService` 的最小协议
2. 支持简单式注册和 `ExecutableTool` 实例注册
3. 支持 `Runtime` 当前轮注入 `complete_handoff`
4. 支持新会话时自动创建默认 `SessionState`，并接入外部显式传入的 `root_agent`
5. 支持从 `ToolService` 选择当前轮可见工具列表

暂时不必一次性做全：

- 多租户工具隔离
- 动态权限与审批
- 热更新注册中心
- 分布式工具目录

## 11. 当前版本结论

当前版本的核心判断是：

- `Runtime` 依赖一组明确的前置流程，而不是假设所有状态和工具天然存在
- `ToolService` 负责 Agent 维度的工具管理，当前轮工具暴露由 `Runtime` / `ContextEngineering` 决定
- 系统工具与业务工具统一走 `ExecutableTool`，但语义边界必须清楚
- 简单式注册保持极简；一旦需要钩子、占位回复或业务上下文封装，就升级为 `ExecutableTool` 子类实例
- `ExecutableTool` 的统一执行入口是 `__call__(...)`；`tool` 装饰器第一阶段只支持普通函数
- 默认 `SessionState` 是 `Runtime` 保可运行的基础兜底；`root_agent` 由业务代码显式创建并传入
- 前置流程先立住，`Runtime` 原型实现才不会在工具和初始化问题上反复猜测
