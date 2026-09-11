# 服务端接口与 SSE 事件投影设计

## 1. 设计目标

本文档用于约束 TurtleMap 示例服务端对外暴露的 HTTP 接口、SSE 流式协议、会话历史查询以及运行中断恢复边界。

服务端接口不直接暴露 `RuntimeEvent`、`SessionState` 等内部模型，而是将最终事件投影为前端稳定消费的消息事件。这样可以保持三层边界清晰：

- `RuntimeEvent`：os 层运行时观测事件，面向系统内部。
- `RuntimeRunResult`：一次 `Runtime.run(...)` 返回的稳定输出，面向业务服务层。
- `ServerMessageEvent`：HTTP / SSE 对外协议，面向前端展示与回放。

## 2. 总体约束

- 所有接口默认超时时间为 120 秒。
- 所有非 SSE 接口统一返回 `ApiResponse`。
- SSE 接口使用标准 `id`、`event`、`data` 格式。
- 前端只依赖服务端协议枚举，不直接依赖 SDK 内部枚举名。
- 一次用户输入会生成一个 `task_id`，同一轮输入产生的输入、文本、工具调用、工具结果和状态提示都应携带相同 `task_id`。
- `sequence` 在同一次 run / SSE 流内单调递增，用于前端排序和断线续传去重。
- 服务端应在任务闭合后再把稳定消息写入历史；未闭合的过程事件只用于实时展示，不直接成为历史。

## 3. 通用响应

### 3.1 ApiResponse

