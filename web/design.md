# TurtleMap Web 前端设计文档

## 1. 背景与目标

本项目计划基于 `examples/main.py` 提供的示例 HTTP 服务，建设一套 ChatGPT 风格的 Web 前端。首期界面采用左侧会话列表、右侧聊天区的双栏结构，重点支持会话创建、会话切换、历史消息展示、用户输入、SSE 流式回复、断线续接与页面恢复。

前端工程统一放在 `./web` 目录下，与现有 Python SDK、示例服务和后端协议文档保持分离。

## 2. 已知后端接口

后端入口为 `examples/main.py`，当前可直接消费的接口如下：

| 能力 | 方法与路径 | 前端用途 |
| --- | --- | --- |
| 页面初始化信息 | `GET /api/v1/info?limit=20` | 获取用户信息、默认活动会话和按创建时间降序排序的会话列表。 |
| 创建会话 | `POST /api/v1/sessions` | 点击新建会话时创建服务端会话。 |
| 查询历史 | `GET /api/v1/sessions/{session_id}/history` | 切换会话或刷新页面后重建聊天记录。 |
| 删除会话 | `DELETE /api/v1/sessions/{session_id}` | 左侧会话删除或清理。 |
| 发送消息 | `POST /api/v1/sessions/{session_id}/completion` | 提交用户输入并消费 SSE 流式输出。 |
| 页面恢复 | `POST /api/v1/sessions/{session_id}/completion/resume` | 页面首次加载后恢复未完成 run。 |

非 SSE 接口统一返回 `ApiResponse`：

```json
{
  "code": 0,
  "message": "ok",
  "success": true,
  "data": {}
}
```

completion 相关接口返回标准 SSE，前端需要监听 `start`、`chunk`、`stream_chunk`、`heartbeat`、`end`、`error`。

### 2.1 页面初始化接口

`GET /api/v1/info?limit=20` 是前端首屏初始化入口。当前服务端已经不再暴露独立的 `GET /api/v1/sessions` 路由，左侧会话列表应从该接口读取。

响应示例：

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

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_info.avatar` | `str` | 当前示例项目写死的用户头像地址。 |
| `user_info.name` | `str` | 当前示例项目写死的用户展示名称。 |
| `session_info.active_session` | `SessionResponse | null` | 最近活动会话，以最新 `SessionState` 快照反查得到；没有状态或会话已删除时为空。 |
| `session_info.sessions` | `SessionResponse[]` | 未删除会话列表，当前按 `created_at DESC, session_id DESC` 返回，最新创建的会话在前。 |

## 3. 首期页面范围

首期只做一个可用的聊天工作台，不做营销首页。

核心区域：

| 区域 | 功能 |
| --- | --- |
| 左侧会话栏 | 新建会话、会话列表、当前会话高亮、删除会话、加载状态与空状态。 |
| 顶部上下文栏 | 展示当前会话标题、连接/生成状态，预留设置入口。 |
| 聊天消息区 | 渲染用户消息、assistant 消息、工具调用摘要、上下文压缩提示、错误提示。 |
| 输入区 | 多行输入、发送按钮、生成中不可点击、预留停止状态、快捷键提交。 |

推荐首期屏幕形态：

| 视口 | 布局 |
| --- | --- |
| 桌面端 | 左侧固定宽度会话栏，右侧聊天区自适应。 |
| 移动端 | 默认展示聊天区，通过按钮打开会话抽屉。 |

## 4. 信息架构与数据模型

前端建议维护以下核心状态：

| 状态 | 说明 |
| --- | --- |
| `userInfo` | 当前用户展示信息，由 `GET /api/v1/info` 的 `user_info` 加载。 |
| `sessions` | 左侧会话列表，由 `GET /api/v1/info` 的 `session_info.sessions` 加载，按创建时间降序排序。 |
| `activeSessionId` | 当前打开的会话 id，首次加载优先使用 `session_info.active_session`。 |
| `messages` | 当前会话稳定历史和实时事件投影后的展示消息。 |
| `activeRun` | 当前 SSE run 信息，包含 `run_id`、`last_event_id`、是否生成中。 |
| `pendingInputs` | 本地乐观渲染的用户输入，使用 `client_event_id` 去重。 |
| `connectionState` | `idle`、`connecting`、`streaming`、`recovering`、`error` 等连接状态。 |

消息展示模型建议不要直接等同于服务端 `ServerMessageEvent`。前端可以先消费事件，再聚合为适合 UI 的 `ChatMessage`。其中 assistant 消息体使用可追加的 `blocks` 列表表达，文本、工具状态、上下文压缩状态和完成状态都作为独立块追加或更新。

```ts
type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  displayRole?: "user" | "assistant" | "tool" | "system";
  taskId?: string | null;
  blocks: ChatMessageBlock[];
  status: "pending" | "streaming" | "complete" | "error";
  createdAt?: number;
};

