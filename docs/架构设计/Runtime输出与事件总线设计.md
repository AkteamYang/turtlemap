# Runtime 输出与事件总线设计

## 1. 文档目标

本文用于整理 os 层 `Runtime.run(...)` 的输出边界，以及事件总线和流式事件发送机制的设计问题。

当前 os 层 `Runtime.run(input_events, session_state)` 接收业务层已加载或创建的 `BaseSessionState`，返回 `RuntimeRunResult`，用于表达本轮执行产生的稳定输出列表；状态对象由业务层负责加载，SDK 只在运行过程中通过 `OSService` 提交 checkpoint。

本文记录当前已收敛的模型字段、职责边界和后续需要继续扩展的设计点。

## 2. 当前问题

输出边界需要持续约束以下问题：

- 一次运行可能产生多个稳定输出，例如 assistant message、tool result、handoff result、后台任务占位回复等。
- 执行过程中可能存在流式输出，最终返回值只表达稳定结果，过程数据通过事件总线发送。
- 系统需要有标准事件出口，而不是把所有过程信息混入 `SessionState`。

这里需要区分两类东西：

- 状态：用于恢复、checkpoint、继续执行，由 `SessionState`、`BaseAgentState`、`BaseProcessingTask` 承接。
- 输出：用于调用方消费、产品展示、实时推送和外部集成，应由独立结果对象或事件总线事件承接。

## 3. 初步方向

当前 os 层运行入口返回运行结果对象：

```text
RuntimeRunResult
    session_id
    outputs: list[TaskOutput]
```

其中 `TaskOutput.outputs` 直接承载本轮已经稳定形成、可以被外部消费的 `FINAL RuntimeEvent`。

暂定原则：

- `FINAL RuntimeEvent` 不替代 `History`，也不替代 `BaseProcessingTask`。
- `FINAL RuntimeEvent` 面向调用方消费，不承担中断恢复职责。
- `SessionState` 仍然是恢复入口，但不作为 os 层运行入口的主要业务返回值。
- 若调用方需要最新状态，应通过专门入口读取，或由 `RuntimeRunResult` 携带必要的轻量索引信息。

## 4. 事件总线占位

事件总线用于在 Runtime 执行过程中实时发送过程事件和稳定结果事件。

事件总线承接的信息包括：

- LLM 流式文本片段
- assistant message 完成事件
- tool call 稳定事件
- handoff 开始、完成事件
- checkpoint 完成事件
- 上下文同步压缩开始、结束事件
- 后台任务占位输出和后续完成通知

本文统一使用 `event bus` / “事件总线” / “事件” 作为标准术语。

## 5. kernel 与 os 边界

事件发送不放在 kernel 层。

kernel 只负责维持 Runtime 最小状态流转和必要流程转发，不直接感知事件总线，也不直接发布事件。事件总线属于 os 层系统能力，由 os 层在流程合适位置发送事件。

这样划分的原因是：

- kernel 应保持最小闭环，避免被产品展示、实时推送和订阅治理污染。
- 事件发送属于系统集成能力，天然需要连接前端、WebSocket、日志、回调和外部系统。
- os 层更适合根据不同产品形态决定哪些事件需要发送、如何发送以及谁能订阅。

实现结构如下：

```text
kernel BaseRuntime 推进状态
        ↓
os Runtime / Provider / ToolService 在合适流程点发布事件
        ↓
event bus 负责事件分发与回调通知
        ↓
ResultCollector 作为监听者接收事件并聚合 RuntimeRunResult.outputs
```

## 6. event_bus_id 设计

事件总线通过 `event_bus_id` 区分一次运行对应的事件通道。

`EventBus` 作为 os 层运行时基础设施使用，不需要业务侧实例化。调用方通过 `EventBus` 类方法创建事件通道、注册监听者和发送事件；`event_bus_id` 仅在订阅和 `publish(event_bus_id, event)` 的投递边界上传递，不进入事件载荷。

