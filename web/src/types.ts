export type ApiResponse<T> = {
  code: number;
  message: string;
  success: boolean;
  data: T;
};

export type UserInfo = {
  avatar: string;
  name: string;
};

export type Session = {
  session_id: string;
  title: string;
  created_at: number | null;
  updated_at: number | null;
};

export type AppInfo = {
  user_info: UserInfo;
  session_info: {
    active_session: Session | null;
    sessions: Session[];
  };
};

export type HistoryMessage = {
  message_id: string;
  task_id: string | null;
  client_event_id: string | null;
  content: ServerMessageEvent[];
  created_at: number;
};

export type ServerMessageType =
  | "input"
  | "activity_indicator"
  | "message_delta"
  | "message_final"
  | "tool_call"
  | "tool_result_start"
  | "tool_result"
  | "context_compression_start"
  | "context_compression_final"
  | "interrupted"
  | "complete";

export type ServerMessageEvent = {
  type: ServerMessageType;
  session_id: string;
  run_id: string;
  task_id: string | null;
  event_id: string;
  parent_event_id: string | null;
  start_ts_ms: number;
  is_history_event: boolean;
  data: Record<string, unknown>;
};

export type SseFrame = {
  id: string | null;
  event: "start" | "chunk" | "stream_chunk" | "heartbeat" | "end" | "error" | string;
  data: string;
};

export type ChatItem = {
  id: string;
  taskId: string | null;
  clientEventId: string | null;
  events: ServerMessageEvent[];
  status: "pending" | "streaming" | "complete" | "error";
  createdAt: number;
  optimisticInput?: string;
  historyExpanded: boolean;
  error?: {
    message: string;
    data: Record<string, unknown>;
  };
  interruptionStateByRequestId: Record<string, ResolvedInterruptionState>;
};

export type ResolvedInterruptionState = {
  title: string;
};

export type ChatMessageBlock = TextBlock | ActivityLabelBlock | InterruptedBlock | CompletionBlock;

export type TextBlock = {
  type: "text";
  id: string;
  content: string;
  reasoningContent?: string | null;
  display?: "history_quote";
  metadata?: Record<string, unknown>;
};

export type ActivityLabelBlock = {
  type: "activity_label";
  id: string;
  title: string;
  subtitle?: string | null;
  durationMs?: number;
  icon?: "sparkles" | "tool" | "database" | "check" | "alert";
  state: "loading" | "normal";
  source: "thinking" | "tool" | "context_compression" | "activity" | "error";
  sourceId?: string | null;
  approvalPending?: boolean;
  metadata?: Record<string, unknown>;
};

export type InterruptionAction = "retry" | "approve" | "reject";

export type InterruptedBlock = {
  type: "interrupted";
  id: string;
  requestId: string;
  interruptionType: string;
  title: string;
  actions: InterruptionAction[];
  responseOptions: string[];
  resolvedTitle?: string;
  durationMs?: number;
  metadata?: Record<string, unknown>;
};

export type CompletionBlock = {
  type: "completion";
  id: string;
  durationMs: number;
  live?: boolean;
  startedAt?: number;
  metadata?: Record<string, unknown>;
};

export type ConnectionState = "booting" | "idle" | "loading" | "recovering" | "streaming" | "error";

export type SendButtonState = "send" | "generating" | "stop";
