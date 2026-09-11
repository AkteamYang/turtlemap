# History 版本管理设计

## 1. 文档目标

本文档用于整理对话 `history` 的多分支版本管理方案，作为后续 `os/store`
层持久化演进的独立设计输入。

当前目标聚焦最小可行方案，重点满足以下能力：

- 支持多轮对话历史保存；
- 支持从任意历史消息位置创建新分支；
- 分支之间共享已存在的消息本体，不复制 message；
- 新分支仅增加少量 section 元数据；
- 支持快速恢复指定分支下的完整历史视图；
- 为后续 `Memory`、摘要压缩和上下文快照提供稳定基础。

## 2. 设计结论

本方案采用：

```text
Branch View + Section Snapshot + Shared Message Storage
```

并明确遵守以下最小化约束：

- 不单独引入 `branch` 表；
- 不引入 `sequence_no`；
- 不复制 message；
- 分支通过 `branch_id` 标识；
- section 表示一个历史区块；
- 分叉时复制 section 记录，而不是复制 message；
- `top_message_id` 用于控制某个 section 在当前分支下的可见边界。

## 3. 分层边界

这里需要先明确一个容易混淆的边界：

- 本文档中的 history 分支管理，属于业务层回放能力在 `os/store` 层的落地；
- `kernel` 中的 `BaseAgentState.history` 仍然表示“当前会话、当前 Agent 的运行时 history 列表”；
- 两者是分层协作关系，不是互斥替代关系。

更具体地说：

- `BaseAgentState.history` 负责承接当前会话正在运行时所需的最近稳定历史；
- `kernel.SessionState` 仍然只负责当前会话的最小客观现场，不直接承担完整的历史版本管理语义；
- `branch_id + section` 负责描述“历史上有哪些可回放版本视图，以及每个版本能看到哪些消息”；
- 当业务层需要从某个历史版本回放、重开分支或恢复旧上下文时，可以由
  `os/store` 先基于 `branch -> section -> message` 重建目标历史；
- 随后再把本次真正需要进入运行态的消息列表装配回 `BaseAgentState.history`。

如果业务层需要在会话维度持有诸如 `branch_id` 之类的扩展参数，也更适合由
`os/store` 维护一份 session 业务扩展记录，或维护 `SessionState` 的派生持久化模型，
而不是直接把这些字段并入 `kernel.SessionState`。

因此本文档讨论的不是“用 branch 方案替换 `BaseAgentState.history`”，而是：

```text
业务层历史版本管理
        ↓
os/store 负责回放与恢复
        ↓
kernel.BaseAgentState.history 承接当前轮运行态
```

## 4. 核心模型

整体结构如下：

```text
Conversation
    └── Section
            └── Message
```

其中需要注意：

- `message` 保存真实消息数据；
- `section` 保存某个分支下“哪些历史区块可见，以及可见到哪里”；
- `branch_id` 表示一次完整历史视图；
- `section_id` 表示逻辑历史区块；
- 同一个 `section_id` 可以在多个 `branch_id` 下复用；
- 一个区块版本由 `(branch_id, section_id)` 唯一确定。

## 5. 数据结构设计

### 5.1 message 表

`message` 表用于保存所有实际产生的消息本体。

设计约束：

- message 只保存一份；
- message 创建后原则上 append-only；
- 分支切换或历史分叉不复制 message；
- `section_id` 仅表示该消息所属的逻辑区块，不表示唯一分支归属。

建议结构如下：

```sql
CREATE TABLE message (
    message_id VARCHAR(64) PRIMARY KEY,
    conversation_id VARCHAR(64) NOT NULL,
    section_id VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL,
    content JSON NOT NULL,
    created_at DATETIME NOT NULL
);
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `message_id` | 消息唯一 ID |
| `conversation_id` | 所属会话或对话 ID |
| `section_id` | 消息所属逻辑区块 ID |
| `role` | `user` / `assistant` / `tool` 等消息角色 |
| `content` | 消息内容 |
| `created_at` | 创建时间 |

说明：

- 这里使用 `VARCHAR` 只是表达项目当前统一使用 `*_id` 字符串 ID 的约定；
- 具体长度、JSON 类型和时间字段精度可在 MySQL 实现时再细化。

### 5.2 section 表

`section` 表用于表达某个 `branch_id` 下可见的历史区块快照。

建议结构如下：

```sql
CREATE TABLE section (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    conversation_id VARCHAR(64) NOT NULL,
    branch_id VARCHAR(64) NOT NULL,
    section_id VARCHAR(64) NOT NULL,
    top_message_id VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,

    UNIQUE(branch_id, section_id)
);
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `id` | 数据库记录主键 |
| `conversation_id` | 所属会话或对话 ID |
| `branch_id` | 历史版本 ID |
| `section_id` | 逻辑区块 ID |
| `top_message_id` | 当前分支下该区块最大可见消息 ID |
| `created_at` | 创建时间 |

关键语义：

- `section_id` 表示逻辑区块身份；
- `branch_id` 表示一条完整历史视图；
- `top_message_id` 表示这个区块在当前分支中的历史截断边界；
- 同一个逻辑区块可以在多个分支中复用，但不同分支可拥有不同的 `top_message_id`。

