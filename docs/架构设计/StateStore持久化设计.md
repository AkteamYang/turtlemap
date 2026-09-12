# StateStore 持久化设计

## 0. 当前实现口径

本文档以当前代码为准时，需要先明确以下口径：

- SDK 只定义 `StateStoreProtocol`、默认 `InMemoryStateStore` 和运行期状态模型。
- MySQL StateStore 已移动到业务层示例目录 `examples/mysql_store`，不再作为 SDK 内置实现。
- `BaseAgentState` 不再单独序列化为独立表，而是跟随 `BaseSessionState` 整体持久化。
- SDK 不再维护独立 `message` 表、`agent_history_message` 表、`history_ids` 或 `save_history_messages` 链路。
- 面向前端展示、搜索或审计的会话历史，应由应用层根据 Runtime 输出或 event bus 自行落库。
- `branch_id` 暂不进入 SDK 当前实现；如业务需要分支，应由业务层 SessionState 子类或业务存储自行扩展。
- `version`、`schema_version` 属于业务层持久化状态字段，当前 MySQL 示例通过 `MySQLSessionState` 承载。

## 1. 文档目标

本文档用于整理 `StateStoreProtocol` 的职责边界、状态保存语义，以及后续 MySQL 版本状态存储的设计方向。

当前讨论先聚焦在最小运行闭环所需的状态持久化，不急于一次性敲定所有序列化细节。

本文档重点回答以下问题：

- `SessionState` 和 `BaseAgentState` 分别保存什么
- BaseRuntime checkpoint 的提交顺序是什么
- 为什么当前不再单独保存 `BaseAgentState`
- MySQL 示例大致需要哪些表
- 哪些序列化细节暂时保持待定

## 2. StateStoreProtocol 的定位

`StateStoreProtocol` 是 os 层 `OSService` 持有的状态存储边界。

它不负责业务推理，也不负责上下文压缩策略；它只负责让 os 层能够可靠完成以下动作：

- 保存新的会话 checkpoint
- 加载跨会话的长期记忆主数据
- 承接 checkpoint 所需的完整 SessionState 状态写入
- 承接上下文压缩任务创建与完成状态更新

也就是说，`StateStoreProtocol` 是 runtime 可恢复性的底层协议。

## 3. store 目录边界

SDK 内部 `os/store` 只保留通用存储协议依赖的默认能力，不绑定具体数据库：

```text
src/turtlemap/os/store/
├── __init__.py
└── memory.py          # 内存态 StateStore 实现
```

MySQL 示例实现位于业务层 `examples/mysql_store`：

```text
examples/mysql_store/
├── __init__.py
├── driver.py
├── models.py          # MySQL persistence model
├── repository.py      # 表级 SQL 读写与事务封装
├── serializer.py      # runtime model 与存储结构之间的序列化/反序列化
├── state_store.py     # StateStoreProtocol 适配实现
└── migrations/
    └── 001_init_state_store.sql
```

这里需要明确：

- `kernel.models` 中的 `BaseSessionState`、`BaseAgentState`、`BaseProcessingTask` 等是 runtime model
- `examples/mysql_store/models.py` 中的表结构对象是 MySQL 示例 persistence model
- persistence model 不应直接复用 runtime model
- runtime model 和 persistence model 之间通过 serializer / mapper 转换

这样设计的原因是：

- runtime model 优先服务运行时表达和状态推进
- persistence model 优先服务数据库约束、索引、查询和事务
- 两者变化节奏不同，不应互相绑死
- MySQL 表可以按业务需要拆分，但 SDK runtime 不直接感知这些表

例如：

- BaseRuntime 中的 `BaseAgentState.history` 是 `list[RuntimeArtifact]`
- 当前 MySQL 示例将 `BaseAgentState.history` 直接随 SessionState 快照保存
- 后台压缩任务不属于 kernel runtime model，应由 os context 中的 `ContextCompressionTaskRecord` 表达
- MySQL 中可以用 `context_compression_task` 表保存压缩任务记录，例如 `lease_owner`、`lease_expires_at`

`StateStoreProtocol` 的实现负责隐藏这些差异。

当前新的数据库持久化示例应优先放在业务层或 examples 中；SDK 内部只保留协议和默认内存实现。

从 kernel 视角看，它只加载和保存 runtime model；从 os/store 视角看，它可以自由选择最适合数据库的持久化模型。