```json
{
  "code": 0,
  "message": "ok",
  "success": true,
  "data": {}
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | `int` | 业务状态码，`0` 表示成功。 |
| `message` | `str` | 可读状态说明。 |
| `success` | `bool` | 请求是否成功。 |
| `data` | `any` | 具体响应数据。 |

### 3.2 错误响应

```json
{
  "code": 40001,
  "message": "会话不存在",
  "success": false,
  "data": null
}
```

建议首批错误码：

| code | 语义 |
| --- | --- |
| `40000` | 请求参数错误。 |
| `40001` | 会话不存在。 |
| `20003` | 当前流无法续接，需要刷新页面读取历史。 |
| `50001` | Runtime 执行失败。 |
| `60001` | Redis Stream 读取超时，当前连接结束。 |

## 4. SSE 协议

### 4.1 Completion 接口

```text
POST /api/v1/sessions/{session_id}/completion
```

请求体：

```json
{
  "event_type": "user_input",
  "query": "帮我查一下杭州天气",
  "interruption_response": null,
  "last_event_id": null,
  "client_event_id": "optional-client-id"
}
```

中断恢复请求示例：

```json
{
  "event_type": "interruption_response",
  "query": null,
  "interruption_response": {
    "request_id": "interruption_request_id:xxx",
    "request_type": "_os_async_tool_request",
    "response": {
      "data": {
        "option": "approve"
      }
    }
  },
  "last_event_id": null,
  "client_event_id": "optional-client-id"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `event_type` | `EventType` | 否 | 输入事件类型。默认 `user_input`；当前还支持 `interruption_response`。 |
| `query` | `str | null` | 条件必填 | `user_input` 时必须为非空文本；`interruption_response` 时必须为 `null`。 |
| `interruption_response` | `InterruptionResponsePayload | null` | 条件必填 | `interruption_response` 时必须携带完整中断恢复响应；`user_input` 时必须为 `null`。 |
| `last_event_id` | `str | null` | 否 | 客户端已收到的最后一个 SSE `id`。普通用户输入传值时服务端从该 id 之后续接。 |
| `client_event_id` | `str` | 是 | 前端生成的幂等标识，用于避免重复提交；所有 completion 输入类型都必须携带。 |

约束：

- `event_type=user_input` 时，`query` 必须是非空字符串，`client_event_id` 必须由前端生成并在重试时保持不变，建议使用 UUID 字符串。
- `event_type=interruption_response` 时，服务端将 `interruption_response` 作为 Runtime 输入包的首个事件直接提交，用于唤醒对应暂停任务；与普通输入一样通过 `client_event_id` 执行 active run、Redis Stream 与 history 幂等判断。
- `last_event_id=null` 表示客户端没有可续接位置；`last_event_id` 有值时，普通用户输入请求应从该 SSE id 之后补发或继续推送。
- completion 接口不负责页面首次加载恢复；页面首次加载恢复统一使用 `completion/resume` 接口。

### 4.2 Resume 接口

```text
POST /api/v1/sessions/{session_id}/completion/resume
```

该接口没有请求体，专用于页面首次加载后的会话恢复。字段定义与 completion 接口内部保持一致，服务端内部可复用同一套运行、SSE 推送和事件投影实现。

约束：

- 前端不传 `query`、`last_event_id` 和 `client_event_id`。
- 服务端根据 session 中的 `run_id` 优先从 Redis Stream 恢复；若无法从 Redis Stream 恢复，则直接走 agent 生成。
- Redis Stream 存在时，因为页面首次恢复没有 `last_event_id`，服务端从 Redis Stream 起点开始补发。

### 4.3 前端调用场景

| 场景 | 请求方式 | 服务端行为 |
| --- | --- | --- |
| 页面首次加载 | 先请求 history；完成后立即请求 `completion/resume`，不传请求体。 | 服务端根据 `run_id` 判断恢复方式。若 Redis Stream 存在，返回 `start.code=0`，并从当前未完成 run 的流起点开始追加；若需要重新走 agent 生成，返回 `start.code=20002`，前端清空当前任务内容后按重放结果展示；无内容时直接返回空内容。 |
| 正常发送消息 | 请求 completion，传 `query`、`client_event_id`，`last_event_id=null`。 | 进入默认追加生成模式，返回 `start.code=0` 后继续流式输出。 |
| 提交中断响应 | 请求 completion，传 `event_type=interruption_response` 和 `interruption_response`。 | 服务端将响应写回对应中断请求并推进已暂停任务，复用普通 Runtime 与 SSE 输出流程。 |
| 前端异常中断后续接 | 请求 completion，传原始 `query`、原始 `client_event_id`、`last_event_id`。 | 若 Redis Stream 仍有缓存且能补发到 `complete`，从 `last_event_id` 之后续接；若 stream 不可用或补发失败，服务端直接进入 replay 重新生成。 |
| 首次请求超时后重试 | 请求参数与普通发送消息一致，并保持 `client_event_id` 不变。 | 优先按幂等逻辑恢复已有运行或历史结果，避免重复生成。 |

### 4.4 SSE Envelope

每条 SSE 使用如下结构：

```text
id: 12
event: chunk
data: {"type":"message_delta","session_id":"session_id:xxx","task_id":"task_id:xxx","sequence":12,"start_ts_ms":1798867200000,"data":{"delta_content":"你好"}}
```

SSE `event` 可选值：

| event | 说明 |
| --- | --- |
| `start` | 本次 SSE 流开始，声明客户端渲染模式。 |
| `heartbeat` | 心跳包，保持连接存活。 |
| `chunk` | 普通业务事件。 |
| `stream_chunk` | 从 Redis Stream 或其他恢复队列补发的历史流事件。 |
| `end` | 本次 SSE 传输完成。 |
| `error` | 本次 SSE 传输失败。 |

`id` 使用服务端投影后的 `sequence` 字符串。前端可在断线重连时通过请求体 `last_event_id` 告诉服务端已收到的最后位置，服务端从该 id 之后续接。

约束：

- `sequence` 是服务端对外展示序号，必须在同一次 run / SSE 流内单调递增；当前由单次 `RuntimeEventHandler` 本地计数器维护。
- Redis Stream 中保存的事件必须携带同一个 SSE `id` / `sequence`，或者保存可从 `last_event_id` 精确定位后一条事件的索引。
- `event: chunk` 类型的业务事件写入 Redis Stream，用作后续 `last_event_id` 续接的数据源。
- `event: heartbeat` 也写入 Redis Stream，并刷新 stream 过期时间，避免长时间无业务输出导致恢复现场过期；恢复补发时 heartbeat 仍按 `event: heartbeat` 发送。
- `RuntimeEventHandler` 内部应启动独立心跳协程，按 `SSE_HEARTBEAT_INTERVAL_SECONDS` 固定间隔发送 heartbeat；Runtime 正常完成并发送 `complete` 前应先停止心跳。
- heartbeat 任务停止时直接设置 stop 状态并等待任务退出；stop 状态一旦设置，后续不得再向 Redis Stream 或当前 SSE 响应写入 heartbeat，保证 `complete` 是最后一个 chunk。
- `event: start`、`event: end`、`event: error` 只是当前 SSE 连接的控制片段，不写入 Redis Stream；重连后由服务端根据运行时状态重新生成对应控制事件。
- Redis Stream 只保存当前未完成 run 的短期业务流事件和 heartbeat，不保存已经进入 history 的稳定历史。

`start` 事件必须作为当前 SSE 响应的第一条业务事件发送，用于告诉前端如何处理已有展示内容：

```text
event: start
data: {"code":0,"message":"ok","success":true,"data":{"session_id":"session_id:xxx","run_id":"run_id:xxx"}}
```

`start` 事件的 `data` 统一使用 `ApiResponse` 结构，客户端通过 `code` 判断本次 SSE 流的操作类型：

| code | 操作类型 | 说明 |
| --- | --- | --- |
| `0` | 默认追加 | SSE 默认行为；前端保留已有内容并继续追加。 |
| `20002` | `replay` | 重放生成模式，前端应清空当前任务已有内容，再按服务端重放结果重新生成。 |
| `20003` | `reload_page` | 数据可能已经存在于历史中，需要前端刷新页面。 |

说明：

- `start.code` 是服务端运行时决策结果，前端不预判当前请求会进入默认追加还是 `replay`。
- 前端只根据实际收到的 `start.code` 选择渲染行为：`0` 按 SSE 默认追加，`replay` 清空当前任务内容后重放，`reload_page` 刷新页面读取 history。
- 这里的 `code` 是 SSE 控制操作码，不等同于普通 HTTP 成功或失败状态；只要 `success=true`，表示服务端已成功给出本次流的处理指令。

### 4.5 ServerMessageEvent

`data` 是 JSON 字符串，统一结构如下：

```json
{
  "type": "message_delta",
  "session_id": "session_id:xxx",
  "run_id": "run_id:xxx",
  "task_id": "task_id:xxx",
  "event_id": "event_id:xxx",
  "parent_event_id": "event_id-root",
  "sequence": 12,
  "start_ts_ms": 1798867200000,
  "is_history_event": false,
  "data": {}
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | `str` | 前端消费的消息事件类型。 |
| `session_id` | `str` | 当前会话 id。 |
| `run_id` | `str` | 标识一次完整的 agent 运行。 |
| `task_id` | `str | null` | 当前输入任务 id。 |
| `event_id` | `str` | 当前逻辑事件链 id。 |
| `parent_event_id` | `str | null` | 父事件链 id，顶层事件使用 `event_id-root`。 |
| `sequence` | `int` | 同一次 run / SSE 流内的展示顺序。 |
| `start_ts_ms` | `int` | 当前事件创建时间，毫秒时间戳。 |
| `is_history_event` | `bool` | 是否为 Runtime 恢复时补发的历史事件；不等同于 Redis Stream 的 SSE 重放。 |
| `data` | `dict` | 当前事件载荷。 |

### 4.6 事件类型

#### input

表示服务端确认收到用户输入。前端发送后可以先做本地占位，收到该事件后用服务端内容替换占位。

```json
{
  "type": "input",
  "data": {
    "input_id": "input_id:xxx",
    "user_input": "帮我查一下杭州天气"
  }
}
```

#### activity_indicator

表示当前任务进展提示，例如检索中、上下文整理中。该类型用于 UI loading，不进入稳定历史。

```json
{
  "type": "activity_indicator",
  "data": {
    "icon": "search",
    "title": "正在查询",
    "subtitle": "获取实时天气数据"
  }
}
```

#### message_delta

表示 assistant 文本增量。

```json
{
  "type": "message_delta",
  "data": {
    "delta_content": "杭州今天"
  }
}
```

#### message_final

表示 assistant 文本已经稳定完成。前端可用它修正增量拼接结果，也可只作为结束标记。

```json
{
  "type": "message_final",
  "data": {
    "duration_ms": 2480,
    "content": "杭州今天晴，气温 26 摄氏度。",
    "reasoning_content": null,
    "finish_reason": "stop",
    "usage": {
      "prompt_tokens": 120,
      "completion_tokens": 24,
      "total_tokens": 144
    }
  }
}
```

#### complete

表示本次 agent 生成已经正常完成。该事件用于业务层确认生成结束，外层 SSE `end` 只表示本次传输结束。`task_id` 取本轮 history 最后一条稳定事件的 `task_id`；若本轮无稳定 history，则为 `null`。当前 `data` 仅携带 `duration_ms`。

```json
{
  "type": "complete",
  "data": {
    "duration_ms": 0
  }
}
```

#### tool_call

表示模型选择了一个工具。该事件来自 `ToolCallEvent` 的对外投影。

```json
{
  "type": "tool_call",
  "data": {
    "id": "call_abc",
    "name": "query_weather",
    "parameters": {
      "city": "杭州"
    }
  }
}
```

#### tool_result

表示工具执行形成稳定结果。该事件来自 `ToolResultEvent` 的对外投影。

```json
{
  "type": "tool_result",
  "data": {
    "duration_ms": 128,
    "tool_call_id": "call_abc",
    "status": "success",
    "content": "杭州当前晴，26 摄氏度。",
    "raw_data": {
      "city": "杭州",
      "temperature_celsius": 26
    }
  }
}
```

`tool_result.data.content` 是模型可见内容；工具失败时也由该字段承载失败原因，前端根据 `status` 判断展示状态。

#### tool_result_start

表示工具已开始执行但尚未形成稳定结果。该事件来自 `ToolResultEvent` 的 `started` 阶段，载荷字段与 `tool_call` 一致，用于前端展示工具名称和参数，不进入业务 history。

```json
{
  "type": "tool_result_start",
  "data": {
    "id": "call_abc",
    "name": "query_weather",
    "parameters": {
      "city": "杭州"
    }
  }
}
```

#### context_compression

表示本轮发生同步上下文压缩。默认只用于开发者观测，不建议普通用户界面展示。

压缩开始时发送 `merged=false`：

```json
{
  "type": "context_compression",
  "data": {
    "duration_ms": 0,
    "mode": "sync",
    "merged": false,
    "compressed_history_count": 0
  }
}
```

压缩结束时发送 `merged=true`：

```json
{
  "type": "context_compression",
  "data": {
    "duration_ms": 64,
    "mode": "sync",
    "merged": true,
    "compressed_history_count": 12
  }
}
```

#### interrupted

表示任务已暂停并等待外部系统提交中断响应。该事件只由
`InterruptedEvent` 的 `final` 阶段投影，前端应基于 `request_id` 保存待处理的
中断请求，并使用 `interruption_type`、`reason` 和 `params` 构建相应交互。

```json
{
  "type": "interrupted",
  "data": {
    "request_id": "interruption_request_id:xxx",
    "task_id": "task_id:xxx",
    "interruption_type": "_os_async_tool_request",
    "reason": "工具 user_approval 正在异步执行",
    "params": {
      "type": "async_hitl",
      "tool_id": "query_weather",
      "data": {
        "options": ["approve", "reject"]
      }
    }
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `request_id` | `str` | 中断请求唯一标识，后续提交中断响应时使用。 |
| `task_id` | `str` | 被暂停任务的 id。 |
| `interruption_type` | `InterruptionRequestType` | 中断请求类型。 |
| `reason` | `str` | 中断原因，供展示和排障使用。 |
| `params` | `dict` | 外部系统处理本次中断所需的结构化参数。 |

## 5. RuntimeEvent 投影规则

服务端应订阅 `Runtime.event_bus_id` 对应事件，并按下列规则投影为 SSE：

| RuntimeEvent | event_phase | 附加条件 | ServerMessageEvent |
| --- | --- | --- | --- |
| `InputEvent` | `final` | - | `input` |
| `MessageEvent` | `in_progress` | - | `message_delta` |
| `MessageEvent` | `final` | - | `message_final` |
| `ToolCallEvent` | `final` | - | `tool_call` |
| `ToolResultEvent` | `started` | - | `tool_result_start` |
| `ToolResultEvent` | `final` | `result != null` | `tool_result` |
| `ContextCompressionEvent` | `started` | `compression_mode=sync` | `context_compression`，`data.merged=false` |
| `ContextCompressionEvent` | `final` | `compression_mode=sync` | `context_compression`，`data.merged=true` |
| `InterruptedEvent` | `final` | - | `interrupted` |
| Runtime 正常结束 | - | `task_id` 取本轮 history 最后一条稳定事件 | `complete`，`data` 仅携带 `duration_ms` |
| `CustomEvent` | 任意 | - | 按 `payload.type` 映射，未知类型透传为 `activity_indicator` 或忽略 |

补充约束：

- `MessageEvent.chunk.choices[].delta.content` 映射到 `message_delta.data.delta_content`。
- 前端底部统一展示“正在思考”状态，不再依赖独立的工具选择开始事件。
- 前端收到 `tool_call` 后创建工具标签，收到 `tool_result_start` 后切换为执行中状态，收到 `tool_result` 后按工具调用 id 匹配更新为终态。
- `MessageEvent.completion.choices[0].message.content` 映射到 `message_final.data.content`。
- 具备稳定阶段语义的 `ServerMessageEvent.data` 可携带 `duration_ms` 字段；Runtime 派生 FINAL 事件由 `ResultCollector` 根据 STARTED/END 事件时间差填充。
- Runtime 正常结束后发送一次 `complete`，且在外层 SSE `end` 之前发送；`complete.task_id` 取本轮 history 最后一条稳定事件的 `task_id`，`complete.data` 当前仅携带 `duration_ms`，该耗时由 `RuntimeEventHandler` 从 handler 创建时开始计时。
- `ToolCallEvent.tool_call.function.arguments` 必须尽量解析为 JSON dict；解析失败时保留原字符串到 `parameters_text`。
- `RuntimeEvent.duration_ms` 映射到声明了 `duration_ms` 的 `ServerMessageEvent.data.duration_ms`。
- `ToolResultEvent.result.raw_data` 直接返回给前端，用于展示 SDK 技术细节。
- 如果底层 Runtime 事件阶段命名与本文协议不一致，例如内部使用 `END` 表示工具或压缩结束，服务端投影层应统一转换为本文约定的 `final` 语义。
- 内部异常不应把完整 traceback 透给前端，只返回可读错误摘要和服务端日志追踪 id。

## 6. 会话接口

### 6.1 创建会话

```text
POST /api/v1/sessions
```

请求体：

```json
{
  "title": "新的会话"
}
```

响应：

```json
{
  "code": 0,
  "message": "ok",
  "success": true,
  "data": {
    "session_id": "session_id:xxx",
    "title": "新的会话"
  }
}
```

### 6.2 页面初始化信息

```text
GET /api/v1/info
```

请求参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `limit` | `int` | 返回会话条数，默认 20。 |

响应：

```json
{
  "code": 0,
  "message": "ok",
  "success": true,
  "data": {
    "user_info": {
      "avatar": "https://api.dicebear.com/9.x/personas/svg?seed=turtlemap",
      "name": "TurtleMap Demo"
    },
    "session_info": {
      "active_session": {
        "session_id": "session_id:xxx",
        "title": "新的会话",
        "created_at": 1798867200000,
        "updated_at": 1798867260000
      },
      "sessions": [
        {
          "session_id": "session_id:xxx",
          "title": "新的会话",
          "created_at": 1798867200000,
          "updated_at": 1798867260000
        }
      ]
    }
  }
}
```

说明：

- `user_info` 当前为示例项目写死数据。
- `active_session` 从 `session_state` 最新快照取第一个会话 id，再读取对应 `session_meta` 作为展示信息；没有状态或会话已删除时为 `null`。
- `sessions` 来自 `session_meta`，按创建时间降序排序，最新创建的会话在前。

### 6.3 删除会话

```text
DELETE /api/v1/sessions/{session_id}
```

响应：

```json
{
  "code": 0,
  "message": "ok",
  "success": true,
  "data": null
}
```

删除语义建议先做软删除，避免后台压缩任务或异步工具结果回写时误触不存在状态。

## 7. 历史接口

### 7.1 查询历史

```text
GET /api/v1/sessions/{session_id}/history
```

响应：

```json
{
  "code": 0,
  "message": "ok",
  "success": true,
  "data": [
    {
      "message_id": "artifact_xxx",
      "task_id": "task_id:xxx",
      "client_event_id": "client_event_id:xxx",
      "content": [
        {
          "type": "input",
          "session_id": "session_id:xxx",
          "task_id": "task_id:xxx",
          "sequence": 1,
          "data": {
            "user_input": "帮我查一下杭州天气"
          }
        },
        {
          "type": "message_final",
          "session_id": "session_id:xxx",
          "task_id": "task_id:xxx",
          "sequence": 2,
          "data": {
            "content": "杭州今天晴，气温 26 摄氏度。"
          }
        }
      ],
      "created_at": 1798867205000
    }
  ]
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `message_id` | `str` | 历史任务组稳定 id，由 `message_projection.id` 转换得到。 |
| `task_id` | `str` | 当前任务组的稳定 task id。 |
| `client_event_id` | `str \| null` | 分组首个 input 的前端幂等 id。 |
| `content` | `list[ServerMessageEvent]` | 当前 task 从首个 input 开始的稳定展示事件列表。 |
| `created_at` | `int` | 历史任务组创建时间。 |

约束：

- 历史接口只返回稳定产物，不返回 `message_delta`。
- history 写入按 `task_id` 分组，每个有效 task 只写入一条 `message_projection` 记录。
- 一个 task 分组的首条事件必须是 `input`；无 `task_id`，或在该 task 首个 input 前到达的事件会被丢弃并记录 warning。
- 同一 task 后续的 `input`、`message_final`、`tool_call`、`tool_result`、`context_compression` 和 `interrupted` 等稳定事件按发生顺序统一保存到该记录的 `content_json` 中。
- `message_projection` 不再存储 `role`，角色由前端依据事件类型和展示规则决定。
- history 列表来自服务端应用层业务消息表，不直接读取 `BaseAgentState.history` 或 `SessionState` 快照。
- `BaseAgentState.history` 只作为 SDK Runtime 内部上下文状态，不作为前端 history 接口的数据源。

## 8. 页面首次加载恢复

页面首次加载恢复使用独立接口进入：

```text
POST /api/v1/sessions/{session_id}/completion/resume
```

该接口没有请求体。对外接口与普通 completion 拆开，服务端内部实现复用同一套 agent 运行、Redis Stream 续接和 SSE 事件投影逻辑。

恢复流程：

1. 服务端加载最新 `SessionState`。
2. 根据 session 中的 `run_id` 查询 Redis Stream。
3. 如果 Redis Stream 存在，发送 `event: start` 且 `data.code=0`，前端按 SSE 默认行为追加展示。
4. 页面首次恢复没有 `last_event_id`，因此从 Redis Stream 起点开始补发。
5. 如果 Redis Stream 不存在但需要继续由 agent 生成，发送 `event: start` 且 `data.code=20002`，提示前端清空当前任务内容并进入重放生成模式。
6. 调用 `Runtime.run(...)` 推进生成流程；若没有可恢复事件或可继续推进的任务，本次 SSE 内容为空。
7. 任务完成后发送 SSE `end`。

约束：

- 恢复方式只能由服务端运行时根据 `run_id`、Redis Stream 和最新 agent state 决定。
- 前端不能根据本地状态推断恢复方式，只能根据首个 `event: start` 中的 `data.code` 执行 UI 行为。

## 9. 并发与幂等

- completion 接口不做 session 级运行互斥，总是获取最新 agent state 进行生成。
- `client_event_id` 可用于识别同一个前端提交，重复请求应返回同一个运行结果或拒绝重复执行。
- `sequence` 用于前端去重；同一个 `run_id + sequence` 只能渲染一次。
- `run_id` 用于标识一次完整的 agent 运行。
- 多个 completion 并发运行时允许后写入的会话快照覆盖先写入的快照；前端 history 以应用层业务消息表为准。
- SSE 只保证当前连接内事件顺序自洽，不保证多个并发连接之间的输出可自动合并为全局有序历史。
- 后台工具结果或后台压缩回写时不需要打断正在进行的 completion；必要时只写 checkpoint，下一轮由 Runtime 恢复感知。

### 9.1 Active Run 缓存

服务端使用 Redis 记录当前会话正在运行的 completion：

```text
key: active_run_ids_{session_id}
type: hash
field: run_id
value: {"client_event_id":"uuid-str","start_ts_ms":1798867200000}
expire: 600
```

运行中的 SSE 事件写入 Redis Stream：

```text
key: run_stream_{run_id}
type: stream
entry id: Redis Stream 原生 id
value: {"sse_id":"12","event":"chunk","data":"..."}
expire: 600
```

约束：

- `active_run_ids_{session_id}` 的过期时间为 600 秒。
- hash field 使用当前 `run_id`，value 保存本次起始输入的 `client_event_id` 和 `start_ts_ms`。
- 同一个会话的活动 `run_id` 数量通常很少，幂等判断时直接遍历 hash value 匹配 `client_event_id`。
- `run_stream_{run_id}` 的过期时间为 600 秒，至少覆盖前端常见刷新、断线和首次请求超时重试窗口。
- 实时生成过程中的 `event: chunk` 和空闲保活 `event: heartbeat` 写入 `run_stream_{run_id}`；`start`、`end`、`error` 不写入 stream。
- Redis Stream 中的 heartbeat 必须分配正常递增的 `sse_id`，不能使用 `0`，否则无法参与 `last_event_id` 续接。
- 写入 Redis Stream 的 `sse_id` 必须与实际发送给前端的 SSE `id` 一致，便于 completion 接口根据 `last_event_id` 从后一条业务事件续接。
- Runtime 正常结束后应先写入 history，再清理对应 `run_id`；异常中断时依赖 Redis 过期兜底。
- 业务 history 写入应通过 `message_projection_run` 按 `run_id` 做一次性写入控制，同一个 `run_id` 只能成功写入一次。

### 9.2 client_event_id 透传

completion 的正常请求链路中，agent 输出的第一个 `input` 就是用户发送的输入。服务端应把请求中的 `client_event_id` 透传到输入事件 metadata 或服务端消息投影中，确保后续可以从流事件和 history 中反查该前端请求。

history 中也应保存 `client_event_id` 字段。建议历史投影结构至少包含：

```text
message_projection
    id
    session_id
    task_id
    client_event_id
    content_json
    created_at

message_projection_run
    run_id
    session_id
    created_at
```

### 9.3 幂等判断流程

completion 接口携带 `client_event_id` 时，服务端按以下顺序判断：

1. 查询 `active_run_ids_{session_id}`，遍历 hash value。如果存在相同 `client_event_id`，说明同一请求仍在运行中，并可取到对应 `run_id`。
2. 若 Redis Stream 中仍保留该运行的事件，根据 stream entry 中的 `sse_id` 从 `last_event_id` 之后续接；`last_event_id=null` 时从该运行流起点补发。
3. 若 active run 存在但 Redis Stream 已无可恢复缓存，或补发过程中无法读到 `complete`，说明原运行进程可能已经异常退出，刷新页面无法恢复短期流，服务端继续走 Runtime replay 兜底。
4. 若没有可用 active run，查询 history 是否已有相同 `client_event_id`。
5. 若 history 已存在该 `client_event_id`，返回 `event: start` 且 `data.code=20003`，提示前端刷新页面读取稳定历史。
6. 若 active run 和 history 都不存在该 `client_event_id`，按普通生成模式执行，并返回 `event: start` 且 `data.code=0`。

## 10. 存储建议

第一阶段可以只依赖 `StateStore` 保存 `SessionState` 快照，并在服务层维护轻量 session 元信息表：

```text
session
    session_id
    title
    deleted
    created_at
    updated_at
```

history 列表使用应用层业务消息表保存，不直接依赖 agent 内部 history。推荐表结构：

```text
message_projection
    id
    session_id
    task_id
    client_event_id
    content_json
    created_at

message_projection_run
    run_id
    session_id
    created_at
```

说明：

- `message_projection` 存稳定展示历史，是 history 接口的数据源。
- `message_projection.id` 使用数据库自增主键维护 history 展示顺序，不复用 SSE `sequence`。
- `message_projection` 按 `task_id` 聚合保存：首条必须是 input，同 task 的稳定展示事件按顺序序列化到 `content_json`；一组只对应一条记录。
- 无法按 task 归属，或在首个 input 前出现的稳定事件不落库，并通过 warning 日志保留排查信息。
- `message_projection_run` 用 `run_id` 控制业务 history 只写入一次，避免同一次 agent 运行重复投影。
- Runtime 正常结束后，服务端应先把本轮稳定展示内容写入 `message_projection`，再保存或清理短期恢复状态。
- 生成中断续接只依赖 Redis Stream 和 completion 请求中的 `last_event_id`，不需要额外建设短期事件投影表。
- 页面首次加载恢复使用 `completion/resume` 接口，不通过事件投影表恢复。
- `message_projection` 是服务端应用层投影，不替代 SDK 内部 `SessionState`；两者职责不同，前者面向 UI history，后者面向 agent runtime 恢复。

## 11. 首批实现建议

建议按下面顺序落地：

1. 实现 `ApiResponse`、`CompletionRequest`、`ServerMessageEvent` 等服务端协议模型。
2. 实现 `RuntimeEvent -> ServerMessageEvent` 投影函数。
3. 实现 completion SSE 接口，并接入 `EventBus.subscribe(...)`。
4. 实现 session 创建、列表、删除接口。
5. 实现基于 `message_projection` 业务表的 history 查询接口。
6. 再补 Redis Stream 流式续接和严格幂等。

这个顺序能先跑通“创建会话 -> 发起输入 -> SSE 流式返回 -> 查询历史”的最小闭环，再逐步补齐恢复和多端体验。
