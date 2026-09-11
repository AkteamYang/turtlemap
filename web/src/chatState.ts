import type {
  ActivityLabelBlock,
  ChatItem,
  ChatMessageBlock,
  CompletionBlock,
  HistoryMessage,
  InterruptedBlock,
  ResolvedInterruptionState,
  ServerMessageEvent,
  TextBlock,
} from "./types";

export type ChatItemPresentation = {
  inputBlock: TextBlock | null;
  historyBlocks: ChatMessageBlock[];
  currentBlocks: ChatMessageBlock[];
};

/** 将持久化任务记录转换为前端任务项。 */
export function historyToItems(history: HistoryMessage[]): ChatItem[] {
  return synchronizeTaskItemStates(history.map((item) => createChatItem({
    id: item.message_id,
    taskId: item.task_id ?? item.content[0]?.task_id ?? null,
    clientEventId: item.client_event_id,
    events: item.content,
    status: "complete",
    createdAt: item.created_at,
  })));
}

/** 创建一次新请求对应的任务项。 */
export function createChatItem(options: {
  id: string;
  clientEventId: string | null;
  taskId?: string | null;
  events?: ServerMessageEvent[];
  status?: ChatItem["status"];
  createdAt?: number;
  optimisticInput?: string;
}): ChatItem {
  return {
    id: options.id,
    taskId: options.taskId ?? null,
    clientEventId: options.clientEventId,
    events: options.events ?? [],
    status: options.status ?? "streaming",
    createdAt: options.createdAt ?? Date.now(),
    optimisticInput: options.optimisticInput,
    historyExpanded: false,
    interruptionStateByRequestId: {},
  };
}

/** 根据 SSE 事件追加或更新任务项，并同步同任务的中断恢复状态。 */
export function applyServerEvent(items: ChatItem[], event: ServerMessageEvent): ChatItem[] {
  const itemIndex = findTargetItemIndex(items, event);
  if (isEmptyCompletion(event, itemIndex === -1 ? null : items[itemIndex])) {
    return itemIndex === -1 ? items : items.filter((_, index) => index !== itemIndex);
  }

  const nextItems = itemIndex === -1
    ? [...items, createChatItem({
      id: event.event_id,
      clientEventId: stringValue(event.data.client_event_id) || null,
      taskId: event.task_id,
      events: [event],
      status: isTerminalEvent(event) ? "complete" : "streaming",
      createdAt: event.start_ts_ms,
    })]
    : items.map((item, index) => index === itemIndex ? appendEvent(item, event) : item);

  return synchronizeTaskItemStates(nextItems);
}

/** 更新单个任务项的历史展开状态，仅保存在当前页面内存中。 */
export function setHistoryExpanded(items: ChatItem[], itemId: string, expanded: boolean): ChatItem[] {
  return items.map((item) => item.id === itemId ? { ...item, historyExpanded: expanded } : item);
}

/** 将仍在生成的任务项标记为失败，并停止其底部运行状态。 */
export function markStreamingItemsError(items: ChatItem[], errorText: string, errorData: Record<string, unknown> = { message: errorText }): ChatItem[] {
  return items.map((item) => item.status === "streaming" ? {
    ...item,
    status: "error",
    error: { message: errorText, data: errorData },
  } : item);
}

/** 运行中的任务需要每秒触发一次重渲染，以刷新完成耗时。 */
export function refreshLiveCompletionDurations(items: ChatItem[]): ChatItem[] {
  return items.some((item) => item.status === "streaming") ? [...items] : items;
}

/** 从一个任务项的原始事件推导用户输入、折叠历史和当前输出。 */
export function buildChatItemPresentation(item: ChatItem, now = Date.now()): ChatItemPresentation {
  const historyEvents = item.events.filter((event) => event.is_history_event);
  const currentEvents = item.events.filter((event) => !event.is_history_event);
  const inputEvent = currentEvents.find((event) => event.type === "input") ?? null;
  return {
    inputBlock: inputEvent
      ? (isInterruptionResponseInput(inputEvent) ? null : inputEventToBlock(inputEvent))
      : optimisticInputToBlock(item),
    historyBlocks: [
      ...historyEvents
        .filter((event) => event.type === "input" && event.data.event_type === "user_input")
        .map(historyInputEventToBlock),
      ...eventsToBlocks(historyEvents.filter((event) => event.type !== "input"), item, false, now),
    ],
    currentBlocks: eventsToBlocks(currentEvents.filter((event) => event.type !== "input"), item, true, now),
  };
}

