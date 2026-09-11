# turtlemap kernel 设计

## 1. 文档目标

本文聚焦 `turtlemap` 中 `kernel` 这一层的设计。

本文的目标不是展开实现细节，而是回答下面几个问题：

- `kernel` 在整个 turtlemap 系统中的职责是什么
- `kernel` 为什么要以 `Runtime` 为核心
- `kernel` 内部最小需要哪些组成部分
- 这些组成部分之间如何形成稳定闭环
- 哪些能力应该明确留在 `os`，而不进入 `kernel`

## 2. kernel 定位

在 `turtlemap` 的整体系统架构中，`kernel` 是最小闭环运行内核。

它的角色不是完整系统，也不是应用层框架，而是提供一组足够稳定的运行原语，让上层 `os` 可以基于这些原语构建更复杂的 AgentOS 能力。

可以把 `kernel` 理解为：

- 面向 Agent 运行的最小执行内核
- 面向 LLM 能力释放的基础承载层
- 面向长期演进仍需保持稳定的底层能力层

`kernel` 的价值不在于功能多，而在于边界清晰、闭环稳定、可被上层长期复用。

从实现策略上看，`kernel` 更适合负责三类内容：

- 架构定义
- 核心数据结构
- 最小闭环所需的核心逻辑

只要这三类内容足够稳定，`kernel` 就可以独立跑通最小闭环；而复杂增强能力则不应该继续堆进 `kernel`，而应该通过 `os` 内部的能力接入机制交给外层 `os`。

## 3. kernel 设计原则

### 3.1 以 Runtime 为核心

`Runtime` 是 `kernel` 的组织核心。

原因是 turtlemap 不是单纯在设计一组静态抽象，而是在设计一个持续运行的 Agent 系统。  
系统的智能能否稳定体现出来，最终取决于：

- `kernel` 核心是否稳定
- 外层 `os` 服务能否保证 `kernel` 可靠获取足够信息

这些都属于 `Runtime` 的组织范围。

所以在 `kernel` 中：

- `Input`、`SessionState`、`Tool`、`ContextEngineering` 是能力组成
- `Runtime` 负责把这些能力组织成一个真正运行的闭环

### 3.2 保持最小原语，不承担上层系统责任

`kernel` 只承载最基础、最稳定、最可复用的运行原语。

它不应该过早吸收：

- 编排系统
- 多 Agent 协作策略
- 治理逻辑
- 产品化接口
- 复杂场景策略

### 3.3 复杂增强通过 os 能力接入协议承接

`kernel` 需要为扩展留口，复杂增强逻辑由外层 os 提供。

- `kernel` 负责定义数据结构与调用约定
- `kernel` 负责定义扩展点
- 复杂增强能力通过 `os` 内部的能力接入协议接入系统

这类适合通过 `os` 能力接入机制承接的能力包括但不限于：

- 高效上下文压缩算法
- `ContextEngineering` 细节
- 治理策略
- 复杂监控与观测
- 高级路由与策略编排

这样做的好处是：

- `kernel` 可以保持最小闭环可独立跑通
- 增强能力可以按需接入，而不是绑死在内核里
- 不同 `os` 形态可以接入不同能力组合
- 后续增强算法可以快速迭代，而不破坏 `kernel` 稳定性

### 3.4 以信息传达可靠性为目标

`kernel` 的核心目标之一，是保证信息能够被可靠地组织、传递和使用。

因此，`kernel` 不是围绕“如何包装一次模型调用”来设计，而是围绕下面这件事来设计：

- 如何通过工程化手段让输入、记忆、任务信息、工具结果在代码编写、运行过程中以足够稳定的方式传达到 LLM

## 4. kernel 职责边界

### 应由 kernel 承担的职责

- 定义核心架构边界
- 定义核心数据结构，如 `SessionState`、`BaseAgentState`
- 接收已经归一化后的输入
- 维护最小内部状态
- 管理上下文工程主流程，但是不处理细节
- 统一工具描述，通过结构化定义和输入 schema 生成，提供高质量的工具描述信息
- 构建推理上下文
- 驱动当前 Owner Agent 的执行循环
- 维护中断与恢复所需的基础能力