## 6. 字段关系说明

### 6.1 section_id 的含义

`section_id` 表示一个逻辑历史区块。

例如：

```text
section_id = section_x
```

它可以同时出现在多个分支中：

```text
branch_a + section_x
branch_b + section_x
```

这表示两个分支共享这一段历史区块对应的 message 集合。

### 6.2 branch_id 的含义

`branch_id` 表示一次完整的历史版本视图。

例如：

```text
branch_a:
section_1
section_2
section_3

branch_b:
section_1
section_2
section_3
section_4
```

可以把 `branch_id` 理解为“当前恢复 history 时所选择的版本入口”。

### 6.3 top_message_id 的含义

`top_message_id` 用于控制某个 section 在当前 branch 下的可见消息上界。

例如，原始分支中：

```text
section_3
message_201 ~ message_300
top_message_id = message_300
```

如果用户回退到 `message_250` 创建新分支，则新分支里的同一逻辑区块变为：

```text
section_3
message_201 ~ message_250
top_message_id = message_250
```

因此 `top_message_id` 是分叉截断的核心控制点。

## 7. 正常追加消息流程

假设当前历史如下：

```text
branch_a
└── section_1
    ├── message_1
    ├── message_2
    └── message_3
```

继续产生：

```text
message_4
message_5
```

如果它们仍属于当前 section，则：

- 新消息写入 `message` 表，且 `section_id = section_1`；
- 更新 `section.top_message_id = message_5`。

如果需要开启新的历史区块，则：

1. 新增一条 section 记录：

```text
branch_id = branch_a
section_id = section_2
top_message_id = message_5
```

2. 后续新消息写入 `message` 表时统一挂到 `section_2`。

## 8. 分叉流程设计

假设当前分支 `branch_a` 下有以下历史：

```text
section_1: message_1 ~ message_100
section_2: message_101 ~ message_200
section_3: message_201 ~ message_300
```

用户从 `message_250` 回退并创建新分支 `branch_b`。

### 8.1 查询当前分支的 section 视图

```sql
SELECT *
FROM section
WHERE conversation_id = :conversation_id
  AND branch_id = :branch_id
ORDER BY top_message_id;
```

得到：

```text
section_1 top = message_100
section_2 top = message_200
section_3 top = message_300
```

### 8.2 定位目标消息所属 section

```sql
SELECT section_id
FROM message
WHERE conversation_id = :conversation_id
  AND message_id = :message_id;
```

得到：

```text
section_3
```

### 8.3 复制历史 section 记录

把原分支中截止目标 section 为止的可见区块复制到新分支：

```text
branch_a:
section_1
section_2
section_3

复制后 branch_b:
section_1
section_2
section_3
```

这里复制的是 `section` 元数据记录，不复制 `message` 本体。

### 8.4 截断最后一个 section

在新分支下，把目标区块的 `top_message_id` 从原值：

```text
message_300
```

改为：

```text
message_250
```

最终结果如下：

```text
branch_a:
section_1 -> message_1 ~ message_100
section_2 -> message_101 ~ message_200
section_3 -> message_201 ~ message_300

branch_b:
section_1 -> message_1 ~ message_100
section_2 -> message_101 ~ message_200
section_3 -> message_201 ~ message_250
```

此时 message 数据仍然只有一份。

## 9. 查询指定 branch 的完整历史

当外部输入 `branch_id` 后，恢复流程分两步。

### 9.1 查询当前分支的 section 列表

```sql
SELECT *
FROM section
WHERE conversation_id = :conversation_id
  AND branch_id = :branch_id
ORDER BY top_message_id;
```

例如得到：

```text
section_1 top = message_100
section_2 top = message_200
section_3 top = message_250
```

### 9.2 依据 section 可见边界查询 message

```sql
SELECT m.*
FROM message m
JOIN section s
  ON m.conversation_id = s.conversation_id
 AND m.section_id = s.section_id
WHERE s.conversation_id = :conversation_id
  AND s.branch_id = :branch_id
  AND m.message_id <= s.top_message_id
ORDER BY m.message_id;
```

得到：

```text
message_1
message_2
...
message_250
```

说明：

- 这里默认 `message_id` 可承载单调有序比较语义；
- 如果后续项目继续统一使用字符串 UUID 风格 `message_id`，则应增加独立的
  可排序字段，例如 `message_seq` 或 `created_at + 局部稳定顺序`；
- 也就是说，“不引入 `sequence_no`”的约束是针对 `section` 排序，不代表
  `message` 层完全不能拥有独立的有序字段。

这个点在真正落库实现时需要单独收敛，否则 SQL 中的 `<= top_message_id`
无法稳定表达历史边界。

## 10. 索引设计

### 10.1 message 表索引

核心查询维度为：

- `conversation_id`
- `section_id`
- `message_id`

建议索引：

```sql
CREATE INDEX idx_message_conversation_section_message
ON message(conversation_id, section_id, message_id);
```

用于快速定位某个 section 下的消息范围。

### 10.2 section 表索引

核心查询维度为：

- `conversation_id`
- `branch_id`