/** 判断是否存在需要定时刷新的运行中任务。 */
export function hasLiveCompletion(items: ChatItem[]): boolean {
  return items.some((item) => item.status === "streaming");
}

function appendEvent(item: ChatItem, event: ServerMessageEvent): ChatItem {
  return {
    ...item,
    taskId: item.taskId ?? event.task_id,
    clientEventId: item.clientEventId ?? (stringValue(event.data.client_event_id) || null),
    events: [...item.events, event],
    status: isTerminalEvent(event) ? "complete" : item.status === "pending" ? "streaming" : item.status,
  };
}

function findTargetItemIndex(items: ChatItem[], event: ServerMessageEvent): number {
  const clientEventId = stringValue(event.data.client_event_id);
  if (clientEventId) {
    const index = items.findIndex((item) => item.clientEventId === clientEventId && item.status !== "complete");
    if (index !== -1) return index;
  }
  if (event.task_id) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      if (
        items[index].taskId === event.task_id
        && (items[index].status !== "complete" || event.type === "complete")
      ) {
        return index;
      }
    }
  }
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (items[index].status === "streaming") return index;
  }
  return -1;
}

function isTerminalEvent(event: ServerMessageEvent): boolean {
  return event.type === "complete" || (event.type === "interrupted" && !event.is_history_event);
}

function isEmptyCompletion(event: ServerMessageEvent, item: ChatItem | null): boolean {
  return event.type === "complete"
    && event.task_id === null
    && (item === null || item.events.length === 0);
}

function synchronizeTaskItemStates(items: ChatItem[]): ChatItem[] {
  const taskId2interruptionState = new Map<string, Record<string, ResolvedInterruptionState>>();

  for (const item of items) {
    if (!item.taskId) continue;
    const interruptionStateByRequestId = taskId2interruptionState.get(item.taskId) ?? {};

    for (const event of item.events) {
      if (isInterruptionResponseInput(event)) {
        const resolvedState = resolveInterruptionState(event);
        if (resolvedState) interruptionStateByRequestId[resolvedState.requestId] = { title: resolvedState.title };
      }
    }

    taskId2interruptionState.set(item.taskId, interruptionStateByRequestId);
  }

  return items.map((item) => {
    if (!item.taskId) return item;
    return {
      ...item,
      interruptionStateByRequestId: taskId2interruptionState.get(item.taskId) ?? {},
    };
  });
}

function eventsToBlocks(events: ServerMessageEvent[], item: ChatItem, isCurrent: boolean, now: number): ChatMessageBlock[] {
  let blocks: ChatMessageBlock[] = [];
  for (const event of events) blocks = applyEventToBlocks(blocks, event);
  blocks = applyResolvedInterruptionStates(blocks, item);
  if (!isCurrent) return blocks;
  if (item.status === "streaming") return [...blocks, createThinkingBlock(), createLiveCompletionBlock(item.id, item.createdAt, now)];
  if (item.status === "error") return [...blocks, {
    type: "activity_label",
    id: `${item.id}:error`,
    title: "生成失败",
    subtitle: item.error?.message ?? "生成失败",
    icon: "alert",
    state: "normal",
    source: "error",
    metadata: { event: item.error?.data ?? {} },
  }];
  return blocks;
}

function applyEventToBlocks(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  if (event.type === "message_delta") return appendDelta(blocks, event);
  if (event.type === "message_final") return applyMessageFinal(blocks, event);
  if (event.type === "tool_call") return addToolLabel(blocks, event);
  if (event.type === "tool_result_start") return startToolLabel(blocks, event);
  if (event.type === "tool_result") return completeToolLabel(blocks, event);
  if (event.type === "context_compression_start" || event.type === "context_compression_final") return applyCompressionLabel(blocks, event);
  if (event.type === "activity_indicator") return addActivityLabel(blocks, event);
  if (event.type === "interrupted") return addInterruptedBlock(markApprovalPendingTool(blocks, event), event);
  if (event.type === "complete") return addCompletionBlock(blocks, event);
  return blocks;
}