### 不应由 kernel 承担的职责

- 环境建模
- 事件采集系统
- 输入归一化策略
- 高级上下文压缩算法
- 上下文工程中的构建细节
- 多 Agent 编排
- 权限治理
- tracing
- 复杂路由策略
- 应用层工作流
- 产品形态接口

### 与 os 的关系

`os` 面向系统能力，`kernel` 面向运行原语。

两者之间的关系可以概括为：

- `os` 负责把外部世界整理成 `kernel` 可以消费的形式
- `kernel` 负责把这些信息稳定转化为运行闭环
- `os` 再基于 `kernel` 的稳定闭环继续扩展治理、编排和系统能力
- `kernel` 定义扩展约定，`os` 通过内部能力接入协议承接复杂增强逻辑

## 5. kernel 最小组成

当前建议 `kernel` 最少围绕五部分设计：

- `Input`
- `SessionState`
- `Tool`
- `ContextEngineering`
- `Runtime`

这五部分不是并列功能模块，而是围绕运行闭环形成的最小集合。

### 5.1 Input

`Input` 负责承接已经进入 `kernel` 的当前输入。

这里的输入虽然是外层系统归一化后的结果，但在进入 `kernel` 时，仍然应该被表达为统一的数据结构。

当前更合适的理解是：

- `Input` 本质上是当前轮次进入 `kernel` 的 `ObservableEvent` 集合
- `ObservableEvent` 是 `kernel` 统一消费外部输入的标准数据结构
- `kernel` 只定义这种结构以及如何处理它，不定义外层如何生成它

这些 `ObservableEvent` 可以来自用户消息、`tool` 执行结果、handoff 完成结果、环境变化，或其他已经被外层整理过的反馈；但这些来源如何采集、如何筛选、如何归一化，不属于 `kernel` 的职责范围。

从当前讨论看，`ObservableEvent` 至少应该具备一部分基础运行语义，例如：

- `event_type`：标识事件对 `Runtime` 的调度语义，而不是业务分类
- `source`：标识事件来自哪里，例如用户、`tool`、系统、环境感知模块等
- `priority`：标识事件处理优先级，供 `Runtime` 做最基本的处理顺序判断
- `event_id`：事件 id，用于标识一次独立进入 `kernel` 的可感知变化
- `source_id`：来源侧对象 id，根据 `source` 类型不同可对应 `tool_id`、`handoff_id`、`timer_id`、environment source id 等；它不表示任务归属 id

`event_type` 第一版先收敛为四类：

- `user_input`：用户主动输入，通常触发新任务创建、任务补充或任务恢复
- `tool_result`：`tool` 执行结果回流，用于恢复和推进已有 `ToolState`
- `handoff_result`：handoff 完成后的结果回流，用于回切 AgentFrame，并在需要时恢复原 Agent 的 `HandoffState`
- `environment_message`：环境变化进入 Agent 感知域，例如定时器、天气、文件变化、外部系统状态变化；具体环境类型不进入 `kernel` 枚举，由 `os` 层和 `payload` 承接

这里的原则是：`event_type` 只表达 `Runtime` 调度差异，不表达业务领域差异。子 Agent 如果作为当前 Agent 的外部能力被调用，应封装成 `tool`，结果走 `tool_result`；如果是 handoff，则表示会话控制权临时切换给目标 Agent，完成后通过 `handoff_result` 回切并恢复必要任务，但不需要增加 `agent_result` 这类事件类型。中断、取消、暂停等系统控制不通过 `ObservableEvent` 表达，而应走 `Runtime` 控制 API。

是否需要像 `requires_response`、`visibility` 这样的更强处理语义，当前阶段先不强行纳入最小集合；这类能力可以等 `History` 过滤、上下文构建和治理边界更清楚后再决定。