type ChatMessageBlock =
  | TextBlock
  | ActivityLabelBlock
  | InterruptedBlock
  | CompletionBlock;

type TextBlock = {
  type: "text";
  id: string;
  content: string;
  reasoningContent?: string | null;
};

type ActivityLabelBlock = {
  type: "activity_label";
  id: string;
  title: string;
  subtitle?: string | null;
  icon: string;
  state: "loading" | "normal";
  source: "thinking" | "tool" | "context_compression";
  sourceId?: string | null;
};

type CompletionBlock = {
  type: "completion";
  id: string;
  durationMs: number;
};

type InterruptedBlock = {
  type: "interrupted";
  id: string;
  requestId: string;
  interruptionType: "_os_exception_resume" | "_os_async_tool_request";
  title: string;
  actions: Array<"retry" | "approve" | "reject">;
  metadata?: Record<string, unknown>;
};
```

发送按钮建议抽象为三个展示状态：

| 状态 | 触发条件 | 行为 |
| --- | --- | --- |
| `send` | 当前会话恢复完成，且没有正在生成的消息。 | 可点击，提交输入。 |
| `generating` | 当前正在生成，但首期暂不支持停止。 | 不可点击，展示生成中状态。 |
| `stop` | 预留给后续停止生成能力。 | 首期不触发，后续接入停止接口后可点击。 |

## 5. SSE 消费策略

前端提交消息时：

1. 若没有当前会话，先调用 `POST /api/v1/sessions` 创建会话。
2. 首次进入会话时必须先调用 `completion/resume` 恢复信息；恢复成功前发送按钮不可用。
3. 前端生成 `client_event_id`，先本地追加用户消息占位，并创建一条 assistant 消息。
4. assistant 消息初始只包含一个 `thinking` 来源的 `activity_label` 块，展示“正在思考”；该状态标签始终保持在当前 assistant 消息体底部。
5. 生成期间发送按钮进入 `generating` 状态，首期不可点击；按钮组件需要预留 `send`、`stop`、`generating` 三态切换能力。
6. 调用 `POST /api/v1/sessions/{session_id}/completion`，普通消息请求体包含 `event_type=user_input`、`query`、`client_event_id`、`last_event_id`。
7. 收到 `event: start` 后，根据 `data.code` 决定展示策略。
8. 收到 `message_delta` 时追加 assistant 文本。
9. 收到 `message_final` 时用稳定最终文本修正当前 assistant 消息。
10. 收到 `complete` 后标记本轮结束。
11. 收到 `end` 后关闭当前 SSE 连接状态，发送按钮恢复为 `send` 状态。

assistant 消息块更新规则：

| 服务端事件 | UI 行为 |
| --- | --- |
| `message_delta` | 若当前 assistant 消息没有文本块，则新增一个 `text` 块；后续 delta 追加到该文本块内容中。文本块展示在 `thinking` 标签上方，保证“正在思考”始终位于底部。 |
| `message_final` | 使用稳定最终文本修正当前 `text` 块，并补充 `reasoningContent`、`finish_reason`、`usage` 等可展示元信息。 |
| `tool_call` | 新增一个 `tool` 来源的 `activity_label` 块，状态为 `loading`，内容展示工具图标、工具名、参数摘要。 |
| `tool_result` | 根据 `tool_call_id` 匹配 `tool_call.data.id`，将对应 `activity_label` 状态改为 `normal`，`subtitle` 展示工具耗时和结果状态。 |
| `context_compression` 开始 | 新增一个 `context_compression` 来源的 `activity_label` 块，状态为 `loading`。 |
| `context_compression` 结束 | 将对应压缩 `activity_label` 改为 `normal`，`subtitle` 展示压缩耗时和压缩历史条数。 |
| `interrupted` | 新增一个 `interrupted` 卡片块，展示中断原因对应的文案和可用操作按钮。卡片位于 `thinking`、`completion` 等尾部状态块之前。 |
| `input`，`data.event_type=interruption_response` | 新增逻辑角色为 `user` 的恢复输入消息，设置 `displayRole=assistant`，以左侧 assistant 样式展示“重试”“已同意”或“已拒绝”；后续恢复输出创建新的 assistant 占位并展示在其下方。 |
| `complete` | 新增一个 `completion` 块，展示“用时 xxx 秒”，并将 assistant 消息状态改为 `complete`。 |
| `end` | 关闭 SSE 连接状态，发送按钮恢复为 `send` 状态。 |

`interrupted` 卡片展示规则：

| `data.interruption_type` | 卡片标题 | 操作按钮 |
| --- | --- | --- |
| `_os_exception_resume` | `任务因运行异常暂停，是否需要重试？` | `重试` |
| `_os_async_tool_request`，且 `data.params.type="async_hitl"` | `请确认是否继续执行该操作?` | `同意`、`拒绝` |
| `_os_async_tool_request`，其他 `data.params.type` | `任务正在等待外部工具返回结果，收到结果后将继续处理。` | 无 |

卡片应延续 assistant 消息的白底、细描边和圆角视觉，不使用用户消息蓝色背景。卡片区域用于打开字段详情，按钮单独调用 `POST /api/v1/sessions/{session_id}/completion`。每次按钮操作使用新的 `client_event_id`、固定 `last_event_id=null`，并传入 `event_type="interruption_response"`、`query=null` 与完整 `interruption_response`：

| 卡片类型 / 按钮 | `interruption_response` |
| --- | --- |
| `_os_exception_resume` / 重试 | `{ request_id, request_type: "_os_exception_resume", response: {} }` |
| `_os_async_tool_request` + `async_hitl` / 同意 | `{ request_id, request_type: "_os_async_tool_request", response: { data: { option: params.data.options[0] } } }` |
| `_os_async_tool_request` + `async_hitl` / 拒绝 | `{ request_id, request_type: "_os_async_tool_request", response: { data: { option: params.data.options[1] } } }` |

`start.code` 处理规则：

| code | 前端行为 |
| --- | --- |
| `0` | 保留已有展示内容，继续追加新事件。 |
| `20002` | 进入 replay 模式，清空当前未完成任务内容后按服务端事件重建。 |
| `20003` | 重新拉取 history，对齐最终稳定态。 |

页面首次加载或切换到某个会话时：

1. 页面首次加载时先调用 `GET /api/v1/info`，初始化 `userInfo`、`sessions` 和 `activeSessionId`。
2. 若 `active_session` 不为空，调用 history 接口渲染稳定历史。
3. history 完成后调用 resume 接口尝试恢复未完成 run。
4. 若 resume 返回 replay，按服务端事件重建正在生成的消息。
5. 若 resume 无内容或正常 end，保持 history 展示。
6. 切换会话时只需要加载目标会话 history，并对目标会话执行 resume。

## 6. 视觉与交互方向

目标风格参考 ChatGPT，但不直接复刻。建议首期采用克制、专注、适合长时间阅读和调试的工作台风格：

| 元素 | 设计建议 |
| --- | --- |
| 左侧栏 | 深浅中性的窄栏，强调会话扫描效率，支持标题截断和更新时间弱展示。 |
| 聊天气泡 | 用户消息靠右或独立背景，assistant 消息靠左或正文流式布局。 |
| 输入框 | 底部吸附，多行自增长，发送按钮使用图标按钮。 |
| 工具事件 | 以紧凑的可折叠工具块展示，不打断正文阅读。 |
| 错误状态 | 就地展示重试入口，避免整页阻断。 |
| 加载状态 | 使用轻量状态行或 shimmer，避免大面积空转动画。 |

首期不建议做复杂主题系统。可以预留 CSS 变量，先完成稳定的浅色主题，再补深色主题。

## 7. 技术方案候选

前端工程可选两条路线：

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| Vite + React + TypeScript | 启动快，适合单页聊天工作台，生态成熟。 | 需要独立构建和 dev server。 |
| Next.js | 后续 SSR、路由和部署能力更完整。 | 对当前单页工作台略重。 |

建议首期使用 `Vite + React + TypeScript`，除非后续明确需要 SSR、多页面路由或统一全栈部署。

SSE 注意点：

- 标准 `EventSource` 不支持 `POST` 请求体，completion 接口是 `POST`，因此前端需要使用 `fetch` 读取 `ReadableStream` 并自行解析 SSE。
- 需要记录已处理的 `run_id + sequence`，避免断线恢复时重复渲染。
- 需要保存当前 run 的 `last_event_id`，用于重试或断线续接。

## 8. 需要你确认或提供的信息

以下信息会影响首版设计和工程搭建：

| 优先级 | 需要确认的信息 | 影响 |
| --- | --- | --- |
| 高 | 前端是否只做本地开发调试，还是要生产部署。 | 决定构建方式、环境变量、反向代理和部署脚本。 |
| 高 | 后端服务固定地址，例如 `http://localhost:8000`，还是需要可配置。 | 决定 API client 和 `.env` 设计。 |
| 高 | 首期是否需要登录、用户隔离或多用户会话。 | 决定路由、鉴权、会话列表过滤和错误处理。 |
| 高 | 会话标题是否由用户输入、首条消息自动生成，还是始终“新的会话”。 | 决定新建会话交互和后续是否需要改名接口。 |
| 高 | 工具调用结果是给普通用户看，还是主要给开发者调试看。 | 决定工具事件默认展开、折叠或隐藏。 |
| 中 | assistant 是否需要 Markdown、代码高亮、表格、复制按钮。 | 决定消息渲染依赖和样式复杂度。 |
| 中 | 是否需要停止生成、重新生成、编辑上一条消息。 | 当前后端未暴露对应接口，若需要需同步补服务端能力。 |
| 中 | 是否需要附件、图片、文件上传。 | 当前协议只有文本输入，若需要需扩展请求体和后端工具链。 |
| 中 | 是否需要保留前端本地状态，例如刷新后记住当前会话。 | 决定 localStorage/sessionStorage 使用策略。 |
| 中 | 是否有品牌名、Logo、主色或视觉禁忌。 | 决定 UI 识别度和主题变量。 |
| 低 | 是否需要国际化，中英文切换。 | 决定文案组织方式。 |
| 低 | 浏览器兼容范围。 | 决定是否需要额外 polyfill 和降级策略。 |