虽然事件通道语义上接近会话级别，但系统内部存在并发运行，仅依赖 `session_id` 不可靠。同一个 session 可能同时存在多次运行、重试或后台恢复流程，因此需要为每次运行显式创建一个临时 `event_bus_id`。

`event_bus_id` 应显式传递，不依赖线程或协程上下文隐式读取。

- `event_bus_id` 由 os 层在 run 前显式创建。
- 创建 event bus 后返回 `event_bus_id` 备用。
- os Runtime 创建或调用时显式携带 `event_bus_id`。
- os Runtime 初始化时保存 `event_bus_id`；若 root agent 已传入 `event_bus_id`，则复用外部通道。
- os Runtime 只关闭自己创建的事件通道；外部传入的 `event_bus_id` 由创建方负责生命周期。
- provider、tool service、executor 等 os 层组件初始化或调用时显式持有 `event_bus_id`。
- 事件发送接口接收明确的 `event_bus_id`，不依赖线程或协程上下文读取。

线程和协程上下文只可作为 os 层内部实现细节，不作为跨层协议和外部集成约定。

## 7. 发送与接收

事件总线本质上是运行时通知结构，服务于事件发送和事件接收，不承担状态恢复职责。

### 7.1 发送

发送侧通过 `event_bus_id` 指定事件通道。

事件发送约束：

- `event_id` 使用 `generate_prefixed_id(...)` 生成。
- 同一段迭代输出或同一次工具调用生命周期内，发送流程局部持有同一个 `event_id`。
- 过程事件和完成事件复用同一个 `event_id`。
- `sequence` 由 event bus 在发送时从内部全局自增计数器分配，发送方不自行分配。

os 层在以下位置发送事件：

- LLM 流式片段产生时
- assistant message 完成时
- tool call 开始、完成或失败时
- handoff 开始或完成时
- checkpoint 完成时
- 后台任务占位输出或完成通知产生时

事件发送接口应尽量保持轻量，不要求发送方知道订阅者是谁。

### 7.2 接收

接收侧通过 `event_bus_id` 注册回调函数。

订阅接口支持传入事件类型集合。若未传入事件类型集合，则监听该通道下全部事件；若传入集合，则只有 `event.event_type` 命中的事件才会触达该 listener。

event bus 按事件顺序执行通知。单个事件可以并发通知多个 listener，但必须等待该事件所有 listener 通知结束后，才继续处理下一个事件。

回调异常进行隔离，不影响 event bus 继续运行，也不影响其他 listener 接收事件。

`run` 结束前，本次运行产生的事件应完成通知。也就是说，`RuntimeRunResult` 返回时，当前运行已经发布的事件不应仍然挂在未通知状态。

由于事件总线本身按事件顺序处理，且 run 主流程需要等待单个事件的 listener 通知完成后才进入下一个事件，因此不需要额外设计 `wait_until_idle` 接口。

## 8. RuntimeRunResult 与事件收集

事件总线会发送大量过程事件，但并非所有事件都应进入 `RuntimeRunResult.outputs`。

结果收集遵循以下规则：

- 所有可观察数据都可以作为事件发送。
- 特定稳定事件会被收集为本轮运行输出。
- 过程输出事件不作为最终 outputs。
- 稳定事件才作为最终结果来源。
- assistant message 完成、后台任务占位回复、handoff 完成回执等稳定事件，可以进入 outputs。

这里需要注意：事件总线只负责分发，不应承担“哪些事件属于最终结果”的业务判断，也不内置 ResultCollector。ResultCollector 是事件总线的普通监听者，由 os 层 run 流程注册和读取。

## 9. 基础模型设计

### 9.1 RuntimeRunResult

`RuntimeRunResult` 是 `run(...)` 的输出结果，面向调用方表达本次运行产生的稳定输出。

基础结构如下：