建议索引：

```sql
CREATE INDEX idx_section_conversation_branch
ON section(conversation_id, branch_id);
```

## 11. 分叉事务要求

分叉必须在同一事务中完成，避免出现半完成状态。

建议流程：

```text
BEGIN TRANSACTION

1. 查询当前 branch 的 section 视图
2. 生成新的 branch_id
3. 复制需要继承的 section 记录
4. 修改目标 section 的 top_message_id
5. COMMIT
```

事务目标：

- 保证分叉期间历史视图一致；
- 保证新分支要么完整创建成功，要么完全不可见；
- 避免出现“已复制部分 section 但最后边界未截断”的中间态。

## 12. 方案优势

### 11.1 不复制历史消息

如果存在：

```text
100 万条 message
10 个 branch
```

则理想情况下仍然只需保存：

- 100 万条 message 本体；
- 少量 section 元数据记录。

因此存储成本主要与消息规模线性相关，而不是与分支数乘法膨胀。

### 11.2 分叉成本低

分叉只需要复制 section 元数据，复杂度主要取决于 section 数量：

```text
O(section 数量)
```

而不是：

```text
O(message 数量)
```

### 11.3 查询路径简单

该方案不需要引入：

- message tree；
- `parent_message_id` 递归遍历；
- DAG 版本回溯。

恢复路径固定为：

```text
branch_id
    ↓
section
    ↓
message
```

这更容易落地到关系型数据库，并且更适合后续 `StateStore` 封装。

## 13. 后续扩展方向

`section` 可以自然承接更多历史治理能力，例如：

```sql
section
----------------
conversation_id
branch_id
section_id
top_message_id
summary_id
embedding_id
```

这样可以进一步支持：

- 区块级历史摘要；
- 长期 Memory 沉淀；
- Context Snapshot；
- RAG 证据绑定；
- 后台压缩任务与历史区块的覆盖关系追踪。

例如：

```text
section_1
message_1 ~ message_1000
summary_a

section_2
message_1001 ~ message_1200
summary_b
```

后续构建上下文时，可以按需组合：

```text
summary
+ recent history window
```

而不必总是回放所有原始消息。

## 14. 与当前 SDK 状态设计的关系

当前 SDK 状态存储已经收敛为：

- `BaseAgentState.history` 随 `BaseSessionState` 整体保存；
- SDK 不内置 `message` 表或 `agent_history_message` 顺序索引表；
- MySQL 等持久化实现位于业务层示例或业务项目中。

本文档描述的是未来 history 版本管理方案，但它并不否定当前
`kernel.BaseAgentState.history` 的职责。

更准确地说，二者关注点不同：

- `BaseAgentState.history` 负责表达当前会话当前 Agent 的运行态 history；
- `BaseSessionState` 负责表达当前 runtime 恢复所需的最小会话现场；
- 当前 `StateStoreProtocol` 主要围绕完整 SessionState checkpoint 保存；
- 本文档方案可在业务层补充 `branch_id + section` 的历史版本视图；
- 若业务上需要 `branch_id` 这类会话级扩展参数，也应优先落在 `os` 层的
  session 扩展记录，而不是直接侵入 kernel 基础模型；
- 这层能力主要服务“业务侧历史回放、回退分叉、旧版本重开”等需求。

因此它更适合作为当前最小可运行实现之上的增强层，而不是要求立即替换
`BaseAgentState.history` 或否定 kernel 现有快照语义。

如果未来要从当前原型平滑迁移到 section 方案，建议优先解决以下问题：

1. 明确 `conversation_id`、`branch_id`、`section_id` 与当前 `session_id`、
   `agent_name`、`message_id` 的映射关系；
2. 明确 message 的稳定有序字段，保证 `top_message_id` 可可靠表达截断边界；
3. 明确 section 的切分时机，例如按轮次、按压缩边界、按 checkpoint 或按人工分叉点；
4. 明确多 Agent 会话下 history version manager 的归属粒度，是 owner agent 级，
   还是 session 级共享历史视图。

## 15. 实现约束清单

实现时应默认遵守以下约束：

1. 不复制 message 数据；
2. `section_id` 是逻辑区块 ID，不是 branch 唯一 ID；
3. `(branch_id, section_id)` 必须唯一；
4. 不单独建立 `branch` 表；
5. 分叉时复制 section 记录；
6. 分叉时修改最后一个 section 的 `top_message_id`；
7. message 默认 append-only；
8. 查询 history 时必须通过 `branch -> section -> message` 恢复；
9. `section` 顺序通过历史边界判断，不额外引入 `sequence_no`；
10. 真正落库前必须先收敛 message 的稳定排序语义。

## 16. 小结

这套方案保留了以下平衡：

- 结构足够简单，适合关系型数据库实现；
- 分支能力完整，支持从任意历史位置回退再继续；
- 不复制 message，本体存储成本低；
- 不与 `kernel.BaseAgentState.history` 的当前态职责冲突；
- 为后续摘要、记忆、压缩与上下文快照留出了自然扩展位。

如果后续要在 `Agent` 会话中引入真正的 history version manager，这份设计可以作为相对稳定的第二阶段基础方案。
