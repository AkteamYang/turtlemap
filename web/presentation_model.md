# TurtleMap Web 展示模型设计

## 1. 文档目的

本文定义 Web 聊天区、详情面板和事件展示的稳定建模规则。它独立于 [design.md](./design.md) 的接口与页面范围设计，重点回答以下问题：

- SSE 与 history 原始事件如何进入前端状态。
- `ChatItem`、消息块和详情条目的职责边界是什么。
- 中断、恢复和历史事件如何跨 item 联动。
- 历史分组、事件详情、胶囊和折叠交互如何由 View Model 决定。

实现以 [types.ts](./src/types.ts)、[chatState.ts](./src/chatState.ts) 和 [App.tsx](./src/App.tsx) 为准。

## 2. 核心原则

### 2.1 单向数据流

前端遵循固定链路：

```text
服务端 history / SSE 事件
  -> ChatItem 原始事件状态
  -> ChatItemPresentation / DetailSelection View Model
  -> React 组件渲染
```

React 组件不得直接解释 SSE 协议字段，也不得为了修正视觉效果改写原始事件。展示差异必须在“事件到 View Model”的投影层解决。

### 2.2 原始事件不可变且有序

- `ChatItem.events` 是服务端事件的原始顺序副本。
- 每次 SSE 到达仅追加新事件，或以同一事件的最终态修正对应 View Model；不按角色、类型或历史标记重排 `events`。
- history 接口中的 `HistoryMessage.content` 已是服务端持久化的一条任务记录，整体映射为一个 `ChatItem`。
- 任何视觉上的重组只发生在投影结果中，并需明确写出展示顺序规则。

### 2.3 任务与记录是不同层级

`task_id` 描述运行任务，`ChatItem` 描述一次可持久化、可展示的记录。一个任务可能被中断后恢复，因此多个 `ChatItem` 可以具有同一 `task_id`。

因此：

- 不允许用全局 `task_id -> ChatItem` 唯一映射覆盖历史记录。
- 同一请求缓冲区中，SSE 可以按 `task_id` 追加到当前运行 item。
- 跨 item 联动仅用于同步任务级状态，例如将旧中断卡片标记为已恢复；不得跨 item 合并工具块或文本块。

### 2.4 UI 状态与业务数据分离

- 原始事件、`ChatItem.status`、中断恢复状态属于业务展示数据。
- `historyExpanded`、详情面板选中项、单个详情事件是否展开属于 UI 状态。
- UI 状态不回写 SSE 数据，不影响 history 持久化；刷新页面后可恢复默认值。

## 3. 原始数据模型

### 3.1 服务端事件

`ServerMessageEvent` 是协议边界对象，关键字段如下：

| 字段 | 作用 |
| --- | --- |
| `type` | 事件类型，例如 `input`、`message_final`、`tool_result`、`complete`。 |
| `task_id` | 运行任务标识，用于追加当前 item 与跨 item 中断状态同步。 |
| `event_id` | 业务事件标识，用于块 id、同事件开始/结束态更新等。 |
| `run_id` | 本次服务端运行标识，用于 SSE 去重与恢复。 |
| `start_ts_ms` | 事件开始时间，用于排序、耗时和新 item 的创建时间。 |
| `agent_name` | 当前回答 Agent 名称，用于相邻 item 的 Agent 切换提示。 |
| `is_history_event_for_interruption` | 当前事件是否为中断恢复时回放的历史片段。 |
| `data` | 各事件类型的业务负载。 |

`data` 是事件语义的唯一来源。详情 JSON 展示原始 SSE event；不要展示前端为便捷生成的 metadata 包装对象。

### 3.2 ChatItem

`ChatItem` 是聊天列表的原始状态单位，而不是直接渲染的消息组件。