## 4. 状态分层

### 4.1 SessionState

`SessionState` 表示会话客观现场。

它更像一张“会话索引表”或“会话控制面快照”，主要记录：

- 当前 `session_id`
- 当前会话涉及哪些 Agent
- 当前 Agent 控制权栈
- 当前输入队列
- 当前会话可恢复的 AgentState 映射

其中最关键的是：

```python
agent_name2agent_state: dict[str, BaseAgentState]
```

`BaseAgentState` 当前不再独立建表保存，而是跟随 `BaseSessionState` 整体
序列化。加载会话后，`OSService.load_session_state(...)` 会根据当前代码中的
Agent 图补齐缺失的 AgentState，并将 AgentState.system 更新为当前代码配置。

这里还需要进一步明确一层边界：

- `kernel.SessionState` 只负责表达 runtime 闭环所需的最小会话客观现场；
- 一些明显偏业务层、回放层或产品层的会话扩展参数，不应直接回灌到
  `kernel.SessionState` 基础模型；
- 这类信息适合由 `os/store` 维护“会话业务派生模型”或“会话扩展记录”。

例如在 history version manager 场景中，以下字段都适合放在 `os` 层扩展里：

- `branch_id`
- 当前选中的历史回放版本
- 当前默认 section 切分策略
- 历史回放入口点、重开来源或产品侧标签

也就是说，后续如果业务上需要“带 branch 语义的 session state”，采用：

```text
kernel.SessionState
    +
os 层 Session 业务扩展记录
```

而不是直接把 `branch_id` 等字段并入 kernel 的最小会话模型。

这样做的原因是：

- `kernel.SessionState` 应保持稳定、通用、可复用；
- `branch_id` 属于 history version manager 的业务语义，不是所有 runtime 都需要；
- 不同产品形态下，会话级业务字段可能继续膨胀，若直接并入 kernel 会让基础模型失稳；
- `StateStore` 与 serializer 本来就负责处理 runtime model 和 persistence model 之间的差异，天然适合承接这一层扩展。

### 4.2 BaseAgentState

`BaseAgentState` 表示某个 Agent 在当前会话中的主观状态。

它主要记录：

- 当前 Agent 名称
- 当前 Agent 的 system 快照
- 当前 Agent 的 history 运行时消息列表
- 当前 Agent 的 processing_tasks
- 当前 Agent 的 memory 视图

`BaseAgentState` 是真正承载 Agent 主观运行状态的地方。

当前实现中，它不会单独保存多个版本，也不维护 `history_ids`。`history`
中的元素是 `RuntimeArtifact`，具体 payload 类型由 os 层在构建上下文或执行工具时
按 artifact type 收窄处理。

面向产品展示的消息历史不再由 SDK 存储层自动从 `history` 拆出保存。应用层如果需要
会话列表、搜索、审计或观测事实表，应根据 Runtime 输出或 event bus 事件单独落库。

### 4.3 长期记忆主数据

`memory.long_term_memory` 会随 `BaseAgentState` 一起保存，用于 checkpoint、回放和 observation 溯源。

但长期记忆的权威来源不是历史 BaseAgentState 快照，而是 StateStore 中独立维护的长期记忆主数据。

因此 `BaseRuntime` 启动时应：

1. 根据 SessionState 找到需要加载的 BaseAgentState 快照
2. 加载 BaseAgentState 快照
3. 再从 StateStore 加载当前生效的长期记忆
4. 用当前生效长期记忆覆盖运行态里的 `memory.long_term_memory`

这样可以同时满足：

- checkpoint 可回放
- observation 可溯源
- 长期记忆跨会话生效

## 5. Checkpoint 提交顺序

当前 BaseRuntime checkpoint 的核心流程应是：

1. kernel 推进内存态 `BaseSessionState` 和其中的 `BaseAgentState`
2. os Runtime 调用 `OSService.save_session_state(...)`
3. 具体 StateStore 实现保存完整 SessionState 快照
4. 若业务层状态模型带有乐观锁版本，则由业务层 StateStore 保存成功后回写

也就是：

```text
BaseRuntime updates in-memory SessionState
        ↓
OSService.save_session_state(save_kind)
        ↓
StateStore saves full SessionState snapshot
```