### 5.2 SessionState / BaseAgentState

`SessionState` 是 `Runtime` 状态管理的最小单元，表示一次会话的客观现场。

这里的会话不是简单聊天窗口，而是“一件事”的运行现场。先有某件事，才会有处理这件事所需的 Agent；Agent 依托于会话而存在，但 Agent 本身可以跨会话被引用。

当前建议先按以下结构收敛：

- `SessionState`：会话级状态根
  - `agent_name2agent_state`：`dict[agent_name, BaseAgentState]`，随 SessionState 保存的完整 AgentState 映射
  - `agent_frames`：`list[AgentFrame]`，保存 Agent 调用栈；`SessionState` 必须包含一个 root `AgentFrame`，当前 Owner Agent 由列表尾部 frame 推导，因此不再额外保存 `owner_agent_name`
  - `input_queue`：`list[Input]`，保存已经持久化但尚未被本轮接管的输入
  - `session_id`、`version`、`schema_version` 等持久化身份与版本字段由 os 或业务层 SessionState 子类承载，不进入 kernel 最小模型
- `AgentFrame`：Agent 调用栈帧
  - `agent_name`：当前 frame 对应的 Agent
  - `from_agent_name`：由哪个 Agent 切换而来；root frame 为 `None`
  - `handoff_id`：当前 handoff id；root frame 为 `None`
  - `handoff_objective`：当前 handoff 的任务目标；root frame 为 `None`
  - `completion_tool`：Runtime 注入的 handoff 完成控制工具，当前先固定为 `complete_handoff`；root frame 为 `None`
  - `return_task_id`：handoff 完成后是否需要回到调用方 Agent 的某个任务继续处理；`None` 表示只回切 Owner Agent，不自动恢复任务
- `BaseProcessingTask.start_input`：启动当前任务时消费的输入包，任务生命周期内固定不变
- `BaseAgentState`：某个 Agent 在当前会话中的主观状态
  - `agent_name`
  - `System`：结构化的系统定义，不只是直接字符串。当前包含 `role`、`objective`、`constraints`、`input_format`、`output_format`、`examples`
  - `Memory`：当前 Agent 在当前会话 / 任务下的记忆上下文视图。长期记忆承接稳定偏好与背景，中期记忆承接会话摘要、阶段总结与当前约束
  - `History`：保存后续构建 LLM 输入会用到的稳定 `RuntimeArtifact`
  - `processing_tasks: list[BaseProcessingTask]`：该 Agent 当前未完成的任务现场集合

也就是说，`SessionState` 像一件事的舞台，记录会话客观状态、参与 Agent、当前输入和 Agent 调用栈；`BaseAgentState` 像某个 Agent 对这件事的主观状态，记录它的系统定义、当前记忆上下文视图、历史和正在处理的任务。

`BaseProcessingTask` 当前只保留两个核心属性：

- `task_id`
- `start_input`
- `state: MessageState | ToolState | HandoffState | AutoResponseState`

`state` 应继承自统一的 `BaseTaskState`，基类提供：

- `kind`：判别字段，用于序列化、反序列化和恢复分派
- `status`：通用生命周期状态，当前至少包含 `running`（正在执行）、`waiting`（等待结果）、`completed`（完成）、`failed`（失败）

具体状态当前先收敛为：

- `MessageState`：承接一次 assistant 产物生成任务，不保存流式增量；当消息完成并确认结果为 `tool_calls` 时，再切换进入 `ToolState`
- `ToolState`：承接整个 tool loop 的已发生执行过程，当前至少包含 `tool_call_message`、`execution_units` 和状态字段。`tool_call_message` 保存进入 tool loop 时的起始 assistant 产物；执行阶段产生的细节结果通过 `ExecutionUnit.result` 持久化。`ExecutionUnit` 是更广义的执行计划单元，既可以表示 `tool` 调用，也可以表示 LLM 执行或用户确认等步骤
- `HandoffState`：承接原 Agent 等待 handoff 完成的任务现场。handoff 本身通过 `AgentFrame` 切换当前 Owner Agent；如果原 Agent 需要在 handoff 完成后继续处理某个任务，则该任务进入 `HandoffState`，等待 `handoff_result` 回流后恢复
- `AutoResponseState`：承接事件触发型自动响应任务，例如定时器、环境变化等未来事件触发后的自动处理

