# turtlemap kernel BaseRuntime 状态流转设计

## 1. 文档目标

本文聚焦 `turtlemap kernel` 中 `BaseRuntime` 的状态流转设计。

本文不重复解释 `kernel` 定位，也不展开 `os` 层治理、编排和产品接口，而是回答下面几个问题：

- `BaseRuntime` 如何基于现有数据结构组织最小闭环
- 输入进入 `BaseRuntime` 后如何分派到已有任务或新任务
- `MessageState`、`ToolState`、`HandoffState`、`AutoResponseState` 如何迁移
- 中断恢复时，`BaseRuntime` 应如何恢复不同任务现场
- 哪些流转属于 `kernel`，哪些细节应留给 `os`

这份文档的目标是为后续 `BaseRuntime` 代码实现提供直接约束。

## 2. 设计原则

### 2.0 术语约定

为避免同一概念出现多种说法，本文统一采用以下术语：

- “任务现场” 指某个 `BaseProcessingTask` 当前可被 `BaseRuntime` 接管并继续处理的状态现场
- “继续执行” 指继续运行任务本身的核心执行逻辑
- “继续推进” 指任务核心执行完成后，继续完成 `History` 回写、状态切换、`input_queue` 补入、`AgentFrame` push/pop 等后续状态转移
- “中断恢复” 指 `BaseRuntime` 重启后，基于最近稳定 checkpoint 重新进入主循环，并继续执行或继续推进
- “阶段性产出” 指任务执行过程中已经稳定落入任务现场、可单独保存并可在后续继续消费的中间结果
- “稳定现场” 指不依赖丢失的内存中间态、可被 `BaseRuntime` 直接接管的任务现场

### 2.1 id 命名约定

本文涉及的各类业务 id，在实现中统一采用 `*_id` 字段命名，字段值统一采用 `业务前缀 + ":" + UUID` 的格式。

例如：

- `session_id = "session:238ab4c8-..."`
- `task_id = "task:238ab4c8-..."`

本文后续若出现 `session_id`、`task_id` 等写法，均按上述统一规则理解；`Agent` 主标识则统一采用 `agent_name`。

### 2.1 BaseRuntime 负责组织闭环，不负责上层策略

`BaseRuntime` 是 `kernel` 的组织轴。

它负责把：

- `Input`
- `SessionState`
- `BaseAgentState`
- `BaseProcessingTask`
- `Tool`
- `ContextEngineering`

组织成稳定的执行循环，但不负责：

- 多 Agent 编排策略
- 权限治理
- tracing
- 高级路由
- 高级上下文压缩策略
- 产品交互策略

### 2.2 一次调度只专注一个执行现场

当前阶段虽然允许一个 Agent 拥有多个未完成任务现场，但一次调度只推进一个 `running` 任务。

约束：

- 同一 Owner Agent 同时最多只能有一个 `running` 任务
- 新输入进入后，优先判断是否命中已有等待任务，否则再创建新任务

### 2.3 状态迁移优先保证可恢复

`BaseRuntime` 的状态迁移设计，优先目标不是“最少对象”，而是“中断后还能恢复”。

因此：

- 未完成 message 不直接写入 `History`
- tool loop 通过 `ToolState` 和 `ExecutionUnit` 保留恢复现场
- handoff 通过 `AgentFrame` 和 `HandoffState` 分别表达控制权切换与等待现场
- 自动响应任务通过 `AutoResponseState` 保留未来事件等待现场

## 3. BaseRuntime 输入输出边界

### 3.1 输入

`BaseRuntime` 当前轮最小输入包括：

- `BaseSessionState`
- 一个或多个 `Input`
- os 层统一事务代理 `OSService`

其中：

- `session_state` 的加载和兜底创建由业务层负责，os `Runtime` 接收外部传入的会话状态。
- `BaseRuntime.run(...)` 接收已经装配好的 `BaseSessionState`，不直接按 `session_id` 读取存储。
- `Input` 是进入输入队列的输入包
- `SessionState` 是状态管理根
- `BaseAgentState` 随 `SessionState.agent_name2agent_state` 保存，并由 `OSService` 按当前 Agent 图补齐缺失项

约束：

- `session_id` 字段名保持 `*_id` 规则，字段值统一采用 `业务前缀 + ":" + UUID` 的格式
- 若外部会话不存在，应由业务层创建新的 `SessionState` 后传入 Runtime

### 3.2 本轮状态变化

`BaseRuntime` 当前轮最小状态变化包括：

- 更新后的 `SessionState`
- 更新后的相关 `BaseAgentState`
- 新增或更新后的 `BaseProcessingTask`
- 新增 `RuntimeArtifact` 历史记录
- 需要继续处理的本轮结果；这些结果会被转换成下一轮 `Input`，再由 `BaseRuntime` 进入下一次循环处理，例如 `tool_result`、`handoff_result`

