# Task Context Engineering 设计

## 1. 文档目标

本文用于整理 `task context engineering` 的核心理念和边界。

这里的 `task context engineering` 指的是：系统不应把用户输入简单转换成一条 `message` 后直接交给 LLM，而应在任务创建和推进阶段，将任务目标、输入、约束、上下文、样例、输出要求等信息整理成任务级上下文，再渲染为 LLM 可消费的输入。

本文只讨论任务级上下文的必要性、职责划分和未来模型方向，不展开具体存储表设计、token 预算策略和完整 prompt 模板。

## 2. 当前问题

当前最小实现中，用户输入通常会直接转换成 `LLMMessage`。

这种方式有一个好处：链路简单，容易跑通最小闭环。

但它也有明显不足：

- 原始输入只表达“用户说了什么”，不一定表达“系统应该如何完成任务”。
- 任务约束、上下文、输出要求和示例容易散落在 history、memory、system prompt 或自然语言补丁里。
- LLM 实际需要的信息和用户原始输入之间缺少稳定结构，后续难以观测、复盘和优化。
- 当任务跨多轮、跨工具、跨 Agent 推进时，单条 message 很难承载稳定任务现场。
- 系统很难区分“当前任务目标”和“对话里的普通事实”。

这会导致一个常见问题：LLM 不是没有能力完成任务，而是系统没有把任务以足够清晰、完整、稳定的方式传达给它。

## 3. 核心判断

任务执行不应只依赖聊天消息，而应有任务级上下文。

一个更接近真实协作的类比是：当我们让别人做一件事时，通常不会只说一句请求，还会补充：

- 要完成什么目标
- 哪些事项必须注意
- 有哪些背景材料
- 参考哪些例子
- 结果应该长什么样
- 什么情况需要追问
- 什么情况算完成

这些“叮嘱”不是闲聊内容，而是任务执行质量的重要组成部分。

因此，`task context engineering` 的目标不是把 prompt 写得更长，而是把任务信息结构化，让 LLM 在执行前获得稳定、明确、可追踪的任务说明。

## 4. 与现有概念的关系

### 4.1 Raw Input

`Raw Input` 表示外部进入系统的原始事实，例如用户输入、环境事件、工具回调事件。

它回答的问题是：

- 外部实际发生了什么？

约束：

- Raw Input 应保持原始性和可追溯性。
- Raw Input 不应直接承担任务说明的全部职责。
- Raw Input 可以被任务理解流程引用，但不应被覆盖或改写。

### 4.2 Task Context

`Task Context` 表示系统为了完成某个任务而整理出的结构化任务上下文。

它回答的问题是：

- 当前任务到底要做什么？
- 完成任务需要遵守哪些边界？
- 哪些背景材料对当前任务有效？
- 结果应如何组织？

Task Context 是对 Raw Input 的任务化整理，不等于 Raw Input 本身。

### 4.3 History

`History` 表示已经稳定发生过的运行产物，例如用户输入、assistant 输出、工具调用和工具结果。

它回答的问题是：

- 过去已经发生了什么？

History 可以为 Task Context 提供事实依据，但 History 本身不应直接等价为任务说明。

### 4.4 Memory

`Memory` 表示跨轮次或跨会话沉淀出的稳定背景、偏好、约束和阶段性摘要。

它回答的问题是：

- 哪些长期或中期信息对当前任务有帮助？

Memory 可以被 Task Context 引用或摘要进入任务说明，但不应无差别塞入当前任务输入。

### 4.5 LLM Input

`LLM Input` 是最终发送给模型的消息序列。

它回答的问题是：

- 模型这一次实际看到了什么？

Task Context 最终会被渲染成 LLM Input 的一部分，但 Task Context 的结构化模型和最终 LLMMessage 不应混为一谈。

## 5. 任务级上下文的建议划分

未来可以将任务级上下文拆成以下几个稳定部分。

### 5.1 task_input

`task_input` 表示当前任务的核心输入。

它通常来自 Raw Input，但会被整理成更适合任务执行的表达。

示例：

```text
用户希望查询北京今天的天气，并获得简短中文回答。
```

### 5.2 objective

`objective` 表示当前任务目标。

它应尽量表达“完成什么”，而不是重复用户原话。

示例：

```text
查询指定城市的天气，并向用户返回可读的天气摘要。
```

### 5.3 constraints

`constraints` 表示任务必须遵守的约束。

约束可以来自系统定义、用户偏好、工具能力边界、业务规则或当前任务补充说明。

示例：

```text
- 天气数据必须来自工具结果。
- 如果城市缺失，先追问城市。
- 不要编造实时天气。
```