```text
RuntimeRunResult
    session_id: str
    event_bus_id: str
    outputs: list[TaskOutput]

TaskOutput
    input: Input
    outputs: list[RuntimeEvent]
```

约束：

- `RuntimeRunResult` 不承载完整 `SessionState`。
- `RuntimeRunResult.outputs` 只包含本次运行对调用方可消费的稳定输出。
- `RuntimeRunResult.outputs` 按 `TaskOutput` 组织，每个元素包含当前任务开始输入和同一任务产生的稳定输出序列。
- `TaskOutput.input` 来自同一 `task_id` 对应的 `InputEvent.input`，用于让业务层直接拿到“本次输入 -> 本次输出”的消费单元。
- `TaskOutput` 顺序由每个 task 首次输出的 `sequence` 决定，组内最终事件也按其起始事件的 `sequence` 排序。

### 9.2 RuntimeEvent

`RuntimeEvent` 是 event bus 传送事件的基础模型。

`RuntimeEvent` 同样使用 `PolymorphicStateModel` 的基类 + 子类形式表达类型。`type_name` 是序列化和多态恢复使用的稳定模型判别标识，`event_type` 作为运行期属性将不同阶段的事件模型归一化为业务事件类型。

基础结构如下：

```text
RuntimeEvent(PolymorphicStateModel)
    type_name: str
    event_id: str
    parent_event_id: str
    session_id: str
    agent_name: str | None
    task_id: str | None
    sequence: int
    event_phase: RuntimeEventPhase
```

约束：

- `event_id` 表示同一逻辑事件链的稳定标识。
- `parent_event_id` 表示当前事件链对应输出所属的父级事件；为空或 `event_id-root` 表示顶层事件。
- `event_bus_id` 属于 event bus 投递边界，不属于 `RuntimeEvent` 的业务事实或持久化内容。
- `event_id` 使用 `generate_prefixed_id(...)` 生成，并由发送流程局部持有和复用。
- 过程事件使用相同 `event_id`，通过不同 `sequence` 表达事件推进顺序。
- `sequence` 由 event bus 在发送时从内部全局自增计数器分配，用于接收侧排序和结果收集。
- `event_phase` 表示当前事件生命周期阶段，首批取值为开始、过程、完成。
- `event_type` 不作为持久字段单独保存，由 `type_name` 归一化得到，且不包含事件阶段信息。
- `event_phase` 是表达事件阶段的唯一字段，`RuntimeEventType` 不编码开始、过程、完成等阶段语义。
- 完整内容和流式数据通过不同事件子类表达，不在基类中用宽字段混放。
- `source` 暂不进入基础字段，后续有明确过滤或治理需求时再补充。
- 同一 `event_id` 的事件序列中，`parent_event_id` 必须保持一致。

首批事件状态包括：

```text
started
in_progress
end
final
```

事件序列只允许以下两种合法形态：

```text
FINAL
```

```text
STARTED -> IN_PROGRESS* -> END
```

约束：

- `FINAL` 表示一次性完整事件，不能与 `STARTED`、`IN_PROGRESS`、`END` 混用。
- 流式事件必须以 `STARTED` 开始，以 `END` 结束。
- 流式事件中间只能出现 `IN_PROGRESS`。
- `STARTED`、`IN_PROGRESS`、`END` 都属于同一个 `event_id`。
- 流式事件中的 `STARTED` 和 `END` 必须携带该事件阶段要求的数据。

首批事件子类包括：

```text
MessageEvent
    chunk: LLMCompletionChunk | None
    completion: LLMCompletion | None

ToolCallEvent
    tool_call: LLMCompletionToolCall

ToolResultEvent
    tool_call_id: str
    result: ToolResult | None
    state_events: list[RuntimeEvent]

ContextCompressionEvent
    compression_mode: ContextCompressionMode
    compression_result: ContextCompressionResult | None
```