## 4. 核心控制流程

### 4.1 总体流程

`BaseRuntime` 一次运行可以抽象为：

1. 接收业务层传入的 `SessionState`
2. 由 `OSService.load_session_state(...)` 按当前 Agent 图补齐 `agent_name2agent_state`
3. 由 `OSService.load_session_state(...)` 完成结构一致性检查、控制权栈修复和 run_id 初始化
4. 识别最近稳定 checkpoint 所代表的执行现场
5. 优先判断当前 Owner Agent 是否存在可继续推进的未完成任务
6. 若没有可推进任务，再处理输入队列
7. 分派输入到已有任务或新任务
8. 推进任务状态
9. 回写 `History`、`SessionState`、`BaseAgentState`
10. 判断是否继续下一轮

### 4.2 伪流程

```text
business layer load or create SessionState
OSService.load_session_state(SessionState)
complete agent_name2agent_state
sanitize AgentFrame stack
recover_if_needed_from_checkpoint(SessionState)

enqueue(Input...)

while has_recoverable_task(session) or queue not empty:
    owner_agent = resolve_owner_agent(session)
    task = pick_recoverable_task(owner_agent)
    if task is not None:
        continue_task(task, session, owner_agent)
    else:
        input = dequeue()
        task = create_new_task(owner_agent, start_input=input)
    if task completed:
        checkpoint_task_completed(task, session, owner_agent)
        advance_transition(task, session, owner_agent)
        checkpoint_transition_completed(task, session, owner_agent)
    commit_at_recoverable_point(session, owner_agent, task)
```

## 5. 前置检查与 checkpoint 前提

`BaseRuntime` 在正式推进前，应先做最小结构检查。

### 5.1 AgentFrame 检查

规则：

- `SessionState.agent_frames` 必须非空
- `agent_frames[0]` 必须是 root frame
- 当前 Owner Agent 由 `agent_frames[-1].agent_name` 推导

最低成本修正策略：

- 从 `agent_frames[0]` 开始遍历
- 当前阶段 `agent_name2agent_state` 会做兜底创建，因此不能只检查“能否加载到 `BaseAgentState`”，还需要继续检查这条 frame 恢复链是否仍然有效
- 若某个中间 frame 对应的 `BaseAgentState` 已经没有任何未完成任务，则说明这条控制权链已经断裂
- 一旦检测到 frame 链断裂，不再尝试从中间位置截断后继续恢复，而是直接回退到一个干净的 root 会话环境

具体回退策略：

- `SessionState.agent_frames` 直接重置为仅包含 root frame
- 清理当前会话中所有未完成 `processing_tasks`
- 清理 `SessionState.input_queue`
- 保留历史消息与其他可复用快照数据

这样做的原因是：

- 这类加载异常在正常写入路径下出现概率很低
- 未来若要做更强恢复，可考虑做版本回退，例如从版本 `A` 回退到 `A-1`
- 当前阶段比起把半脏状态继续留给 `BaseRuntime` 主流程处理，直接回到干净 root 会话环境更稳，也能显著减少恢复边界 case

### 5.2 checkpoint 前提

在讨论中断恢复流程前，应先明确：`BaseRuntime` 只围绕最近一个稳定 checkpoint 做恢复，而不是围绕任意中间内存态做恢复。

这意味着：

- 恢复逻辑只面向“已经成功提交”的状态
- 未提交的半中间态不需要专门设计恢复分支
- checkpoint 必须能够独立表达“当前由谁继续、接下来等什么、下一轮从哪里开始”
- 中断恢复只是基于 checkpoint 的继续执行或继续推进，而不是重新推断一段丢失的执行过程

## 6. 中断恢复流程

### 6.1 何时触发恢复

`BaseRuntime` 重启后不需要为中断恢复设计复杂特例，而应直接基于当前 `SessionState`、`input_queue` 和最近稳定 checkpoint 重新进入正常主循环。

进入主循环后，不应立即从 `input_queue` 取出新输入，而应先判断当前 Owner Agent 是否已经存在稳定可恢复的未完成任务现场。

只有当当前没有可继续推进的任务时，才应从 `input_queue` 中取出新的 `Input` 并开始创建新任务。

### 6.2 恢复优先级

当前阶段建议：

1. 若存在“已完成但尚未完成状态转移推进”的任务，先继续推进
2. 若存在可继续执行的未完成任务，先继续该任务
3. 只有当当前没有可推进任务时，才从 `input_queue` 中取出新输入并创建新任务
4. `AutoResponseState` 只有在命中其 `trigger_event_type` 对应事件时才恢复

补充说明：