| 字段 | 规则 |
| --- | --- |
| `id` | history 使用 `message_id`；流式新 item 初始使用首个 `event_id`；本地占位使用本地 id。 |
| `taskId` | 允许为空，也允许在多个 item 中重复。 |
| `clientEventId` | 用于本地乐观输入与首批 SSE `input` 去重。 |
| `events` | 该记录的原始有序事件集合。 |
| `status` | `pending`、`streaming`、`complete`、`error`。 |
| `optimisticInput` | 用户提交后、服务端 `input` 尚未到达前的临时输入。 |
| `historyExpanded` | 当前页面内“展开任务”的折叠状态。 |
| `interruptionStateByRequestId` | 由同 task 的恢复输入推导出的中断终态。 |

### 3.3 SSE 归属规则

`applyServerEvent` 将一个 SSE 事件归属到 `ChatItem` 时，按以下优先级寻找目标：

1. 用 `data.client_event_id` 匹配仍未完成的乐观 item。
2. 从列表末尾寻找同 `task_id` 的可追加 item。
3. 最后回退到最近的 `streaming` item。
4. 无目标时创建新 `ChatItem`。

`task_id=null` 且没有有效事件的 `complete` 是空恢复完成信号，不创建或保留 UI item。这样 `start + complete + end` 的 resume 不会造成页面闪现。

## 4. 聊天展示 View Model

### 4.1 ChatItemPresentation

`buildChatItemPresentation(item, now)` 将原始 item 投影为：

```ts
type ChatItemPresentation = {
  inputBlock: TextBlock | null;
  agentName: string | null;
  historyBlocks: ChatMessageBlock[];
  currentBlocks: ChatMessageBlock[];
};
```

页面固定按下列顺序渲染，而非直接遍历 `events`：

```text
非历史分组的首个 input（用户消息）
-> Agent 切换提示（仅与上一个可见 item 的 Agent 不同）
-> 历史分组（默认折叠）
-> 非历史分组的其余事件块
```

这是聊天叙事顺序；原始事件顺序仍保留在 `ChatItem.events` 和详情面板中。

### 4.2 input 规则

| 条件 | 展示规则 |
| --- | --- |
| 非历史 `input` 且 `data.event_type=user_input` | 作为右侧 user 消息展示。 |
| 非历史 `input` 且 `data.event_type=interruption_response` | 不在当前 item 顶部展示；其语义用于更新同 task 的中断状态。 |
| 非历史 `input` 且 `data.source=handoff` | 系统生成的交接输入，不在当前 item 顶部展示；Agent 标识仍可由该事件确定。 |
| 尚未收到服务端 input | 若有 `optimisticInput`，临时展示为用户消息。 |
| 历史 input 且 `event_type=user_input` | 仅在历史分组中展示为引用样式，内容仅取 `data.user_input`。 |

### 4.3 历史分组规则

`is_history_event_for_interruption=true` 的事件属于中断恢复回放历史。

- 聊天区将此类事件投影为 `historyBlocks`，默认折叠在“展开任务（N）”内。
- 该折叠状态仅保存在 `ChatItem.historyExpanded`，滚动或 SSE 更新不会重置；刷新 history 后默认折叠。
- 历史区只负责当前 item 内的视觉归档，不改变事件的归属，不参与跨 item 工具状态匹配。
- 展开/收起使用统一的 `grid-template-rows 180ms ease` 抽屉动画；不使用透明度渐隐。

### 4.4 消息块

`ChatMessageBlock` 是聊天 UI 的直接 View Model。所有块由 `eventsToBlocks` 以事件顺序增量构建。

| 块类型 | 来源事件 | 更新规则 |
| --- | --- | --- |
| `text` | `message_delta`、`message_final`、`input` | delta 按 `event_id` 追加；final 用稳定内容替换同一文本块。 |
| `activity_label` | 工具、上下文压缩、通用活动、思考、失败 | 按稳定 block id 新增或更新。 |
| `interrupted` | `interrupted` | 呈现原因、操作和恢复后的终态。 |
| `completion` | `complete` 或运行中计时 | 一个 item 只保留一个最终 completion 块。 |

当前 item 为 `streaming` 时，投影末尾额外追加：

```text
...已有业务块 -> 正在思考 -> 实时 completion
```

实时 completion 使用 `now - createdAt` 计算，不依赖累计计时器；最终 `complete.data.duration_ms` 到达后替换为服务端耗时。