### 5.4 context

`context` 表示对当前任务有帮助的背景材料。

它可以来自 history、memory、检索结果、工具前置结果或业务系统状态。

约束：

- context 应与当前任务相关。
- context 不应无差别复制完整 history。
- context 应保留来源或材料 id，便于 observation 回放。

### 5.5 examples

`examples` 表示当前任务可参考的输入输出样例或格式样例。

它的作用不是补事实，而是帮助模型稳定执行方式。

示例：

```text
输入：查询上海天气
输出：上海当前晴，26 摄氏度，东风 2 级。
```

### 5.6 output_format

`output_format` 表示期望结果形式。

它可以是自然语言要求，也可以是结构化模型要求。

示例：

```text
用中文给出一句简短回答。
```

### 5.7 success_criteria

`success_criteria` 表示任务完成判断标准。

它帮助模型判断什么时候可以结束，什么时候需要继续追问、调用工具或说明失败。

示例：

```text
- 已获得城市天气数据。
- 回答中包含天气、温度和风力。
- 若无法获得数据，应说明原因。
```

## 6. 职责边界

### 6.1 kernel

kernel 不应理解 Task Context 的具体字段。

kernel 只需要知道：

- 当前有一个 `BaseProcessingTask`
- 任务有稳定起点输入 `start_input`
- 任务状态可以推进、挂起、恢复和完成
- 任务最终产生 `RuntimeArtifact`

Task Context 的构建、解释、渲染和观测都应放在 os 层。

### 6.2 os

os 层负责 Task Context Engineering。

具体包括：

- 从 Raw Input 创建任务级上下文
- 从 history、memory、工具结果中选择与任务相关的材料
- 合并系统约束、用户偏好和任务约束
- 将结构化 Task Context 渲染成 LLMMessage
- 将 Task Context 作为 context material 落库，用于 observation
- 在任务推进过程中更新或补充任务级上下文

### 6.3 ContextBuildProvider

`ContextBuildProvider` 不应只做“消息拼接器”。

未来它应逐步承担：

- task context material 的选择
- task context 到 LLMMessage 的渲染
- token 预算下的 task context 裁剪
- LLM 输入材料 id 序列的生成

### 6.4 Observation

Observation 不应从原始输入反推 LLM 输入。

Task Context 被渲染给 LLM 前，应作为统一上下文材料保存，并在 `llm_call.input_material_ids` 中记录其 id。

这样可以稳定回答：

- 本次 LLM 调用用了哪个任务输入？
- 哪些约束进入了模型上下文？
- 哪些 history 和 memory 被引用？
- 模型看到的任务说明与用户原始输入有什么差异？

## 7. 最小落地路径

第一阶段不需要一次性实现完整任务理解系统。

建议按以下路径演进：

1. 保留当前 Raw Input 到 LLMMessage 的最小链路。
2. 增加一个 os 层任务上下文模型，例如 `TaskContext`。
3. 创建新任务时，从 `BaseProcessingTask.start_input` 生成最小 `TaskContext`。
4. `TaskContext` 先只包含 `task_input`、`objective`、`constraints`、`output_format`。
5. `ContextBuildProvider` 将 `TaskContext` 渲染成一段明确的 task block。
6. 将 `TaskContext` 作为 `context_material(type=task_input)` 落库。
7. `llm_call.input_material_ids` 记录本次调用实际使用的 Task Context、history、memory 等材料 id。

这样可以先形成“任务级上下文可观测”的闭环，再逐步引入更复杂的 context、examples、success criteria 和任务理解能力。

## 8. 关键约束

- Task Context 是任务说明，不是原始输入副本。
- Task Context 应保持结构化，最终再渲染为 LLMMessage。
- Task Context 不应进入 kernel 核心模型。
- Task Context 应作为 observation 可追踪材料。
- History 和 Memory 是 Task Context 的材料来源，不是 Task Context 本身。
- LLM Input 是 Task Context 的渲染结果，不是 Task Context 的唯一存储形态。
- 第一阶段可以先做显式规则整理，不需要依赖模型自动理解任务。

## 9. 结论

`task context engineering` 的核心结论是：

- Agent 系统不应只把用户输入当作聊天消息传给 LLM。
- 系统应在任务级别整理输入、目标、约束、上下文、样例和完成标准。
- kernel 负责任务现场和状态推进，os 负责任务级上下文工程。
- Task Context 应作为统一上下文材料进入 observation，LLM 调用只记录实际使用的材料 id 序列。

这能让 LLM 获得更可靠的信息传达，也能让系统稳定追踪“模型到底为什么这样回答”。