- `BaseRuntime` 主循环的驱动单位不是“先取一个输入”，而是“先找到下一个可推进单元并推进”
- 这个可推进单元可能是一个已有未完成任务，也可能是在无任务可推进时取出的一个新 `Input`

### 6.3 MessageState 恢复

`MessageState` 中断时：

- 不把 LLM 流式增量视为可靠恢复状态
- 恢复时应基于最近 checkpoint 中保存的任务现场继续执行

约束：

- 未完成 message 不进入 `History`
- 若客户端曾看到部分流式输出，由 `os` 审计或展示通道承接

### 6.4 ToolState 恢复

`ToolState` 中断时：

- 依据 `messages` 和 `execution_units` 重建稳定现场
- 若当前 `ToolState.status = running`，则 `BaseRuntime` 按 `execution_units` 的顺序扫描，找到第一个 `pending` 的 unit
- 已完成 unit 不重复执行
- `pending` unit 可直接启动执行
- 等待外部输入或异步结果时，任务应处于 `paused` 并持有中断 request；收到 response 后恢复任务，再继续执行原 `pending` unit
- unit 执行完成后，`BaseRuntime` 不自行推断下一状态，而是读取 `ExecutionUnitResult.next_unit_status` 更新该 unit
- 过程中的阶段性 checkpoint 以每个 `ExecutionUnit` 完成为边界

当前阶段 `kernel` 只负责：

- 根据 `tool_call_id` 匹配 `tool_result`
- 保留 tool loop 顺序
- 按显式 unit 列表推进，不额外维护 `ToolState.phase`

补充约束：

- `background` 工具的异步闭环不再挂在原 `ToolState` 上等待结果
- 原 `ToolState` 在当前轮完成后，应通过 `AutoResponseState` 承接后续 `tool_result` 事件
- 具体规则见本文“11. background 工具异步闭环”

### 6.5 HandoffState 恢复

当前阶段不建议把 `HandoffState` 设计成需要单独恢复的复杂中间现场。

原因是：

- handoff 完成信号由子 Agent 调用 `complete_handoff` 明确触发
- 状态保存时机可以选择在子 Agent frame 已完成、且回切推进条件已经具备之后
- 这样恢复时只需要继续推进 handoff 完成后的 `AgentFrame` 回切流程，而不需要解释半截 `HandoffState`

因此约束应为：

- `BaseRuntime` 不应在“等待 `handoff_result` 但尚未形成稳定回切点”的半中间态保存
- `complete_handoff` 一旦成功触发，应把 handoff 完成所需信息整理为稳定结果，再统一提交
- 恢复时只处理“handoff 已完成但后续循环尚未继续”的稳定现场

### 6.6 AutoResponseState 恢复

`AutoResponseState` 中断时：

- 默认继续保持 `running`
- 若恢复时发现触发事件已进入输入队列，则恢复对应任务
- 对 `trigger_event_type = tool_result` 的场景，恢复目标是继续补齐异步 unit 结果，而不是重新执行原 `ToolState`

## 7. 输入分派逻辑

### 7.1 输入入队

每个 `Input` 先进入 `SessionState.input_queue`。

出队时：

- 写入新建 `BaseProcessingTask.start_input`
- 按 `priority` 和输入顺序处理 `events`

约束：

- `input_queue` 必须持久化，不能只存在内存中
- `Input` 成功写入 `input_queue` 只表示输入已经可靠进入系统，这属于输入持久化，不等同于新的 `BaseRuntime` checkpoint
- `tool_result`、`handoff_result`、定时触发结果等若要进入下一轮，都应先写入 `input_queue`
- `BaseProcessingTask.start_input` 只表示启动任务时已经出队并被当前任务接管的输入
- `Input.events` 虽然定义为数组，但当前阶段一个 `Input` 中只处理一个 `ObservableEvent`；保留数组结构是为后续并发或批处理扩展做准备

### 7.2 事件分派顺序

当前阶段建议：

1. 先按 `priority` 排序
2. 再按输入中原始顺序稳定处理

### 7.3 匹配已有任务

`BaseRuntime` 先根据当前 Owner Agent 的 `processing_tasks` 尝试匹配已有任务。

匹配原则：

- `tool_result` 优先匹配 `AutoResponseState`
- `handoff_result` 优先匹配 `HandoffState`
- `environment_message` 优先匹配 `AutoResponseState`
- `user_input` 默认创建新任务；中断 response 则按 `request_id` 匹配暂停任务并恢复执行

### 7.4 无运行中任务时的 Input -> MessageState 接管

当前 Owner Agent 若不存在可继续推进的 `running` 任务，则 `BaseRuntime` 才进入新输入接管流程。

当前阶段建议按下面顺序处理：