function inputEventToBlock(event: ServerMessageEvent): TextBlock {
  return {
    type: "text",
    id: `${event.event_id}:input`,
    content: isInterruptionResponseInput(event) ? interruptionResponseText(event) : stringValue(event.data.user_input),
    metadata: buildEventMetadata(event),
  };
}

function historyInputEventToBlock(event: ServerMessageEvent): TextBlock {
  return {
    ...inputEventToBlock(event),
    display: "history_quote",
  };
}

function optimisticInputToBlock(item: ChatItem): TextBlock | null {
  if (!item.optimisticInput) return null;
  return { type: "text", id: `${item.id}:optimistic-input`, content: item.optimisticInput, metadata: { clientEventId: item.clientEventId, optimistic: true } };
}

function appendDelta(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  const id = messageBlockId(event);
  const content = stringValue(event.data.delta_content);
  const index = blocks.findIndex((block) => block.type === "text" && block.id === id);
  if (index === -1) return [...blocks, { type: "text", id, content, metadata: buildEventMetadata(event) }];
  return blocks.map((block, blockIndex) => blockIndex === index && block.type === "text" ? { ...block, content: `${block.content}${content}`, metadata: buildEventMetadata(event) } : block);
}

function applyMessageFinal(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  const block: TextBlock = { type: "text", id: messageBlockId(event), content: stringValue(event.data.content), reasoningContent: optionalStringValue(event.data.reasoning_content), metadata: buildEventMetadata(event) };
  const index = blocks.findIndex((current) => current.type === "text" && current.id === block.id);
  if (!block.content && !block.reasoningContent) {
    return index === -1 ? blocks : blocks.filter((_, blockIndex) => blockIndex !== index);
  }

  return index === -1 ? [...blocks, block] : blocks.map((current, currentIndex) => currentIndex === index ? block : current);
}

function addToolLabel(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  const toolCallId = stringValue(event.data.id);
  const params = event.data.parameters ?? event.data.parameters_text;
  return upsertBlock(blocks, {
    type: "activity_label",
    id: `${toolCallId}:tool`,
    title: optionalStringValue(event.data.name) ?? "工具调用",
    subtitle: params ? formatInlineData(params) : "等待工具返回",
    icon: "tool",
    state: "loading",
    source: "tool",
    sourceId: toolCallId,
    metadata: buildEventMetadata(event),
  });
}

function completeToolLabel(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  const toolCallId = stringValue(event.data.tool_call_id);
  const matchingIndex = findLastToolLabelIndex(blocks, toolCallId);
  const matchingBlock = matchingIndex === -1 ? null : blocks[matchingIndex] as ActivityLabelBlock;
  const status = optionalStringValue(event.data.status) ?? "done";
  const result: ActivityLabelBlock = {
    type: "activity_label",
    id: matchingBlock?.id ?? `${event.event_id}:tool-result`,
    title: matchingBlock?.title ?? "工具结果",
    subtitle: optionalStringValue(event.data.content) ?? "",
    durationMs: numberValue(event.data.duration_ms),
    icon: status === "success" ? "check" : "alert",
    state: "normal",
    source: "tool",
    sourceId: toolCallId,
    metadata: {
      ...matchingBlock?.metadata,
      resultEvent: event,
    },
  };
  return matchingIndex === -1 ? [...blocks, result] : blocks.map((block, index) => index === matchingIndex ? result : block);
}

function startToolLabel(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  const toolCallId = stringValue(event.data.id);
  const matchingIndex = findLastToolLabelIndex(blocks, toolCallId);
  const matchingBlock = matchingIndex === -1 ? null : blocks[matchingIndex] as ActivityLabelBlock;
  if (matchingBlock && !matchingBlock.approvalPending) {
    return blocks.map((block, index) => index === matchingIndex ? {
      ...matchingBlock,
      metadata: {
        ...matchingBlock.metadata,
        startEvent: event,
      },
    } : block);
  }

  const params = event.data.parameters ?? event.data.parameters_text;
  const block: ActivityLabelBlock = {
    type: "activity_label",
    id: `${event.event_id}:tool-start`,
    title: optionalStringValue(event.data.name) ?? "工具调用",
    subtitle: params ? formatInlineData(params) : "等待工具返回",
    icon: "tool",
    state: "loading",
    source: "tool",
    sourceId: toolCallId,
    metadata: buildEventMetadata(event),
  };
  return [...blocks, block];
}