### 4.5 工具块规则

工具相关事件始终投影为 `source="tool"` 的 `activity_label`，但只在同一个 `ChatItem` 内匹配：

| 事件序列 | 工具块行为 |
| --- | --- |
| `tool_call` | 创建 loading 工具调用块，标题为工具名，副标题为参数。 |
| `tool_call -> tool_result` | 以 `tool_call.data.id = tool_result.data.tool_call_data.id` 匹配，将原块更新为结果态；详情事件为二者。 |
| `tool_result` 未匹配调用 | 独立创建工具结果块，副标题展示 `data.raw_data.content`。 |
| 审批中断 | 停止已有工具调用 loading；命中 `source_tool_id` 的块标记为 `approvalPending`。 |
| 审批恢复后的 `tool_result_start` | 仅在已有同 id 工具块且其为 `approvalPending` 时创建新的 loading 工具块；详情起点是 `tool_result_start`。 |
| `tool_result_start -> tool_result` | 更新新工具块为结果态；详情事件为这两个事件。 |
| 工具名以 `_handoff` 开头 | 不构建聊天工具标签；原始事件仍保留，complete 详情照常展示。 |

不做跨 item 的工具块合并、状态更新或详情拼接。

### 4.6 上下文压缩与活动标签

| 事件 | 状态与文案 |
| --- | --- |
| `context_compression_start` | loading，副标题形如 `L0 · 正在整理会话上下文`。 |
| `context_compression_final` 且 `merged=false` | 终态失败图标，副标题 `Lx · 压缩失败 · 耗时`。 |
| `context_compression_final` 且 `merged=true, level=1` | 终态成功，副标题 `L1 · 已裁剪会话记忆 · 耗时`。 |
| `context_compression_final` 且 `merged=true, level=0/2` | 终态成功，副标题 `Lx · 已压缩 N 条历史 · 耗时`。 |
| `activity_indicator` | 通用 loading 活动标签。 |

### 4.7 中断与任务级联动

中断卡片是 item 内块，但恢复结果是 task 级事实。

1. `interrupted` 创建可操作卡片。
2. 后续任一同 `task_id` item 收到 `input(event_type=interruption_response)`。
3. `synchronizeTaskItemStates` 扫描所有 item，按 `request_id` 聚合出“用户已重试 / 已同意 / 已拒绝”。
4. 所有同 task 的旧中断卡片投影为已恢复态，隐藏操作按钮。

这样中断前记录仍保留，恢复记录也独立保留，但 UI 不会留下可重复点击的旧审批卡片。

## 5. 详情面板 View Model

### 5.1 DetailSelection

点击聊天块只创建 `DetailSelection`，不修改 `ChatItem`：

```ts
type DetailSelection = {
  id: string;
  title: string;
  role: string;
  taskId: string | null;
  eventGroups: DetailEventGroup[];
};
```

`DetailItem` 保存展示所需的事件名、状态、耗时、胶囊字段与 JSON 数据。其 `data` 必须是对应 SSE event 本身，而非 `metadata` 展开后的重复对象。

### 5.2 complete 与非 complete 的统一边界

只有 `completion` 块代表“查看整个 task 的事件链”。因此规则严格分为两类：

| 点击块 | 事件来源 | 分组与交互 |
| --- | --- | --- |
| `completion` | 当前 `ChatItem.events` 的全部事件，从 input 到 complete | 按历史标记分连续组；多事件条目默认折叠。 |
| 非 `completion` 块 | 块自身 metadata 事件；工具块优先使用 `detailEvents` | 不分组；事件默认展开、不可点击、不显示折叠图标。 |

这条边界避免文本块、历史引用或单个工具块错误展示“历史事件组 + 单事件”。

### 5.3 complete 详情中的历史分组

仅对 complete 详情调用 `groupDetailItemsByInterruptionHistory`：

- 对 `ChatItem.events` 单次遍历。
- 仅当相邻事件的 `is_history_event_for_interruption` 值变化时新建分组。
- 分组内、分组间均保持原始事件顺序，绝不采用 `filter(history) + filter(current)` 重新排序。
- history 组默认折叠；展开后事件左缩进。
- 非 history 组直接展示。