是否拥有多轮会话输入权，是区分 `tool` 和 `handoff` 的核心标准：`tool` 不改变当前会话的对话主体；`handoff` 会通过追加 `AgentFrame` 临时切换当前会话的 Owner Agent。`AgentFrame` 表达会话控制权切换，`HandoffState` 表达原 Agent 的等待现场。handoff 开始时，`Runtime` 需要向目标 Agent 注入 `complete_handoff` 控制工具；目标 Agent 未完成任务时可以正常进行自然语言多轮对话，完成任务时必须调用 `complete_handoff` 产生结构化完成信号。`Runtime` 只根据该控制工具触发 frame 回切，不依赖普通自然语言解析。

这里的设计约束可以收敛为：

- `SessionState` 是 Runtime 状态管理根，`BaseAgentState` 是会话内某个 Agent 的主观状态
- `agent_name2agent_state` 负责随 SessionState 保存完整 AgentState，加载后由 OSService 按当前 Agent 图补齐缺失状态
- 当前 Owner Agent 由 `agent_frames[-1].agent_name` 推导，不做双写
- `BaseProcessingTask` 属于 `BaseAgentState`，表达某个 Agent 自己正在处理的任务现场
- `AgentFrame` 属于 `SessionState`，表达会话控制权如何切换以及是否需要返回某个任务
- `History` 保存面向 LLM 输入构建的稳定 RuntimeArtifact，具体 payload 由 os 层解释

完整审计日志和 Web 展示所需的更全量记录，由外层 `os` 的独立通道承接。

### 5.3 Tool

`Tool` 是 `kernel` 对外部可调用能力的统一抽象。

它回答的问题是：

- 当前有哪些能力可以被调用
- 它们适合做什么
- 它们如何被调用
- 它们的输入输出如何表达

`Tool` 的重点不在某个具体工具，而在统一描述和统一接入。

这里需要明确 `tool` 和 `handoff` 的边界：是否拥有多轮会话输入权，是区分二者的核心标准。`tool` 可以异步、可恢复、可长时间执行，也可以在实现上封装一个 Agent，但它不接管用户后续多轮输入；`handoff` 则会让目标 Agent 在一段时间内接管用户后续输入，并以新的对话主体继续处理问题。

因此，子 Agent 如果只是作为当前 Agent 的外部能力被调用，应封装成 `tool`；如果需要接管后续多轮会话输入，则属于 `handoff`，由 `Runtime` / `os` 负责切换当前会话的对话主体，而不是放进当前 Agent 的 `BaseProcessingTask.state`。

- 我们希望用一套结构化的 `ToolMetadata` 来规范化 `tool` 的使用，从而提高接入效果的下限，而不是把工具描述质量完全交给开发者个人的提示词能力。
- 工具接入方式可以同时支持 `tool` 装饰的普通函数和 `ExecutableTool` 对象。
- `ToolMetadata`：
  - `Identity` 身份层：`name`、`version`、`id`、`namespace`
  - `Semantic` 语义层：`description`、`use_cases`、`anti_use_cases`、`examples`、`tags`
  - 执行层：`execution_strategy`。`Runtime` 统一通过 `ExecutableTool.__call__(...)` 调用工具；`execution_strategy` 表达调用后的等待策略，当前先收敛为 `sync` 和 `async`
  - `Schema` 结构层：`input_schema`
  - 执行前判断入口：`before_execute`。`kernel` 只调用这个入口，不解释 `os` 子类扩展字段；治理逻辑由外层 `os` 通过 `ExecutableTool` 子类或扩展结构承接