1. 从 `SessionState.input_queue` 中取出一个 `Input`
2. 读取 `Input.events[0]`
3. 若该事件是 `user_input`，则创建新的 `BaseProcessingTask(MessageState, running, start_input=input)`
4. 将该任务加入当前 Owner Agent 的 `processing_tasks`
5. 进入 `MessageState` 执行流程

补充约束：

- 只有当前没有运行中任务时，才允许从 `input_queue` 取出新的 `Input`
- 当前阶段 `Input.events` 虽为数组，但运行时按单事件处理，因此默认读取第一个事件
- 创建任务时应记录 `start_input`
- `MessageState` 不保存流式生成增量；过程输出由 os event bus 承接
- `MessageState.message` 只保存稳定 `RuntimeArtifact`

## 8. 主要状态迁移

### 8.1 用户输入主路径

```text
user_input
-> dequeue Input
-> create BaseProcessingTask(MessageState, running, start_input=input)
-> ContextEngineering 构建上下文
-> LLM 生成 assistant message
-> 若为普通回复:
   -> 写入 History
   -> MessageState completed
-> 若为 tool_calls:
   -> MessageState completed
   -> 切换到 ToolState
```

### 8.2 MessageState 执行

当 `BaseRuntime` 因 `user_input` 创建新的 `MessageState` 后，执行顺序建议为：

1. 创建新的 `BaseProcessingTask`
2. 将 `state` 初始化为 `MessageState(status = running)`
3. 将 `start_input` 设为当前出队的 `Input`，并将 `origin_input_id` 设为 `start_input.input_id`
4. 以“MessageState 任务已创建完成”为边界保存一次关键 checkpoint
5. 基于当前 `SessionState`、Owner Agent 和 `BaseAgentState` 构建上下文
6. 发起一次 LLM 调用
7. LLM 生成过程中的流式事件由 os event bus 发送
8. 当消息最终确认完成后，将稳定结果写入 `MessageState.message`
9. 再根据稳定结果决定：
   - 写入 `History`
   - 或切换到 `ToolState`

约束：

- `MessageState` 创建完成后应立即形成一个可恢复 checkpoint
- `MessageState` 执行过程中不再按增量片段追加 checkpoint
- 若中断，恢复时应基于已保存的任务现场重新执行该 message 任务

### 8.3 MessageState -> ToolState

触发条件：

- 当前 `MessageState` 已完成
- assistant 输出结果被确认是 `tool_calls`

迁移动作：

1. 基于 assistant 返回的 `tool_calls` 一次性生成完整 `ExecutionUnit` 列表
2. 创建或切换到 `ToolState`
3. 将 assistant 的工具调用产物保存到 `ToolState.tool_call_message`
4. 后续由 `BaseRuntime` 按 unit 顺序逐个执行

### 8.4 ToolState -> continuation

当 `ToolState` 推进到某个执行单元时：

1. 按顺序扫描 `execution_units`
2. 找到第一个 `status = pending` 的 unit 并启动执行
3. unit 执行完成后写入对应 `ExecutionUnitResult`
4. `BaseRuntime` 读取 `ExecutionUnitResult.next_unit_status`，更新当前 unit 的状态
5. 若结果中携带 `appended_execution_units`，则追加到当前 `ToolState.execution_units`
6. 若结果中携带 `task_switch_action = pause`，则挂载中断 request 并暂停任务
7. 以该 `ExecutionUnit` 完成为边界建立一次阶段性 checkpoint
8. 若所有 unit 都已完成，则进入 complete 阶段统一构建并写入最终 `History`

### 8.4.1 确认类中断业务流转

确认类 unit 通过任务中断模型等待外部 response，例如：

- 工具执行前需要用户确认
- 工具执行中需要用户补充结构化参数
- 工具执行中需要用户对上一步结果做明确选择

这类 unit 的处理边界建议如下：

1. unit 首次执行时构造 `InterruptionRequest`，并将任务切换为 `paused`
2. 原 unit 保持 `pending`，不创建额外的 unit 级等待状态
3. response 通过 `request_id` 匹配 request，挂载 response 后将任务恢复为 `running`
4. unit 再次执行时读取 request 中的 response，并在 os 层解释更细的业务判定

当前阶段建议至少支持以下业务结果：

- `confirmed`
- `rejected`
- `clarify`
- `switch_topic`

其中：

- `confirmed` / `rejected`
  当前确认类 unit 结束；后续 unit 可以继续执行，也可以由 `os` 层决定跳过某些分支 unit
- `clarify`
  当前确认类 unit 不结束，而是重新构造中断 request 并再次暂停任务
- `switch_topic`
  当前输入不再继续服务当前任务，而是要求 `BaseRuntime` 暂停当前任务，并把当前输入作为新任务起点继续处理