## 9. 首期推荐范围

为了尽快形成可试用闭环，建议第一版只包含：

- `Vite + React + TypeScript` 工程骨架。
- ChatGPT 式双栏聊天工作台。
- 会话创建、列表、切换、删除。
- history 加载和空状态。
- `fetch + ReadableStream` 的 POST SSE 消费。
- `input`、`message_delta`、`message_final`、`tool_call`、`tool_result`、`context_compression`、`complete` 的基础渲染。
- 断线后的 `last_event_id` 续接与页面加载 `resume`。
- 基础错误提示和重试入口。

以下能力建议放到第二阶段：

- 登录与多用户体系。
- 会话重命名、搜索、归档。
- 停止生成、重新生成、编辑消息。
- 附件上传、多模态输入。
- 深色主题和完整偏好设置。
- 复杂工具结果的专用可视化。

## 10. 待定决策

当前可以先按以下默认假设推进工程：

| 决策项 | 默认假设 |
| --- | --- |
| 技术栈 | Vite + React + TypeScript。 |
| 开发后端地址 | `http://localhost:8000`，通过环境变量覆盖。 |
| 鉴权 | 首期不做登录。 |
| 消息渲染 | 支持 Markdown 和代码块复制。 |
| 工具事件 | 默认折叠，assistant 消息下方展示摘要。 |
| 当前会话记忆 | 使用 localStorage 记住最近打开的 `session_id`。 |
| UI 主题 | 先做浅色 ChatGPT 风格，保留主题变量。 |