function markApprovalPendingTool(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  const sourceToolId = getApprovalSourceToolId(event);
  if (!sourceToolId) return blocks;

  return blocks.map((block) => block.type === "activity_label"
    && block.source === "tool"
    && block.title === sourceToolId
    ? {
      ...block,
      state: "normal",
      approvalPending: true,
      metadata: {
        ...block.metadata,
        interruptionEvent: event,
      },
    }
    : block);
}

function getApprovalSourceToolId(event: ServerMessageEvent): string {
  if (!isAsyncHitlInterruption(event)) return "";
  return stringValue(objectValue(objectValue(event.data.params)?.data)?.source_tool_id);
}

function findLastToolLabelIndex(blocks: ChatMessageBlock[], toolCallId: string): number {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.type === "activity_label" && block.source === "tool" && block.sourceId === toolCallId) {
      return index;
    }
  }
  return -1;
}

function applyCompressionLabel(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  const final = event.type === "context_compression_final";
  const merged = Boolean(event.data.merged);
  const level = numberValue(event.data.level);
  return upsertBlock(blocks, { type: "activity_label", id: `${event.event_id}:compression`, title: "上下文整理", subtitle: final ? compressionFinalSubtitle(event, merged, level) : `L${level} · 正在整理会话上下文`, icon: final ? (merged ? "check" : "alert") : "database", state: final ? "normal" : "loading", source: "context_compression", sourceId: event.event_id, metadata: buildEventMetadata(event) });
}

function addActivityLabel(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  return upsertBlock(blocks, { type: "activity_label", id: `${event.event_id}:activity`, title: stringValue(event.data.title) || "处理中", subtitle: optionalStringValue(event.data.subtitle), icon: "sparkles", state: "loading", source: "activity", sourceId: event.event_id, metadata: buildEventMetadata(event) });
}

function addInterruptedBlock(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  return upsertBlock(blocks, { type: "interrupted", id: `${event.event_id}:interrupted`, requestId: stringValue(event.data.request_id), interruptionType: stringValue(event.data.interruption_type), title: interruptionTitle(event), actions: interruptionActions(event), responseOptions: interruptionResponseOptions(event), metadata: buildEventMetadata(event) });
}

function addCompletionBlock(blocks: ChatMessageBlock[], event: ServerMessageEvent): ChatMessageBlock[] {
  const block: CompletionBlock = { type: "completion", id: `${event.event_id}:complete`, durationMs: numberValue(event.data.duration_ms), metadata: buildEventMetadata(event) };
  const index = blocks.findIndex((current) => current.type === "completion");
  return index === -1 ? [...blocks, block] : blocks.map((current, currentIndex) => currentIndex === index ? block : current);
}

function applyResolvedInterruptionStates(blocks: ChatMessageBlock[], item: ChatItem): ChatMessageBlock[] {
  return blocks.map((block) => block.type === "interrupted" && item.interruptionStateByRequestId[block.requestId] ? { ...block, resolvedTitle: item.interruptionStateByRequestId[block.requestId].title, actions: [] } : block);
}

function upsertBlock(blocks: ChatMessageBlock[], block: Exclude<ChatMessageBlock, TextBlock | CompletionBlock>): ChatMessageBlock[] {
  const index = blocks.findIndex((current) => current.id === block.id);
  return index === -1 ? [...blocks, block] : blocks.map((current, currentIndex) => currentIndex === index ? block : current);
}

function createThinkingBlock(): ActivityLabelBlock {
  return { type: "activity_label", id: "thinking", title: "正在思考", state: "loading", source: "thinking" };
}

function createLiveCompletionBlock(id: string, startedAt: number, now: number): CompletionBlock {
  return { type: "completion", id: `${id}:completion`, durationMs: Math.max(0, now - startedAt), live: true, startedAt };
}