补充约束：

- `clarify` 不应创建新的 `BaseProcessingTask` 或确认 unit；应优先在同一个 unit 内保留多轮确认现场
- `switch_topic` 不应只靠自然语言隐式约定；应由 `ExecutionUnitResult.task_switch_action` 明确要求 `BaseRuntime` 接管任务级切换
- 对于 `confirmed` / `rejected` / `clarify` 这类 unit 内部业务结果，`kernel` 不要求统一建模到 `ExecutionUnitResult` 基类，而是由 `os` 层结果子类承接

### 8.4.2 `switch_topic` 的 BaseRuntime 动作

当确认类 unit 返回的话题切换结果要求 `BaseRuntime` 接管时，建议执行以下动作：

1. 当前 `BaseProcessingTask` 从 `running` 切到 `paused`
2. 写入一条“当前任务已暂停，等待后续恢复”的占位消息到 `History`
3. 清理当前轮已接管输入现场
4. 将当前新输入作为新任务起点，走正常的 `Input -> MessageState` 创建流程

这样做的原因是：

- `switch_topic` 是任务级调度动作，不只是 unit 局部状态变化
- 当前输入的归属需要被明确切换给新任务
- 被暂停任务后续仍可能通过显式恢复入口重新进入主循环

### 8.5 ToolState -> background

`background` 工具不再采用“原 `ToolState` 保持未完成并等待外部补写结果”的设计。

当前阶段建议改为：

1. `background` 类型的 tool unit 在当前轮执行时直接返回占位描述
2. 该 unit 当轮即闭合为 `completed`
3. 带 `background` unit 的 `ToolState` 不额外生成 `llm_call` unit
4. 原 `ToolState` 正常走完整个 complete 推进
5. 异步结果通知改由独立的 `AutoResponseState` 承接

完整规则见本文“11. background 工具异步闭环”。

### 8.6 handoff 主路径

```text
assistant 决定 handoff
-> 创建/更新原 Agent 的 HandoffState(waiting)
-> push AgentFrame
-> 目标 Agent 成为 Owner Agent
-> BaseRuntime 注入 complete_handoff
-> 目标 Agent 多轮执行
-> complete_handoff
-> 生成 handoff_result
-> pop AgentFrame
-> 若存在 return_task_id:
   -> 将 handoff_result 转成新 Input
   -> 在下一轮恢复原任务推进
```

关键点：

- `AgentFrame` 表达控制权切换
- `HandoffState` 只表达原 Agent 的等待语义，不应承载需要复杂恢复的半中间态
- handoff 的恢复重点应放在 `AgentFrame` 完成后的推进，而不是解释未完成的 handoff 中断现场

### 8.7 AutoResponseState 主路径

```text
创建 AutoResponseState(running)
-> 等待 trigger_event_type 对应事件
-> 命中触发条件
-> AutoResponseState running
-> 生成回复或触发 tool
-> 完成后写入 History
```

## 9. History 回写规则

### 9.1 应写入 History 的内容

以下内容应写入 `History`：

- 已完成的 `user` / `assistant` 可见消息
- 已确认完成的 `tool` role 消息
- 后台任务的占位回复
- 含 `background` unit 的 `ToolState` 完成推进时补入的固定任务说明消息
- handoff 过程中真正对用户可见的消息
- `switch_topic` 导致任务暂停时写入的占位消息

### 9.2 不应写入 History 的内容

以下内容不应直接写入 `History`：

- LLM 流式生成过程中的临时事件
- 未完成 tool loop 中的临时内部状态
- 未完成 handoff 的等待现场
- 自动响应任务的等待态

### 9.3 回写时机

建议规则：

- 普通 assistant 回复完成时立即写入
- tool loop 完整闭合后写入最终用户可见结果
- `background` 工具在当前轮写入占位回复，并在 `ToolState` complete 推进时补入固定任务说明消息
- handoff 完成后的结果在回切后按实际输出写入
- `switch_topic` 发生时，由 `BaseRuntime` 在任务切换点写入 pause 占位消息

## 9.4 background 工具异步闭环

### 9.4.1 设计目标

`background` 工具的核心目标是：

- 当前轮先稳定闭合原始 `ToolState`
- 让用户立即看到占位描述和固定说明消息
- 异步结果未来回流时，不直接恢复原 `ToolState`
- 改由一个独立的 `AutoResponseState` 承接结果补齐与后续通知

### 9.4.2 当前轮执行规则

当某个 tool unit 的 `execution_strategy = async` 时，建议按以下规则执行：

1. 当前 unit 执行时只生成占位描述，不等待稳定工具结果
2. 当前 unit 直接闭合为 `completed`
3. 当前 `ToolState` 继续执行后续 unit
4. 若当前 `ToolState` 中存在 `background` unit，则构建 unit 时不再追加 `llm_call` unit