- `tool` 返回结果在 `kernel` 层统一收敛为 `ToolResult`，当前至少包含：`status`、`content`、`raw_data`、`error`、`metadata`。其中 `content` 默认就是后续传给 LLM 的 `tool` message 内容，`raw_data` 作为程序侧透传数据，不要求 `kernel` 统一解释。
- 当 `execution_strategy = async` 时，Runtime 通过中断 request 表达等待状态，不要求工具额外生成占位描述。
- 复杂治理能力不进入 `kernel` 基类字段；`timeout`、`risk_type`、`Access`、重试、熔断、审计等策略字段，应由外层 `os` 通过 `ToolMetadata` 子类和 `before_execute` 扩展承接。

### 5.4 ContextEngineering

`ContextEngineering` 负责基于 `SessionState` 和当前 Owner Agent 的 `BaseAgentState`，构建 LLM 当前可消费的上下文。

它回答的问题是：

- 基于 `SessionState` 和当前 Owner Agent 的 `BaseAgentState` 构建 LLM 需要的多轮消息，组织 `System`、`Knowledge`、`History`、`Task` 等上下文层
- 将 `ToolMetadata` 转化为模型可消费的 `tool schema`
- 提供默认可运行实现，同时允许把更复杂的预算控制、压缩算法和状态更新策略代理到 `os` 层

### 5.5 Runtime

`Runtime` 负责把前面四部分组织成真实执行闭环。

它回答的问题是：

- 当前这一轮是否开始执行
- 当前执行处于什么阶段
- 下一步该继续、等待、重试还是结束
- 当前状态如何延续到下一轮

因此，`Runtime` 不是五者之一的普通模块，而是五者的组织轴。运行过程可以先抽象为：

- 业务层加载或创建 `SessionState` 后传入 os `Runtime.run(...)`，`OSService` 根据当前 Agent 图补齐 `agent_name2agent_state`
- 异常检查：从 `SessionState.agent_frames` 索引 0 开始向后检查，如果索引 `i` 对应 `agent_frame.agent_name` 在 `agent_name2agent_state` 中不存在，则将 `i` 以及后续 frame 弹出；遍历 Owner Agent 对应的 `processing_tasks`，保证最多只有一个执行中的任务，多个需要抛出异常
- 先对 `SessionState` 执行**中断恢复策略**
- `Runtime` 维护一个接收 Input 对象的输入队列，Input 对象会先被加入队列
- `Runtime` 从 Input 队列中取出一个 Input 对象，创建 `BaseProcessingTask` 并写入 `start_input`
- `Runtime` 读取当前 `BaseProcessingTask.start_input` 中待处理的 `ObservableEvent`
- `Runtime` 先根据 `agent_frames[-1].agent_name` 找到当前 Owner Agent，再根据事件中的关联信息或**事件特征匹配逻辑**，决定继续执行已有 `BaseProcessingTask`，还是创建新的 `BaseProcessingTask`
- 如果恢复已有任务，则基于 `BaseProcessingTask.state` 继续推进
- 如果创建新任务，则初始化新的 `BaseProcessingTask` 并进入执行流程
- 当仍有待处理输入或新结果进入时，继续下一轮循环；当没有待处理内容时，本轮运行结束
- 任务处理完成，例如 `MessageState` 输出完成、`ToolState` 执行完成，或目标 Agent 调用 `complete_handoff` 时，需要执行完成操作：
  - 将任务起点输入和输出结果整理为稳定 `RuntimeArtifact` 后更新到 Agent 的 `History` 中
  - 如果目标 Agent 调用 `complete_handoff`，则 `Runtime` 根据 `handoff_id` 生成 `handoff_result`，并先回切 / 弹出当前 `AgentFrame`；如果当前帧存在 `return_task_id`，则将 `handoff_result` 放入 Input 队列，在下一次循环中自动恢复原 Agent 的对应 `HandoffState`