因为 `BaseAgentState` 已随 SessionState 保存，当前实现不再存在“先保存 AgentState、
再保存 SessionState 引用版本”的两阶段提交问题。

## 6. 原子性与一致性

理想情况下，一个 checkpoint 应在数据库事务中完成 SessionState 快照写入。

如果第一阶段为了实现简单暂时不做强事务，也至少要保证：

- 不要静默吞掉保存失败；
- 若业务层使用乐观锁，应明确处理并发更新；
- 恢复时若发现状态无法反序列化，应进入明确的业务兜底逻辑。

## 7. MySQL 表设计方向

### 7.1 session_state 表

`session_state` 表保存业务层 MySQL 示例的会话级快照。

字段定义：

| 字段 | 说明 |
| --- | --- |
| `id` | 持久化记录自增主键 |
| `session_id` | 会话 id |
| `schema_version` | SessionState 数据结构版本 |
| `version` | SessionState 乐观锁版本 |
| `state_json` | SessionState 序列化内容 |
| `save_kind` | 保存语义，例如 checkpoint / background_context_compression |
| `created_at` | 创建时间 |

索引定义：

```text
primary key(id)
unique(session_id, schema_version, version)
index(session_id, schema_version, version, id)
```

当前 MySQL 示例中 `version` 是同一 `session_id + schema_version` 下的乐观锁版本。
保存时先按调用方持有版本更新；如果更新失败，再读取最新版本并追加一条
`latest_version + 1` 的记录。并发插入相同版本时依赖唯一键触发异常，再由重试机制处理。

### 7.2 agent_state 表

当前实现不再需要 `agent_state` 表。

`BaseAgentState` 直接位于 `BaseSessionState.agent_name2agent_state` 中，随
`session_state.state_json` 一起保存。业务层如果后续为了查询、审计或分析需要拆分
AgentState，可以在自己的 persistence model 中扩展，但不属于 SDK 当前默认约束。

### 7.5 agent_long_term_memory 表

长期记忆作为跨会话主数据，应单独保存。

字段定义：

| 字段 | 说明 |
| --- | --- |
| `uid` | 用户唯一标识 |
| `memory_text` | 当前生效长期记忆文本 |
| `version` | 长期记忆版本，MySQL 自增主键 |
| `updated_at` | 更新时间 |

长期记忆是用户维度的跨会话主数据，不按 Agent 维度隔离。

第一阶段可以通过 `uid` 加载最新版本作为当前生效版本。

如果后续需要 observation 或审计，可以再增加长期记忆历史表。

### 7.6 context_compression_task 表

后台压缩任务不应挂在 `SessionState` JSON 中，MySQL 版本应单独建表。

原因是：

- 压缩任务需要跨进程创建
- 创建需要条件更新或唯一约束
- 任务状态更新频率和 SessionState checkpoint 不完全一致
- 单独建表更容易做租约过期、失败标记和观测

字段定义：

| 字段 | 说明 |
| --- | --- |
| `id` | 压缩任务自增主键 |
| `session_id` | 会话 id |
| `agent_name` | Agent 名称 |
| `status` | running / completed / failed |
| `running_task_key` | 仅 running 状态生成的唯一槽位 key，非 running 为 NULL |
| `lease_owner` | 当前租约持有者 |
| `lease_expires_at` | 租约过期时间 |
| `error` | 失败原因 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

同一 `session_id + agent_name` 未过期 running 任务最多一条。

当前 MySQL 实现使用生成列 `running_task_key` 加唯一键表达该约束：

- `status = running` 时，`running_task_key = session_id-agent_name`
- 非 running 状态时，`running_task_key = NULL`
- unique key 建立在 `running_task_key` 上

MySQL unique 允许多条 `NULL`，所以 `completed` / `failed` 历史记录不会阻止后续新任务插入；只有 running 任务会占用唯一槽位。修改 `status` 时生成列会自动重算，任务完成后槽位自然释放。

当前实现创建任务时会在事务中 `SELECT ... FOR UPDATE` 检查 running 任务：
若已有未过期租约则返回 `None`；若租约已过期则刷新 `lease_owner` 和
`lease_expires_at` 并返回原任务；若不存在 running 任务则插入新任务。
`lease_owner` 由 `shared.ids.build_executor_instance_id()` 生成，格式为
`hostname:短 uuid`，表示一次具体执行尝试，而不是应用全局实例。

## 8. 序列化边界