这样做的原因是：

- `background` 代表“当前轮同步执行已经结束”，而不是“当前 unit 仍需继续等待”
- 后台任务已提交和需要用户确认都通过任务暂停与中断 request 表达，不引入 unit 级等待状态

### 9.4.3 ToolState complete 推进规则

当原 `ToolState` 进入 complete 推进阶段时，若检测到存在 `background` unit，建议执行以下动作：

1. 正常整理当前 `ToolState` 的稳定消息并写入 `History`
2. 额外补一条固定任务说明消息，明确告知“任务已转为异步执行，结果返回后会通知用户”
3. 创建一个新的 `AutoResponseState`
4. 新建的 `AutoResponseState.status` 直接为 `running`
5. `trigger_event_type` 固定为 `tool_result`
6. `payload` 装载当前异步闭环所需的最小恢复材料
7. 由 `ToolService` 负责识别哪些 unit 属于异步 unit，并启动对应后台任务

其中 `payload` 建议使用带 `type` 判别字段的 `TypedDict union` 结构，而不是裸字典。

### 9.4.4 AutoResponseState 建议负载

对于 `trigger_event_type = tool_result` 的 `AutoResponseState`，`payload` 建议至少承接：

- `source_task_id`
- `source_agent_name`
- `origin_input`
- `tool_state`
- `background_units`

其中：

- `tool_state` 用于保留原始 tool loop 的稳定现场
- `background_units` 用于记录哪些 `unit_id` 正在等待异步结果，以及哪些结果已经回填

### 9.4.5 tool_result 回流规则

后台任务完成后，应统一以新的 `Input` 重新进入 `BaseRuntime`。

当前阶段建议：

- `ObservableEvent.event_type = tool_result`
- `ObservableEvent.payload` 使用 `TypedDict union`
- `payload` 中至少携带：
  - `auto_response_task_id`
  - `unit_id`
  - `tool_result`

`BaseRuntime` 收到该事件后：

1. 优先匹配对应 `AutoResponseState`
2. 根据 `auto_response_task_id` 和 `unit_id` 把结果回填到 `payload.background_units`
3. 检查当前 `AutoResponseState` 是否已经收齐全部异步 unit 的结果
4. 若未收齐，则保持 `running`
5. 若已收齐，则将该 `AutoResponseState` 切到 `completed`

### 9.4.6 AutoResponseState complete 推进规则

当 `AutoResponseState` 进入 complete 推进阶段，且 `trigger_event_type = tool_result` 时，建议：

1. 从 `payload` 中取出原始 `tool_state`
2. 取出原始问题与全部异步 `tool_result`
3. 构造一个新的 system input
4. 新 input 中同时包含：
   - 原始问题
   - 后台工具调用结果
   - 其他必要恢复上下文
5. 让该 system input 重新进入标准 `MessageState` 创建流程

这里的关键原则是：

- 异步结果回流后，继续通知用户属于一轮新的系统输入
- 它不再是“恢复原始同步 tool task”，而是“基于原始问题和异步结果重新组织一次通知型回复”

## 10. SessionState 更新规则

### 10.1 input_queue / start_input

- 新输入先写入 `SessionState.input_queue`
- 当前轮开始接管某个输入时，从 `input_queue` 出队并固定写入 `BaseProcessingTask.start_input`
- `BaseSessionState` 不保存当前输入现场
- 若处理中断，未完成任务及其 `start_input` 共同构成可恢复现场

### 10.2 agent_frames

- handoff 开始时 push
- handoff 完成时 pop
- 当前 Owner Agent 永远由 `agent_frames[-1].agent_name` 推导

### 10.3 processing_tasks

- 新任务创建时加入当前 Owner Agent 的 `processing_tasks`
- 完成任务可移除，或在保留历史恢复价值时标记 `completed`
- `paused` 任务保留，用于后续恢复、跳转或未完成任务管理
- `paused` 任务保留其 `InterruptionRequest`，等待匹配 response 后恢复为 `running`

## 11. 可恢复节点与统一保存规则

### 11.1 基本原则

核心原则只有一句话：只有当当前状态已经形成不依赖丢失内存中间态的稳定现场时，才允许保存为 checkpoint。

每次保存统一提交：

- `SessionState`
- 相关 `BaseAgentState`
- `History` 增量
- 版本映射关系

### 11.2 checkpoint 分类

当前阶段建议按通用状态节点划分 checkpoint，而不是按业务路径逐一设计。