`ToolCallEvent` 默认使用 `FINAL` 形态。`ToolResultEvent` 默认使用流式形态，即使工具本身没有中间输出，也应发送 `STARTED -> END`，其中 `END` 携带最终 `result`。工具失败时，失败信息应收敛到 `ToolResultEvent.END.result` 中，由 `ToolResult.content` 提供可读文本。

`ContextCompressionEvent` 使用 `STARTED -> END` 形态。同步压缩事件会进入 `ResultCollector`，用于让调用方感知本轮 run 内发生过硬限制兜底压缩；异步后台压缩事件只作为观测通知发送，不进入 `RuntimeRunResult.outputs`。

最终稳定输出不再额外定义 `RuntimeOutput` 模型。每个事件子类负责把同一 `event_id` 的事件链收敛为自身的 `FINAL RuntimeEvent`：流式 message 合并为带 `completion` 的 `MessageEvent`，工具结果收敛为带 `result` 的 `ToolResultEvent`，同步压缩收敛为带 `compression_result` 的 `ContextCompressionEvent`。工具执行期间的子事件通过 `ToolResultEvent.state_events` 保存为最终事件树。

### 9.3 parent_event_id 与事件树

`parent_event_id` 由事件创建方显式赋值，EventBus 不再维护父事件栈，也不在
`publish` 时自动推导父级关系。

约束：

- publish 时，EventBus 只负责分配 `sequence`、写入时间戳并通知 listener，不修改 `parent_event_id`。
- 顶层事件不再依赖固定根 id 判断；只要 `parent_event_id` 不指向本次已收集的输出事件，就视为顶层。
- 子事件必须在创建时显式携带父级事件 id，例如工具内部派生出的子事件应把 `parent_event_id` 设置为对应 `ToolResultEvent.event_id`。
- `ToolResultEvent.END` 使用与 `STARTED` 相同的 `parent_event_id`。
- 并发执行场景不能依赖进程内父事件上下文推断父级关系，所有跨协程事件关系都应通过显式字段传递。

`parent_event_id` 只描述事件树关系，不替代 `task_id`、`tool_call_id`、`session_id` 等业务关联字段。

### 9.4 ResultCollector

`ResultCollector` 负责以事件监听者身份接收 `RuntimeEvent`，并将完整事件链收敛为对应类型的 `FINAL RuntimeEvent`。

`ResultCollector` 与 event bus 解耦。event bus 只负责按 `event_bus_id` 分发事件，不感知 ResultCollector 的存在；ResultCollector 只通过事件监听接口接收事件，不直接参与事件发布。

职责边界：

- 只订阅指定 `event_bus_id`；收到的事件天然属于该通道，无需在事件载荷中重复校验。
- `collect(...)` 只维护扁平事件结构，不在收集阶段构建最终事件树。
- 过程事件不单独作为输出，只作为流式事件链的合并输入。
- 每个事件子类通过 `create_final_event_from_events(...)` 构造自身最终事件。
- 顶层最终事件按其起始事件的 `sequence` 排序。

内部索引结构：

```text
event_id2events: dict[str, list[RuntimeEvent]]
parent_event_id2event_ids: dict[str, list[str]]
```

约束：

- `event_id2events` 保存同一 `event_id` 下的完整事件序列。
- `parent_event_id2event_ids` 保存父事件到子事件 id 的映射。
- 构建 `TaskOutput` 时临时从 `event_id2events` 派生 `task_id2input_event`，不作为收集阶段的常驻状态。
- 子事件按其起始 `sequence` 排序后进入父 `ToolResultEvent.state_events`。
- 顶层最终事件是 `parent_event_id` 未指向已收集事件链的事件。
- `RuntimeRunResult.outputs` 默认返回按 `task_id` 组织的 `TaskOutput` 列表，树内子事件通过 `ToolResultEvent.state_events` 表达。
- `InputEvent` 只作为 `TaskOutput.input` 的来源锚点，不作为普通最终输出聚合。
- `InputEvent` 是单条 `FINAL` 事件，同一个 `event_id` 下只会有一个输入事件，collector 构建输入索引时直接取事件列表第一个元素。
- 若某个输出分组找不到同 `task_id` 的 `InputEvent`，说明事件链不完整，应直接抛出异常，而不是返回缺失输入的任务输出。

