# turtlemap Observation 最小设计

## 1. 文档目标

本文只回答一个问题：

- 在复杂 Agent 系统中，如何以较低存储成本记录每次 LLM 调用实际看到的上下文材料集合

本文刻意只讨论最小可落地方案，不展开完整 tracing、全链路审计、指标系统和日志平台设计。

## 2. 问题背景

从用户视角看，对话通常是线性累加的：

- 用户发消息
- Agent 回复消息

但对 `Runtime` 而言，真实进入 LLM 的上下文材料往往不是简单聊天记录，而是动态组装结果，例如：

- `System`
- 最近 `History`
- `Memory`
- 结构化任务输入
- 当前任务现场补充消息
- `tool result`
- 异步任务补充说明
- 运行时临时插入的 system / assistant / tool message

这意味着“用户看到的对话”与“LLM 实际收到的上下文”并不总是相同。

如果每次 LLM 调用都完整保存一份全文 prompt，虽然最直接，但会带来几个问题：

- 数据量快速膨胀
- 相同消息被反复存储
- 难以区分哪些消息是复用的，哪些是本轮临时组装的
- 后续做调试、统计和复盘时不够结构化

因此，最小 observation 方案应优先解决两个核心问题：

1. 让每次 LLM 调用能重建出“当时到底看到了哪些上下文材料”
2. 尽量避免重复存储同一份上下文材料全文

## 3. 设计原则

当前最小方案遵循以下原则：

- 上下文材料正文尽量只存一次
- 每次 LLM 调用只写入本次新增的增量事实
- 调用记录只保存本次实际使用的材料 id 有序序列
- 调用看到的材料顺序必须可恢复
- 区分原始输入、结构化任务输入、history、memory 和工具结果等材料类型
- 先解决可重建和可排障问题，再考虑更复杂的性能优化

## 4. 最小模型

当前最小只包含两类核心记录：

- `context_material`
- `llm_call`

其中：

- `context_material` 负责保存可被 LLM 输入引用的上下文材料
- `llm_call` 负责保存一次 LLM 调用记录

### 4.1 context_material

`context_material` 表表示可被 LLM 输入引用的上下文材料实体。

至少包含以下字段：

- `id`：材料主键
- `material_type`：材料类型，例如 `system`、`task_input`、`history`、`memory`、`tool_result`、`tool_schema`
- `material_hash`：材料内容指纹，用于按需去重
- `payload`：材料正文或结构化载荷
- `metadata`：补充结构化信息，例如来源、版本、关联任务、附加标签
- `created_at`：创建时间

这里的关键点是：

- `context_material` 表存的是 LLM 输入材料本体
- 同一份稳定材料不应在每次调用时重复插入
- 原始用户输入和实际给到 LLM 的结构化任务输入可以是两条不同材料，避免后续从原始输入反推 LLM 输入

是否完全依赖 `material_hash` 做全局去重，后续可以再调优；最小阶段先允许“尽量去重”，不强求所有材料必须全局唯一。

### 4.2 llm_call

`llm_call` 表表示一次真实发生的 LLM 调用。

至少包含以下字段：

- `id`：调用主键
- `session_id`：所属会话
- `agent_name`：触发调用的 Agent
- `task_id`：可选任务 id
- `call_type`：调用类型，例如首轮 message、tool continuation、auto response
- `model`：实际使用的模型标识
- `created_at`：调用时间

这张表不负责保存完整 prompt，只负责记录“一次调用发生了”以及最基础的归属信息。

`llm_call` 还应保存本次实际使用的材料 id 序列：

- `input_material_ids`：本次 LLM 调用实际输入材料 id 的有序列表
- `output_material_ids`：本次 LLM 调用产生的输出材料 id 的有序列表

第一阶段可以直接把 id 序列保存在 `llm_call` 表中。采用使用 JSON array；若工程上希望进一步简化，也可以使用逗号分隔字符串，但读取时通常仍由程序解析后再批量查询材料表。

后续如果需要按材料反查所有 LLM 调用、统计材料复用频率，或给每条引用关系挂更多字段，再升级为 `llm_call_material` 关联表。

## 5. 最小查询方式

### 5.1 取某次调用的材料 id 列表

直接读取 `llm_call.input_material_ids` 即可。

```sql
select input_material_ids
from llm_call
where id = $1;
```

如果使用 MySQL JSON array，可以通过 `JSON_TABLE` 在 SQL 层展开并保持顺序：

```sql
select m.*
from llm_call c
join json_table(
  c.input_material_ids,
  '$[*]' columns (
    position for ordinality,
    material_id varchar(128) path '$'
  )
) ids
join context_material m on m.id = ids.material_id
where c.id = ?
order by ids.position;
```

### 5.2 还原某次调用的完整材料内容

程序侧也可以先读取 `input_material_ids`，解析成 id 列表后批量查询：

```sql
select *
from context_material
where id in (...);
```

然后按 `input_material_ids` 的原始顺序重排。

### 5.3 查询本次调用中哪些材料是动态补进来的

动态材料通过 `context_material.material_type` 和 `metadata` 区分，例如 `task_input`、`tool_result`、`memory`、`runtime_patch`。第一阶段不单独建立引用关系表，因此这类筛选默认在程序侧读取材料后完成。

## 6. 与当前 Runtime 的衔接

当前最小方案把 observation 的记录点放在“上下文已经构建完成、模型调用即将发出”这一刻。

也就是说，可以把记录过程理解为：

1. `context_build_provider.build(...)` 产出最终 `messages` 和对应上下文材料
2. observation 层只将本次新增材料标准化与去重入库
3. 创建一条 `llm_call`
4. 将本次实际使用的 `input_material_ids` 写入 `llm_call`
5. 再发起真实模型调用
6. 调用完成后，将 assistant message、tool call 等输出作为新增材料入库，并回写 `output_material_ids`

这样做有两个好处：

- 记录的是 LLM 实际看到的最终输入，而不是中间草稿
- 不要求 `Runtime` 额外保存一份完整 prompt 副本

当前阶段不应把 observation 逻辑混进 `kernel` 核心状态对象中，而应作为独立记录通道挂在模型调用前后。

## 7. 当前阶段刻意不解决的问题

为了保持最小闭环，本文暂不展开以下问题：

- 是否保存每次调用的完整快照副本
- 是否记录 token 级别明细
- 是否记录上下文裁剪前后的差异
- 是否记录每条消息的来源链路详情
- 是否记录模型输出增量流式片段
- 是否引入单独的 `context_assembly` 表
- 是否升级为 `llm_call_material` 关联表

这些能力都可以在当前两类核心记录稳定后继续追加，但不应一开始就拉高设计复杂度。

## 8. 结论

当前最小 observation 方案可以收敛为一句话：

- 用 `context_material` 表存统一上下文材料
- 用 `llm_call` 表存一次调用
- 用 `llm_call.input_material_ids` / `output_material_ids` 保存一次调用实际使用和产生的材料 id 有序序列

这样既能较低成本重建 prompt，又能避免每次重复存全文，也为后续更复杂的 tracing 和观测能力保留了自然扩展空间。