当前序列化细节尚未最终敲定。

第一阶段可以先明确原则：

- `kernel` 层继续保持纯 Python 运行态对象
- 业务层存储实现可定义独立 persistence model，不直接复用 runtime model
- 示例 `examples/mysql_store/serializer.py` 负责把 runtime 状态转换为可存储结构
- MySQL 表中的 `state_json` 暂时可以保存完整 JSON 快照
- MySQL 查询行通过 `DictCursor` 以字典方式取值，再直接交给 persistence model `model_validate`
- 反序列化时依赖 `PolymorphicStateModel` 的 `type_name` 恢复具体子类
- 不应让 MySQL 表结构过早侵入 kernel 状态对象设计

后续需要单独敲定的问题包括：

- dataclass 如何统一转 JSON
- 新增 RuntimeArtifact payload 类型后如何注册和恢复
- `ObservableEvent.payload` 等开放载荷如何按业务类型恢复
- 枚举是否统一存储为字符串
- 观测事实表、会话展示历史与 Runtime 状态快照之间的边界

当前先采用“完整快照 JSON + 多态 type_name 恢复”的方式推进 MySQL 示例版本。

等运行闭环稳定后，再根据查询、观测和数据分析需求拆细表。

## 9. StateStoreProtocol 后续细化方向

当前 `StateStoreProtocol` 仍偏最小原型。

后续可以考虑调整为更贴近 checkpoint 语义的接口：

```python
async def save_session_state(
    session_state: BaseSessionState,
    save_kind: SessionStateSaveKind,
) -> None:
    ...

async def load_agent_long_term_memory(
    session_state: BaseSessionState,
    agent_name: str,
) -> str:
    ...

async def save_context_compression_result(
    session_state: BaseSessionState,
    agent_name: str,
    compression_result: ContextCompressionResult,
) -> None:
    ...
```

这里的重点是：

- `save_session_state(...)` 保存完整 SessionState 快照
- `BaseRuntime` 不需要关心 MySQL 事务细节，存储细节由 os `Runtime` / `OSService` 承接
- 后台压缩结果由 `save_context_compression_result(...)` 读取最新状态并尝试安全合并
- 业务层如需加载会话，应在 Runtime 外部调用自己的存储实现，再把 SessionState 传入 `Runtime.run(...)`

更理想的 checkpoint API 是：os 层交给 store 一份完整状态，store 在事务内完成版本推进并回写业务层版本字段。

这样可以避免 `BaseRuntime` 自己管理太多持久化顺序细节。

## 10. 输入入队保存语义

当前 `SessionStateSaveKind.INPUT_ENQUEUE` 表示输入进入系统时的持久化。

它和 checkpoint 的区别是：

- 输入入队只更新 `SessionState.input_queue`
- 输入入队不保存新的 BaseAgentState 快照
- 输入入队需要保证新输入不丢
- 输入入队应基于最近稳定 checkpoint 的 SessionState 保存

MySQL 版本可以有两种实现：

1. 继续把 input_queue 放在 SessionState JSON 中，每次入队保存一个新的 SessionState 版本。
2. 单独建立 input_queue 表，将输入事件作为 append-only 记录保存。

第一阶段继续使用 SessionState JSON，保持实现简单。

如果后续输入并发、重放和观测要求提升，再考虑拆表。

## 11. 当前阶段状态

当前 StateStore 相关代码已经完成以下收敛：

1. SDK 默认使用 `InMemoryStateStore`，MySQL 需要业务层显式传入。
2. MySQL 示例位于 `examples/mysql_store`，不再位于 `src/turtlemap/os/store/mysql`。
3. `BaseAgentState` 不再独立建表或维护独立版本，直接随 SessionState 快照保存。
4. SDK 不提供 `save_history_messages` / `load_agent_history_messages` 链路。
5. 会话展示历史、搜索和审计应由应用层根据 Runtime 输出或 event bus 自行落库。
6. 后台压缩任务仍通过 StateStore 管理，并通过 `lease_owner` 支持跨进程 running 任务去重。
7. MySQL 示例使用 `MySQLSessionState.version` 作为乐观锁版本，使用 `schema_version` 区分业务快照结构版本。

下一阶段更值得推进的是 observation 事实表、长期记忆主数据更新入口，以及
业务层会话展示历史与 SDK Runtime 状态之间的清晰分层。
