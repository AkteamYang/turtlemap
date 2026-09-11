# 接口设计

整体接口超时120s

## completion接口

POST /api/v1/sse/{session_id}/completion

- request
  - session_id
  - event_type: user_input | interruption_response，默认 user_input
  - query: str | null，user_input 时为非空文本，interruption_response 时传 null
  - interruption_response: InterruptionResponsePayload | null，interruption_response 时传完整恢复载荷
- response
  - 支持SSE，标准id、event、data结构，
    - id是chunk序列号
    - event字符串类型：SSE_HEARTBEAT(心跳包) CHUNK（普通流消息）、STREAM_CHUNK(redis stream流)、SSE_END(传输完成)
    - data是数据载荷，json字符串
  - data json结构，type、data
    - 公共字段
      - type表示数据类型：activity_indicator、input、chunk、tool_call、tool_result
      - start_ts_ms 开始时间戳毫秒
      - duration_ms 耗时
      - sequence 序号
      - task_id 一次输入响应同属一次任务
    - type==activity_indicator 对应一条状态进展label如：检索中、图谱检索中等，图标+title+subtitle+三个点的loading动画
      - data
        - icon
        - title
    - type==input 表示用户输入，展示为用户输入，用户从输入框中输入时先用输入框内容立即作为占位展示到ui上，待服务端返回后用第一条input内容替换
      - data
        - user_input，用户输入的内容
    - type==chunk LLM返回的content
      - data
        - delta_content 增量content内容
    - type==tool_call 选择了一个工具，获得一个工具后tool_call_start展示tool图标+tool_call.name，复用activity_indicator
      - data
        - name
        - parameters 参数字典
        - id
    - type==tool_result 工具结果
      - data
        - tool_call_id 工具调用id
        - result 工具结果字典

## history列表接口

GET /api/v1/sessions/{session_id}/history

- request
  - session_id: str 会话id

- response
  - code: int
  - message: str
  - success: true/false
  - data: list[dict]
    - message_id: str
    - task_id: str
    - client_event_id: str | null，当前 task 首个 input 的前端幂等 id
    - content: json string
      - data: list[dict]
        - 同 **data json结构**

history 按 `task_id` 分组保存，一组只对应一条记录。每组首条事件必须是 `input`；
无 `task_id` 或在该 task 首个 input 前到达的事件会被丢弃并记录 warning。`content`
保存同一 task 的完整稳定事件列表，`message_projection` 不再保存 `role`。

## 页面初始化信息接口

GET /api/v1/info

- response
  - code: int
  - message: str
  - success: true/false
  - data: dict
    - user_info
      - avatar: str
      - name: str
    - session_info
      - active_session: dict | null，从 session_state 最新快照取第一个会话
      - sessions: list[dict]，按创建时间降序排序，最新创建的会话在前
        - title
        - session_id

## 创建session接口

POST /api/v1/sessions

- response
  - code: int
  - message: str
  - success: true/false

## 删除session接口

DELETE /api/v1/sessions/{session_id}

- response
  - code: int
  - message: str
  - success: true/false