这里先不把它设计成复杂事件系统，而是把重点放在“事件进入、任务归属、任务恢复和最小闭环稳定运行”上。

#### 中断恢复策略

中断恢复要优先保证会话结构和任务现场不丢失，而不是强行把所有中断都包装成一次正常完成。

判断是否需要恢复，应基于当前 Owner Agent（`agent_frames[-1]` 对应的 Agent） 的 `processing_tasks` 中是否存在仍处于执行中的任务。恢复时，`Runtime` 找到当前 Owner Agent，并遍历其 `processing_tasks`；当前阶段虽然支持多任务现场并存，但一次调度先只恢复一个执行中任务，类似人类在多个任务之间切换，但同一时刻只专注处理一个现场。

不同状态的恢复策略如下：

- `MessageState`：恢复时基于最近 checkpoint 中保存的任务现场继续执行。现有 LLM 接口通常不能可靠地从 token 级断点继续生成，因此不把已生成增量视为可恢复语义状态；未完成 message 不进入 `History`，如果客户端已经看到部分流式文本，可由外层 `os` 的审计或展示通道保留。
- `ToolState`：表示 tool loop 尚未闭合。恢复时应基于已保存的 `tool_call_message`、`execution_units` 及其 `result` 继续推进；已完成的 unit 不重复执行，处于执行中或等待状态的 unit 由具体执行语义决定继续、查询结果、等待事件或重试。
- `HandoffState`：表示原 Agent 正在等待 handoff 完成。恢复时不主动生成回复，而是等待 `handoff_result`；handoff 是否完成由目标 Agent 调用 `complete_handoff` 决定，而不是由自然语言回复或某个 `BaseProcessingTask` 状态推断。
- `AutoResponseState`：表示任务正在等待未来事件触发。恢复时通常不主动执行，而是继续等待匹配的 `ObservableEvent`；如果恢复时发现触发事件已经到达，则按事件恢复对应任务。

恢复完成后，`Runtime` 再从 `input_queue` 接管新的 `Input` 并创建任务。这样可以避免新输入覆盖旧现场，也能支持用户在异步任务未完成时继续发起新输入。

#### 事件特征匹配逻辑

接收到新的 `Input` 时应该如何匹配到对应的 `BaseProcessingTask`，我们根据不同情况展开讨论：
- 如果 `processing_tasks` 为空，则新创建一个 `MessageState` 类型的 `BaseProcessingTask`，用于接收 LLM 的 assistant message 输出
- 如果不为空，`processing_tasks` 的状态应该都是“等待结果”，因为我们前置进行了“中断恢复策略”。遍历 `Input.events`：对于 `tool_result`，优先匹配 `AutoResponseState` 并补齐对应异步 unit 的结果；对于 `HandoffState`，根据 `handoff_result` 恢复 `return_task_id` 对应任务；其他 `AutoResponseState` 类型的匹配规则可继续按 `trigger_event_type` 细化

## 6. kernel 运行闭环

从运行视角看，`kernel` 的核心闭环可以概括为：

```text
Input
  -> SessionState
  -> Owner BaseAgentState
  -> ContextEngineering
  -> LLM Reasoning
  -> Action
  -> State Update
  -> Next Loop
```

如果转成更贴近运行阶段的表达，可以理解为：

1. 接收输入
2. 更新状态
3. 构建上下文
4. 触发推理
5. 执行动作
6. 回写状态
7. 决定是否进入下一轮

这条闭环里最重要的不是“每一步做得多复杂”，而是：

- 每一步语义清楚
- 每一步输入输出边界清楚
- 每一步都能被恢复
- 整个闭环可以长期稳定运行

## 7. `Runtime` 在 `kernel` 中的核心地位

既然 `turtlemap` 的系统设计哲学是“以 `Runtime` 为核心”，那 `kernel` 设计里就需要明确 `Runtime` 的地位。

`Runtime` 在 `kernel` 中至少承担三类责任：

### 7.1 组织责任