历史组和单事件详情均采用统一的高度抽屉动画：`grid-template-rows 180ms ease`。展开与收起不使用透明度动画。

### 5.4 详情事件行与胶囊

每条详情事件行按如下结构展示：

```text
序号 -> 事件类型代码样式 -> 弹性空白 -> 可选胶囊 -> 可选折叠箭头
```

| 条件 | 右侧胶囊 |
| --- | --- |
| `input` | `event.data.event_type`。 |
| `tool_call`、`tool_result_start` | `event.data.name`。 |
| `interrupted` 且存在 `event.data.params.tool_id` | 工具标识胶囊，复用工具调用胶囊样式。 |
| `context_compression_final` 且 `event.data.level` 为 `0`、`1`、`2` | 等级胶囊，展示为 `L0`、`L1`、`L2`。 |
| `tool_result` 且存在 `event.data.status` | 状态胶囊，成功绿色，失败红色。 |
| 任意事件存在 `event.data.duration_ms` | 耗时胶囊，使用统一 `ms / s / min / h` 格式。 |

当详情只有单个非 complete 事件时，直接显示 JSON 内容，不呈现点击 affordance 或箭头。

### 5.5 JSON 规则

- JSON 以格式化、语法高亮和行号形式展示。
- 容器宽度始终跟随详情面板；长行仅在 `pre` 内横向滚动，不可撑开面板或遮挡工具栏。
- `context_compression_final` 如存在 `data.compressed_mid_term_memory`，在 JSON 下方完整展示“会话记忆”。

## 6. 组件职责边界

| 位置 | 职责 | 不应承担的职责 |
| --- | --- | --- |
| `api.ts` | HTTP/SSE 协议、SSE 帧解析、网络重试和 `last_event_id` 续接。 | 投影聊天块或操作 CSS 状态。 |
| `types.ts` | 协议类型、原始状态类型、聊天块类型。 | 业务转换。 |
| `chatState.ts` | 原始事件归属、任务联动、事件到聊天 View Model 的投影。 | DOM、组件局部展开状态。 |
| `App.tsx` | 页面状态编排、选择详情、调用投影函数、渲染组件。 | 在 JSX 中解释事件字段并拼接业务块。 |
| CSS | 视觉、布局、统一抽屉动画。 | 决定业务状态或事件关系。 |

## 7. 后续扩展流程

新增一种服务端事件或展示能力时，按固定步骤扩展：

1. 在 `ServerMessageType` 与协议类型中声明事件。
2. 明确它属于 item 内状态、task 级联动，还是仅用于详情。
3. 在 `chatState.ts` 的事件投影中新增确定性规则，产出已有块类型或新增明确的块类型。
4. 若需要详情，明确该块的 `detailEvents` 与 JSON 应展示的原始 SSE event。
5. 仅在 View Model 稳定后新增 React 组件和 CSS。
6. 为中断恢复、history 回放、空恢复 complete、未匹配工具结果至少各验证一次。

禁止以下做法：

- 在组件中按 `event.type` 临时分支决定业务状态。
- 为视觉问题修改或删除 `ChatItem.events`。
- 因为同 `task_id` 就跨 item 合并工具块或文本块。
- 用 metadata 的冗余包装对象替代原始 SSE event 作为详情 JSON。
- 通过增加 CSS 特例掩盖 View Model 与实际事件不一致的问题。

## 8. 关键不变量清单

- `ChatItem.events` 始终是原始事件顺序。
- 一个持久化 history record 对应一个 `ChatItem`，即使同 task 有多个 record。
- 跨 item 仅同步同 task 的中断恢复状态。
- 工具匹配仅限单个 item。
- 聊天区的展示顺序可以重组，但详情 complete 必须保持事件原顺序。
- 只有 complete 详情可按 `is_history_event_for_interruption` 分组和折叠。
- 非 complete 详情永远直接展开，不显示折叠箭头。
- 详情 JSON 永远来源于正确的原始 SSE event。