- “任务创建完成 checkpoint” 表示 `BaseProcessingTask` 已创建完成并进入可调度状态，恢复后 `BaseRuntime` 已经知道当前任务现场从哪里继续
- “任务执行完成 checkpoint” 表示任务核心执行已经结束，但后续状态转移推进尚未完成
- “任务子阶段完成 checkpoint” 表示任务整体尚未结束，但某个子阶段已经形成稳定、可独立恢复的阶段性产出

边界约束：

- checkpoint 只保存稳定现场，不依赖丢失的内存中间态
- “任务执行完成 checkpoint” 不包含后续状态转移推进，例如 `History` 回写、状态切换或任务清理
- “任务子阶段完成 checkpoint” 只在确实出现稳定阶段性产出时建立，不等于所有 `running` 片段都要保存

这意味着：

- `input_queue` 没有独立于 `SessionState` 的单独存储；其持久化仍依赖 `SessionState` 的版本化保存
- `input_queue` 追加只承担输入不丢的持久化职责，不单独构成 `BaseRuntime` checkpoint
- 任务执行与状态转移推进应分开；两者之间以及推进完成后都允许形成稳定 checkpoint
- 恢复流程应围绕“当前 checkpoint 属于哪一类”继续推进，而不是围绕具体业务名称分叉

### 11.3 当前阶段建议的可恢复节点

当前阶段不必逐条枚举所有业务路径的可恢复节点。

只要某个时刻明确落入上一节定义的 checkpoint 分类之一，并且满足：

- 已形成稳定现场
- 不依赖丢失的内存中间态
- 重启后可以直接继续执行或继续推进状态转移

那么该时刻就应允许统一保存。

具体实现时，普通回复、tool loop、handoff、auto response 都只需要对照上述三类 checkpoint 判断是否允许保存。

## 12. 最小异常处理

当前阶段 `kernel` 只处理最基本异常：

- SessionState 结构不一致
- Owner Agent 缺失
- 多个 `running` 任务冲突
- `tool_result` 找不到对应 `tool_call_id`
- `handoff_result` 找不到对应 `handoff_id`

处理原则：

- 先保证结构不进一步损坏
- 能明确恢复的就恢复
- 无法自动恢复的保留错误状态并停止当前任务

## 13. 版本化提交与恢复协议

### 13.1 设计目标

`BaseRuntime` 的状态提交应保持对 kernel 简单，对业务存储可扩展。

当前目标是：

- kernel 只维护内存态推进；
- os Runtime 通过 `OSService.save_session_state(...)` 提交 checkpoint；
- 业务层 StateStore 决定是否使用乐观锁、追加版本或覆盖保存；
- `BaseAgentState` 随 `SessionState` 完整保存，不再单独形成引用关系。

### 13.2 核心思路

当前建议：

1. 业务层加载或创建 `SessionState`
2. os Runtime 初始化时补齐 `agent_name2agent_state`
3. kernel 推进状态
4. checkpoint 时保存完整 SessionState 快照

也就是说：

- `SessionState` 是唯一生效锚点
- `BaseAgentState` 不再单独保存为可被引用的版本
- `input_queue` 的追加也通过保存新的 `SessionState` 实现；只是这种保存语义上属于输入持久化，不等同于新的 `BaseRuntime` checkpoint

### 13.3 版本字段建议

当前建议由业务层 SessionState 子类或 persistence model 承接版本化存储。

其中：

- SDK `BaseSessionState` 不内置版本字段
- SDK `BaseAgentState` 不单独维护版本
- 业务层可以像 `examples.mysql_store.MySQLSessionState` 一样增加 `version` 和 `schema_version`
- `BaseAgentState` 随 `SessionState.agent_name2agent_state` 整体保存

补充约束：

- 业务层 `SessionState.version` 表示当前会话的持久化版本，不只用于快照恢复，也用于校准 `os` 层基于会话生成的派生记录
- `os` 层若保存会话记录、展示记录、索引记录或其他派生投影，可由业务层按自身 SessionState 版本字段绑定
- `os` 层派生记录不能反向作为 `kernel` 恢复依据；`kernel` 中真实状态仍以 `SessionState(version=...)` 为准

### 13.4 提交顺序

建议一次 `BaseRuntime` 提交按下面顺序执行：

1. kernel 更新当前内存态 `SessionState`
2. os Runtime 调用 `OSService.save_session_state(...)`
3. 具体 StateStore 保存完整 SessionState 快照
4. 若业务层状态模型带有乐观锁版本，则在保存成功后回写新版本

关键约束：

- `SessionState` 是唯一生效锚点
- `BaseAgentState` 已随 SessionState 保存，不再存在独立提交顺序
- StateStore 不应把保存失败静默吞掉
- `input_queue` 的持久化同样通过保存 SessionState 完成。

### 13.5 恢复规则

恢复时建议：