function resolveInterruptionState(event: ServerMessageEvent): { requestId: string; title: string } | null {
  const response = objectValue(event.data.interruption_response);
  const requestId = stringValue(response?.request_id);
  if (!requestId) return null;
  const option = stringValue(objectValue(objectValue(response?.response)?.data)?.option);
  return { requestId, title: option === "approve" ? "用户已同意" : option === "reject" ? "用户已拒绝" : "用户已重试" };
}

function buildEventMetadata(event: ServerMessageEvent): Record<string, unknown> {
  return { event, eventType: event.type, runId: event.run_id, taskId: event.task_id, eventId: event.event_id, parentEventId: event.parent_event_id, startTsMs: event.start_ts_ms, data: event.data };
}

function compressionFinalSubtitle(event: ServerMessageEvent, merged: boolean, level: number): string {
  const duration = formatDuration(numberValue(event.data.duration_ms));
  const levelPrefix = `L${level} · `;
  if (!merged) return `${levelPrefix}压缩失败 · ${duration}`;
  if (level === 1) return `${levelPrefix}已裁剪会话记忆 · ${duration}`;
  return `${levelPrefix}已压缩 ${numberValue(event.data.compressed_history_count)} 条历史 · ${duration}`;
}

function interruptionTitle(event: ServerMessageEvent): string {
  if (event.data.interruption_type === "_os_exception_resume") return "任务因运行异常暂停，是否需要重试？";
  if (isAsyncHitlInterruption(event)) return "请确认是否继续执行该操作?";
  return "任务正在等待外部工具返回结果，收到结果后将继续处理。";
}

function interruptionActions(event: ServerMessageEvent): InterruptedBlock["actions"] {
  if (event.data.interruption_type === "_os_exception_resume") return ["retry"];
  return isAsyncHitlInterruption(event) ? ["approve", "reject"] : [];
}

function isAsyncHitlInterruption(event: ServerMessageEvent): boolean {
  return event.data.interruption_type === "_os_async_tool_request" && objectValue(event.data.params)?.type === "async_hitl";
}

function interruptionResponseOptions(event: ServerMessageEvent): string[] {
  const options = objectValue(objectValue(event.data.params)?.data)?.options;
  return Array.isArray(options) ? options.filter((value): value is string => typeof value === "string") : [];
}

function interruptionResponseText(event: ServerMessageEvent): string {
  const response = objectValue(event.data.interruption_response);
  const data = objectValue(objectValue(response?.response)?.data);
  const option = stringValue(data?.option);
  if (option === "approve") return "已同意";
  if (option === "reject") return "已拒绝";
  return "重试";
}

function isInterruptionResponseInput(event: ServerMessageEvent): boolean {
  return event.type === "input" && event.data.event_type === "interruption_response";
}

function messageBlockId(event: ServerMessageEvent): string { return `${event.event_id}:message`; }
function stringValue(value: unknown): string { return typeof value === "string" ? value : ""; }
function optionalStringValue(value: unknown): string | null { return typeof value === "string" ? value : null; }
function numberValue(value: unknown): number { return typeof value === "number" && Number.isFinite(value) ? value : 0; }
function objectValue(value: unknown): Record<string, unknown> | null { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function formatInlineData(value: unknown): string { return typeof value === "string" ? value : JSON.stringify(value); }

export function formatDuration(durationMs: number): string {
  const normalizedDurationMs = Math.max(0, Math.round(durationMs));
  if (normalizedDurationMs < 1_000) {
    return `${normalizedDurationMs} ms`;
  }

  const totalSeconds = normalizedDurationMs / 1_000;
  if (totalSeconds < 60) {
    return `${totalSeconds.toFixed(1).replace(/\\.0$/, "")} s`;
  }

  const totalMinutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = Math.floor(totalSeconds % 60);
  if (totalMinutes < 60) {
    return remainingSeconds > 0 ? `${totalMinutes} min ${remainingSeconds} s` : `${totalMinutes} min`;
  }

  const totalHours = Math.floor(totalMinutes / 60);
  const remainingMinutes = totalMinutes % 60;
  return remainingMinutes > 0 ? `${totalHours} h ${remainingMinutes} min` : `${totalHours} h`;
}