首批收集类型包括：

- 普通 message
- tool call
- tool result
- context compression
- input

其他输出类型后续按子类扩展。

收集规则：

- 一次性完整事件必须是单条 `FINAL`。
- 流式事件必须是 `STARTED -> IN_PROGRESS* -> END`。
- collector 监听 `INPUT`、`MESSAGE`、`TOOL_CALL`、`TOOL_RESULT`、`CONTEXT_COMPRESSION`；`INPUT` 只提供任务输入，其余类型可构建为最终事件。
- 已收到 `FINAL` 的事件链直接复用该事件；流式事件链在收到 `END` 后才构建最终事件。
- 最终事件保留起始事件的 `event_id`、`parent_event_id` 和 `sequence`，以保持树关系和输出排序稳定。
- 顶层最终事件先按 `task_id` 分组，再结合对应 `InputEvent.input` 组装成 `TaskOutput`。
- 构建 `ToolResultEvent` 时，先构建自身最终结果，再根据 `parent_event_id2event_ids` 递归构建 `state_events`。
- `ToolResultEvent.state_events` 中的子最终事件按各自 `sequence` 排序。
- 只有 `ToolResultEvent` 承载 `state_events`；其他事件不承载子事件。

os 层 run 的使用流程如下：

```text
event_bus_id = EventBus.create_event_bus()
create ResultCollector(event_bus_id)
EventBus.subscribe(event_bus_id, ResultCollector.collect, event_types=ResultCollector.listen_event_types())
run main Runtime flow
read outputs from ResultCollector
return RuntimeRunResult(outputs=outputs)
```

## 10. 待确认问题

后续需要重点确认：

1. 输出结果和 `History` 写入之间的提交顺序如何定义。
2. checkpoint 失败时，已经发送的流式事件如何解释和补偿。
3. provider、tool service、executor 等组件持有 `event_bus_id` 的生命周期如何约束。

## 11. 暂定结论

当前实现已把 kernel `BaseRuntime` 的状态恢复边界和 os `Runtime` 的结果输出边界拆开：状态用于恢复，输出用于消费，事件总线用于实时传输过程事件。

暂定结论：

- 后续统一使用 event bus / 事件总线。
- 后续统一使用 event / 事件。
- kernel 不直接发送事件。
- os 层负责事件总线实现，并在合适流程点发送事件。
- 事件总线是运行时通知结构，不承担状态保存职责。
- `event_bus_id` 显式传递，暂不采用线程或协程上下文隐式绑定。
- os Runtime 初始化时保存 `event_bus_id`；未传入时自建通道，析构时只关闭自建通道。
- `sequence` 由 event bus 在发送时从内部全局自增计数器分配。
- 单个事件的多个 listener 并发通知，事件之间按 `sequence` 顺序执行。
- 回调异常进行隔离，不影响 event bus 继续运行。
- 过程事件不单独作为最终结果，流式事件链合并后形成稳定输出。
- 同一事件链只允许 `FINAL` 或 `STARTED -> IN_PROGRESS* -> END` 两种形态。
- 保存 `SessionState` 前会按 `event_id` 折叠每个 `ProcessingTask.event_buffer`；若同一事件链已有 `FINAL`，只保留 `FINAL` 快照，避免恢复重放时再次触发聚合。
- 最终输出统一为 `FINAL RuntimeEvent`，不再维护独立 `RuntimeOutput` 类型体系。
- `parent_event_id` 表达事件树关系，`ToolResultEvent.state_events` 承载工具执行期间产生的子最终事件。
- `ContextCompressionEvent` 保留用于同步压缩输出；异步压缩事件只用于外部观测，不进入本轮稳定输出。