1. 业务层读取或创建某个 `session_id` 对应的 `SessionState`
2. 将 `SessionState` 传入 os `Runtime.run(...)`
3. `OSService.load_session_state(...)` 根据当前 Agent 图补齐 `agent_name2agent_state`
4. 若某个 BaseAgentState 缺失，则根据当前运行配置初始化干净状态。

补充说明：

- 代码中的 `system` 是当前权威定义，恢复后会覆盖旧 AgentState.system
- 业务层如果需要结构兼容隔离，应通过 SessionState 子类的 `schema_version` 整体控制
- BaseAgentState 不再由 `AgentInfo` 或独立版本引用；后续分支化恢复规则单独设计

当前阶段不再通过 `SessionState` 引用某个 BaseAgentState 快照版本；分支化一致性策略后续单独细化。

### 13.6 os 派生记录校准

恢复时除了恢复 `kernel` 自身状态，业务层也可以校准 `os` 层已生成的派生记录。

建议规则：

1. 若业务层 SessionState 带有 `version`，派生记录可以绑定 `session_id + version`
2. 若没有版本字段，派生记录应只作为展示、审计或观测数据，不反向参与 kernel 恢复
3. 业务层派生记录过期时可以重建，但不应覆盖 Runtime 的真实状态

## 14. 版本保留与清理策略

### 14.1 清理原则

清理策略由具体业务存储实现决定。

原因是：

- 时间不能表达状态是否仍是最近稳定恢复点
- 老会话的最新稳定版本必须保留
- 短时间内高频写入会话，仅按时间也无法有效控制体积

### 14.2 推荐保留策略

当前建议每个 `session_id`：

1. 至少保留最新可恢复的 `SessionState`
2. 若业务层使用版本化快照，可额外保留最近 `2` 到 `5` 个已提交稳定版本
3. 历史展示、观测事实和审计记录按业务保留策略独立清理

### 14.3 可清理对象

以下对象可以安全清理：

- 已被新版本完全替代、且不在保留窗口内的旧 `SessionState`
- 已确认不再需要的业务派生记录

### 14.4 可选时间兜底

如果后续需要时间策略，建议只作为兜底规则，而不是主规则。

例如：

- 清理 `7` 天前且不在最近版本保留窗口内的历史会话快照

当前阶段不建议使用：

- “清理 10 分钟前版本”
- “清理 1 小时前版本”

因为这种策略会让恢复点和排障窗口变得不可控。

## 15. kernel / os 边界

### 属于 kernel

- 输入进入后的最小分派
- Owner Agent 推导
- 任务现场创建、切换、恢复
- `ToolState` / `HandoffState` / `AutoResponseState` 的最小迁移
- 仅针对 `background` 工具触发 waiting 状态自检兜底
- `History` 的最小回写规则

### 属于 os

- 输入采集和归一化
- 权限和治理
- tool 执行器细节
- `background` 工具外部状态查询与异步结果补录实现
- 长时任务轮询和重试
- token 预算策略
- 高级压缩策略
- tracing / 审计 / 展示通道
- 自动响应复杂触发规则

## 16. 第一阶段实现建议

第一阶段建议只实现四条最小主路径：

1. `user_input -> MessageState -> History`
2. `assistant tool_calls -> ToolState -> tool_result -> continuation`
3. `handoff -> AgentFrame push/pop -> 等待态完成后的下一轮推进`
4. 中断恢复 `MessageState` / `ToolState`

暂时不必一次性做全：

- 复杂 `AutoResponseState`
- 高级多任务调度
- tool 执行器策略
- 复杂压缩回写

此外建议首批就把以下基础设施一起做掉：

- `SessionState` / `BaseAgentState` 的版本化快照存储
- `SessionState` 作为唯一生效锚点的提交协议
- 基于引用关系的最小清理策略

## 17. 当前版本结论

当前版本的核心判断是：

- `BaseRuntime` 是 `kernel` 真正把数据结构组织成闭环的核心
- `SessionState`、`BaseAgentState`、`BaseProcessingTask` 提供最小可恢复现场
- 输入分派优先命中已有等待任务，否则创建新任务
- handoff 通过 `AgentFrame` 表达控制权切换，`HandoffState` 仅表达等待语义，不作为复杂中断恢复现场
- `History` 只保存已完成且面向后续 LLM 输入有价值的用户可见记录
- 状态保存只发生在可恢复节点，而不是跟随每一次局部内存变动
- checkpoint 应按状态节点类型分类，而不是按 `handoff`、`tool` 这类业务路径单独分类
- `SessionState` 作为版本化提交的唯一生效锚点
- 版本清理优先依据引用关系，而不是依据时间

只要这套状态流转先立住，后续 `BaseRuntime` 代码实现和最小可运行样例就有了稳定落点。