把输入、状态、上下文、工具、输出串成一条可运行链路。

### 7.2 控制责任

决定当前执行处于什么控制状态，例如：

- 继续循环
- 等待输入
- 调用工具
- 输出结果
- 结束执行
- 失败重试

### 7.3 恢复责任

保证运行过程可以暂停、恢复、延续，而不是每轮都重新开始。

从这个角度看：

- 没有 `Runtime`，其他组成部分只是一组静态能力
- 有了 `Runtime`，这些能力才真正形成 `kernel`

## 8. SessionState / Knowledge 在 kernel 中的角色

在 `turtlemap` 中，`Knowledge` 不是附属功能，而是影响系统智能水平的核心要素之一。

但在 `kernel` 阶段，`Knowledge` 不应被简单理解为动态检索结果集合。更准确地说，`BaseAgentState.Knowledge` 是当前 Agent 面向当前会话 / 当前任务的记忆上下文视图。

当前建议是：

- 长期记忆和中期记忆都可以采用结构化纯文本载体
- 中期记忆承接会话摘要、阶段总结和当前约束，支持 LLM 滚动重写
- 长期记忆承接稳定偏好、身份信息和项目背景
- 若后续需要“当前回答相关的动态知识”，应单独作为 `retrieved_context` 或等价概念处理，而不是继续混入 `Knowledge`

也就是说，`kernel` 当前更关心的是：

- `SessionState` 如何作为 Runtime 状态管理根，并通过 `agent_frames` 管理 Owner Agent 切换
- `Knowledge` 如何作为运行时上下文视图参与 LLM 输入构建
- `History` 如何保留最近用户可见交互记录，并在进入 LLM 前再过滤

而不是：

- 完整长期记忆如何存储
- 动态检索上下文如何召回与排序
- 记忆压缩、冲突处理和过期策略如何实现

这些更具体的增强路径，应由外层 `os` 或专门的 memory service 承接。

另外，当前讨论已经进一步明确：

- `mid_term_memory` 作为会话工作记忆，直接随 `BaseAgentState` 快照推进和恢复
- `long_term_memory` 虽然会写入 `BaseAgentState` 快照用于回放和 observation 溯源，但每次新的 Runtime 运行仍应从 `StateStoreProtocol` 重新加载当前生效值

## 9. kernel 的非目标

为了保持 `kernel` 稳定，当前需要明确一些非目标。

当前 `kernel` 不追求：

- 一次性覆盖所有 Agent 场景
- 直接内建复杂编排引擎
- 直接内建完整治理系统
- 直接内建 tracing 体系
- 直接内建高级上下文增强算法
- 直接内建产品层接口体系
- 直接承担所有环境建模工作

这些能力很重要，但不应该在 `kernel` 层一起解决。

## 10. 当前阶段重点

当前阶段做 `kernel` 设计，更重要的是先确认下面几件事：

- `kernel` 的最小组成到底是什么
- `Runtime` 如何组织这套最小闭环
- `SessionState` / `BaseAgentState` 在闭环中的位置是什么
- `kernel` 与 `os` 的边界如何长期保持稳定

只要这几件事先立住，后续无论是工具增强、记忆增强，还是环境感知增强，都有稳定落点。

## 11. 当前版本结论

当前版本可以先把 `turtlemap` 的 `kernel` 理解为：

- 一个以 `Runtime` 为组织核心的最小闭环运行内核
- 一个负责定义架构、核心数据结构和最小闭环核心逻辑的底层能力层
- 一个面向输入、状态、上下文、工具和执行循环的基础运行层
- 一个服务于 LLM 能力释放，而不是替代 LLM 的底层承载层
- 一个需要长期保持稳定、可恢复、可扩展的系统基础层

在这个定位下，`kernel` 先解决“最小闭环如何可靠运行”的问题；更上层的治理、编排、环境感知、tracing、复杂上下文增强与产品化能力，则通过 `os` 内部能力接入协议放到 `os` 中继续展开。
