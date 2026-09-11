import { CSSProperties, FormEvent, KeyboardEvent, MouseEvent, UIEvent, memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertCircle,
  Bot,
  Check,
  ChevronsLeft,
  ChevronsRight,
  CircleStop,
  Copy,
  Database,
  FileJson,
  Loader2,
  Menu,
  PanelLeftOpen,
  Plus,
  Send,
  Sparkles,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import {
  createSession,
  deleteSession,
  getAppInfo,
  getHistory,
  isAbortError,
  streamCompletion,
  streamResume,
} from "./api";
import {
  applyServerEvent,
  buildChatItemPresentation,
  createChatItem,
  formatDuration,
  hasLiveCompletion,
  historyToItems,
  markStreamingItemsError,
  refreshLiveCompletionDurations,
  setHistoryExpanded,
} from "./chatState";
import type {
  ActivityLabelBlock,
  ChatItem,
  ChatMessageBlock,
  ConnectionState,
  InterruptedBlock,
  SseFrame,
  SendButtonState,
  ServerMessageEvent,
  Session,
  UserInfo,
} from "./types";

const ACTIVE_SESSION_KEY = "turtlemap.activeSessionId";
const LOCAL_AVATAR_URL = "/avatar.png";
/*
const ACTIVITY_DEMO_BLOCK: ActivityLabelBlock = {
  type: "activity_label",
  id: "activity-demo",
  title: "正在思考",
  // subtitle: "正在分析当前任务",
  // icon: "sparkles",
  state: "loading",
  source: "activity",
};
*/

type DetailSelection = {
  id: string;
  title: string;
  role: string;
  taskId: string | null;
  items: DetailItem[];
};

type DetailItem = {
  id: string;
  title: string;
  eventType?: string;
  status?: string;
  durationMs?: number;
  data: Record<string, unknown>;
};

type ErrorFrameInfo = {
  code: number | null;
  message: string;
  data: Record<string, unknown>;
};

export function App() {
  const [userInfo, setUserInfo] = useState<UserInfo | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [connectionState, setConnectionState] = useState<ConnectionState>("booting");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [detailPanelOpen, setDetailPanelOpen] = useState(false);
  const [detailWidth, setDetailWidth] = useState(360);
  const [detailSelection, setDetailSelection] = useState<DetailSelection | null>(null);
  const activeSessionRef = useRef<string | null>(null);
  const lastEventIdRef = useRef<string | null>(null);
  const messagesViewRef = useRef<HTMLElement | null>(null);
  const isComposingRef = useRef(false);
  const shouldFollowOutputRef = useRef(false);
  const pendingInitialScrollRef = useRef(false);
  const pendingHistoryScrollTopRef = useRef<number | null>(null);
  const eventFlushFrameRef = useRef<number | null>(null);
  const pendingServerEventsRef = useRef<ServerMessageEvent[]>([]);
  const processedEventKeysRef = useRef<Set<string>>(new Set());
  const streamFailedRef = useRef(false);
  const streamAbortControllerRef = useRef<AbortController | null>(null);

  const activeSession = sessions.find((session) => session.session_id === activeSessionId) ?? null;
  const visibleItems = items.filter((item) => item.events.some((event) => event.type !== "complete"));
  const isBusy = connectionState === "booting" || connectionState === "loading" || connectionState === "recovering" || connectionState === "streaming";
  const isSessionLoading = connectionState === "booting" || connectionState === "loading" || connectionState === "recovering";
  const isInputDisabled = connectionState === "booting" || connectionState === "loading" || connectionState === "recovering";
  const sendButtonState: SendButtonState = connectionState === "streaming" ? "generating" : "send";
  const canSend = sendButtonState === "send" && !isInputDisabled && inputValue.trim().length > 0;
  const hasRunningItem = hasLiveCompletion(items);

  useEffect(() => {
    activeSessionRef.current = activeSessionId;
  }, [activeSessionId]);

  useEffect(() => {
    void bootstrap();

    return () => {
      abortCurrentStream();
      if (eventFlushFrameRef.current !== null) {
        window.cancelAnimationFrame(eventFlushFrameRef.current);
      }
    };
  }, []);

  useLayoutEffect(() => {
    if (pendingHistoryScrollTopRef.current !== null && messagesViewRef.current !== null) {
      messagesViewRef.current.scrollTop = pendingHistoryScrollTopRef.current;
      pendingHistoryScrollTopRef.current = null;
      return;
    }

    if (pendingInitialScrollRef.current) {
      pendingInitialScrollRef.current = false;
      scrollMessagesToBottom();
      return;
    }

    if (!shouldFollowOutputRef.current || messagesViewRef.current === null) {
      return;
    }

    scrollMessagesToBottom();
  }, [items]);

  useEffect(() => {
    if (!hasRunningItem) {
      return;
    }

    const intervalId = window.setInterval(() => {
      setItems((current) => refreshLiveCompletionDurations(current));
    }, 1000);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [hasRunningItem]);

  function handleDetailResizeStart(event: MouseEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = detailWidth;

    function handleMouseMove(moveEvent: globalThis.MouseEvent) {
      const nextWidth = startWidth + startX - moveEvent.clientX;
      setDetailWidth(Math.min(Math.max(nextWidth, 280), 640));
    }

    function handleMouseUp() {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
      document.body.classList.remove("resizing-detail");
    }

    document.body.classList.add("resizing-detail");
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
  }

  async function bootstrap() {
    setConnectionState("booting");
    setErrorText(null);
    try {
      const info = await getAppInfo();
      setUserInfo(info.user_info);
      setSessions(info.session_info.sessions);

      const rememberedSessionId = window.localStorage.getItem(ACTIVE_SESSION_KEY);
      const defaultSession =
        info.session_info.sessions.find((session) => session.session_id === rememberedSessionId) ??
        info.session_info.active_session ??
        info.session_info.sessions[0] ??
        null;

      if (defaultSession === null) {
        setActiveSessionId(null);
        setItems([]);
        setDetailSelection(null);
        setConnectionState("idle");
        return;
      }

      await openSession(defaultSession.session_id, false);
    } catch (error) {
      setConnectionState("error");
      setErrorText(error instanceof Error ? error.message : "初始化失败");
    }
  }

  async function openSession(sessionId: string, closeSidebar = true) {
    abortCurrentStream();
    activeSessionRef.current = sessionId;
    clearPendingServerEvents();
    processedEventKeysRef.current = new Set();
    streamFailedRef.current = false;
    setActiveSessionId(sessionId);
    lastEventIdRef.current = null;
    setItems([]);
    setDetailSelection(null);
    setConnectionState("loading");
    setErrorText(null);
    window.localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);

    if (closeSidebar) {
      setSidebarOpen(false);
    }

    try {
      const history = await getHistory(sessionId);
      if (activeSessionRef.current !== sessionId) {
        return;
      }
      pendingInitialScrollRef.current = true;
      setItems(historyToItems(history));
      setConnectionState("recovering");
      let hasResumeDisplayEvent = false;
      const streamController = createStreamController();
      await streamResume(sessionId, (frame) => {
        handleSseFrame(frame, sessionId, {
          skipEmptyResumeComplete: !hasResumeDisplayEvent,
        });

        const event = parseChunkFrame(frame);
        if (event !== null && event.type !== "complete") {
          hasResumeDisplayEvent = true;
        }
      }, streamController.signal);
      clearStreamController(streamController);
      if (activeSessionRef.current === sessionId && !streamFailedRef.current) {
        setConnectionState("idle");
      }
    } catch (error) {
      if (isAbortError(error) || activeSessionRef.current !== sessionId) {
        return;
      }
      const errorInfo = buildClientErrorInfo(error);
      const pendingEvents = takePendingServerEvents();
      streamFailedRef.current = true;
      setConnectionState("error");
      setErrorText(errorInfo.message);
      setItems((current) => {
        const nextItems = pendingEvents.reduce(applyServerEvent, current);
        return markStreamingItemsError(nextItems, errorInfo.message, errorInfo.data);
      });
    }
  }

  async function handleCreateSession() {
    setErrorText(null);
    try {
      const session = await createSession();
      setSessions((current) => [session, ...current.filter((item) => item.session_id !== session.session_id)]);
      await openSession(session.session_id);
    } catch (error) {
      setConnectionState("error");
      setErrorText(error instanceof Error ? error.message : "创建会话失败");
    }
  }

  async function handleDeleteSession(sessionId: string) {
    setErrorText(null);
    try {
      await deleteSession(sessionId);
      const nextSessions = sessions.filter((session) => session.session_id !== sessionId);
      setSessions(nextSessions);

      if (activeSessionId === sessionId) {
        const nextSession = nextSessions[0] ?? null;
        if (nextSession === null) {
          setActiveSessionId(null);
          setItems([]);
          setDetailSelection(null);
          setConnectionState("idle");
          window.localStorage.removeItem(ACTIVE_SESSION_KEY);
        } else {
          await openSession(nextSession.session_id);
        }
      }
    } catch (error) {
      setConnectionState("error");
      setErrorText(error instanceof Error ? error.message : "删除会话失败");
    }
  }

  async function handleSubmit(event?: FormEvent) {
    event?.preventDefault();
    const query = inputValue.trim();
    if (!query || connectionState === "streaming") {
      return;
    }

    let sessionId = activeSessionId;
    let resolvedSessionId: string | null = null;
    let streamController: AbortController | null = null;
    setInputValue("");
    setErrorText(null);

    try {
      if (sessionId === null) {
        const session = await createSession();
        sessionId = session.session_id;
        setSessions((current) => [session, ...current]);
        setActiveSessionId(sessionId);
        activeSessionRef.current = sessionId;
        window.localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
      }

      const currentSessionId = sessionId;
      const clientEventId = crypto.randomUUID();
      resolvedSessionId = currentSessionId;
      streamController = createStreamController();
      streamFailedRef.current = false;
      shouldFollowOutputRef.current = true;
      setItems((current) => [...current, createChatItem({
        id: clientEventId,
        clientEventId,
        optimisticInput: query,
      })]);
      setConnectionState("streaming");

      await streamCompletion(
        currentSessionId,
        {
          query,
          client_event_id: clientEventId,
          last_event_id: null,
        },
        (frame) => handleSseFrame(frame, currentSessionId),
        streamController.signal,
      );
      clearStreamController(streamController);

      if (activeSessionRef.current === currentSessionId && !streamFailedRef.current) {
        setConnectionState("idle");
      }
    } catch (error) {
      if (isAbortError(error) || (resolvedSessionId !== null && activeSessionRef.current !== resolvedSessionId)) {
        return;
      }

      const errorInfo = buildClientErrorInfo(error);
      const pendingEvents = takePendingServerEvents();
      streamFailedRef.current = true;
      setConnectionState("error");
      setErrorText(errorInfo.message);
      setItems((current) => {
        const nextItems = pendingEvents.reduce(applyServerEvent, current);
        return markStreamingItemsError(nextItems, errorInfo.message, errorInfo.data);
      });
    }
  }

  async function handleInterruptionAction(
    block: InterruptedBlock,
    action: InterruptedBlock["actions"][number],
  ) {
    const sessionId = activeSessionRef.current;
    const interruptionResponse = buildInterruptionResponse(block, action);
    if (sessionId === null || interruptionResponse === null || connectionState === "streaming") {
      if (interruptionResponse === null) {
        setErrorText("中断响应参数不完整，无法继续处理");
      }
      return;
    }

    const streamController = createStreamController();
    streamFailedRef.current = false;
    shouldFollowOutputRef.current = true;
    setErrorText(null);
    setConnectionState("streaming");
    const clientEventId = crypto.randomUUID();
    setItems((current) => [...current, createChatItem({
      id: clientEventId,
      clientEventId,
    })]);

    try {
      await streamCompletion(
        sessionId,
        {
          event_type: "interruption_response",
          query: null,
          interruption_response: interruptionResponse,
          client_event_id: clientEventId,
          last_event_id: null,
        },
        (frame) => handleSseFrame(frame, sessionId),
        streamController.signal,
      );
      clearStreamController(streamController);

      if (activeSessionRef.current === sessionId && !streamFailedRef.current) {
        setConnectionState("idle");
      }
    } catch (error) {
      if (isAbortError(error) || activeSessionRef.current !== sessionId) {
        return;
      }

      const errorInfo = buildClientErrorInfo(error);
      const pendingEvents = takePendingServerEvents();
      streamFailedRef.current = true;
      setConnectionState("error");
      setErrorText(errorInfo.message);
      setItems((current) => {
        const nextItems = pendingEvents.reduce(applyServerEvent, current);
        return markStreamingItemsError(nextItems, errorInfo.message, errorInfo.data);
      });
    }
  }

  function handleMessagesScroll(event: UIEvent<HTMLElement>) {
    const target = event.currentTarget;
    shouldFollowOutputRef.current = isNearScrollBottom(target);
  }

  function scrollMessagesToBottom() {
    const container = messagesViewRef.current;
    if (container === null) {
      return;
    }

    container.scrollTo({
      top: container.scrollHeight,
      behavior: "auto",
    });
  }

  function isNearScrollBottom(element: HTMLElement): boolean {
    const distanceToBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
    return distanceToBottom < 4
  }

  function handleSseFrame(
    frame: SseFrame,
    sessionId: string,
    options: { skipEmptyResumeComplete?: boolean } = {},
  ) {
    if (activeSessionRef.current !== sessionId) {
      return;
    }

    if (frame.id && frame.id !== "null") {
      lastEventIdRef.current = frame.id;
    }

    if (frame.event === "start") {
      handleStartFrame(frame);
      return;
    }

    if (frame.event === "end") {
      flushPendingServerEvents();
      if (!streamFailedRef.current) {
        setConnectionState("idle");
      }
      return;
    }

    if (frame.event === "error") {
      const errorInfo = parseErrorFrame(frame);
      const pendingEvents = takePendingServerEvents();
      if (errorInfo.code === 20003 && activeSessionRef.current) {
        setItems((current) => pendingEvents.reduce(applyServerEvent, current));
        void openSession(activeSessionRef.current, false);
        return;
      }

      streamFailedRef.current = true;
      setConnectionState("error");
      setErrorText(errorInfo.message);
      setItems((current) => {
        const nextItems = pendingEvents.reduce(applyServerEvent, current);
        return markStreamingItemsError(nextItems, errorInfo.message, errorInfo.data);
      });
      return;
    }

    if (frame.event !== "chunk" && frame.event !== "stream_chunk") {
      return;
    }

    const event = parseChunkFrame(frame);
    if (event === null) {
      return;
    }
    if (event.type === "complete" && event.task_id === null) {
      return;
    }
    if (options.skipEmptyResumeComplete && event.type === "complete") {
      return;
    }

    const eventKey = `${event.run_id}:${event.is_history_event ? "history" : "current"}:${frame.id ?? `${event.type}:${event.event_id}:${event.start_ts_ms}`}`;
    if (processedEventKeysRef.current.has(eventKey)) {
      return;
    }
    processedEventKeysRef.current.add(eventKey);
    enqueueServerEvent(event);
  }

  function enqueueServerEvent(event: ServerMessageEvent) {
    pendingServerEventsRef.current.push(event);
    if (eventFlushFrameRef.current !== null) {
      return;
    }

    eventFlushFrameRef.current = window.requestAnimationFrame(() => {
      eventFlushFrameRef.current = null;
      flushPendingServerEvents();
    });
  }

  function flushPendingServerEvents() {
    const pendingEvents = takePendingServerEvents();
    if (pendingEvents.length === 0) {
      return;
    }

    setItems((current) => pendingEvents.reduce(applyServerEvent, current));
  }

  function takePendingServerEvents(): ServerMessageEvent[] {
    if (eventFlushFrameRef.current !== null) {
      window.cancelAnimationFrame(eventFlushFrameRef.current);
      eventFlushFrameRef.current = null;
    }

    return pendingServerEventsRef.current.splice(0);
  }

  function clearPendingServerEvents() {
    takePendingServerEvents();
  }

  function createStreamController() {
    abortCurrentStream();
    const controller = new AbortController();
    streamAbortControllerRef.current = controller;
    return controller;
  }

  function clearStreamController(controller: AbortController) {
    if (streamAbortControllerRef.current === controller) {
      streamAbortControllerRef.current = null;
    }
  }

  function abortCurrentStream() {
    streamAbortControllerRef.current?.abort();
    streamAbortControllerRef.current = null;
  }

  function parseChunkFrame(frame: SseFrame): ServerMessageEvent | null {
    if (frame.event !== "chunk" && frame.event !== "stream_chunk") {
      return null;
    }

    return JSON.parse(frame.data) as ServerMessageEvent;
  }

  function handleStartFrame(frame: SseFrame) {
    const payload = JSON.parse(frame.data) as { code: number; message: string; success: boolean };
    if (payload.code === 20003 && activeSessionRef.current) {
      void openSession(activeSessionRef.current, false);
    }
  }

  function parseErrorFrame(frame: SseFrame): ErrorFrameInfo {
    try {
      const payload = JSON.parse(frame.data) as { code?: number; message?: string; data?: unknown };
      return {
        code: typeof payload.code === "number" ? payload.code : null,
        message: payload.message || "服务端返回错误",
        data: objectRecordValue(payload.data) ?? objectRecordValue(payload) ?? {
          message: payload.message || "服务端返回错误",
        },
      };
    } catch {
      return {
        code: null,
        message: "服务端返回错误",
        data: {
          raw: frame.data,
        },
      };
    }
  }

  function buildClientErrorInfo(error: unknown): ErrorFrameInfo {
    const message = error instanceof Error ? error.message : "生成失败";
    return {
      code: null,
      message,
      data: {
        message,
      },
    };
  }

  function handleInputKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (isComposingRef.current || event.nativeEvent.isComposing) {
      return;
    }

    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
    }
  }

  function handleMessagesViewClick(event: MouseEvent<HTMLElement>) {
    const target = event.target;
    if (target instanceof Element && target.closest(".inspectable-block, .history-toggle")) {
      return;
    }

    setDetailSelection(null);
    setDetailPanelOpen(false);
  }

  return (
    <div
      className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${detailPanelOpen ? "detail-open" : ""}`}
      style={{ "--detail-width": `${detailWidth}px` } as CSSProperties}
    >
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="sidebar-header">
          <div className="brand-block">
            <button className="brand-image-button" type="button" aria-label="TurtleMap" />
            <div className="brand-caption">AgentOS Framework</div>
          </div>
          <button className="icon-button hide-desktop" type="button" onClick={() => setSidebarOpen(false)} aria-label="收起会话栏">
            <ChevronsLeft size={18} />
          </button>
        </div>

        <button className="new-session-button" type="button" onClick={handleCreateSession}>
          <Plus size={18} />
          新建会话
        </button>

        <div className="session-list">
          {sessions.length === 0 ? (
            <div className="empty-sidebar">暂无会话</div>
          ) : (
            sessions.map((session) => (
              <button
                className={`session-item ${session.session_id === activeSessionId ? "active" : ""}`}
                type="button"
                key={session.session_id}
                onClick={() => void openSession(session.session_id)}
              >
                <span>{session.title}</span>
                <span
                  className="delete-session"
                  role="button"
                  tabIndex={0}
                  onClick={(event) => {
                    event.stopPropagation();
                    void handleDeleteSession(session.session_id);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.stopPropagation();
                      void handleDeleteSession(session.session_id);
                    }
                  }}
                  aria-label="删除会话"
                >
                  <Trash2 size={15} />
                </span>
              </button>
            ))
          )}
        </div>

        <div className="user-card">
          <img src={LOCAL_AVATAR_URL} alt="" />
          <div>
            <div className="user-name">{userInfo?.name ?? "YaHaoo"}</div>
            {/* <div className="user-state">{statusLabel(connectionState)}</div> */}
          </div>
        </div>
      </aside>

      <main className="chat-panel">
        <header className="topbar">
          <button className="icon-button hide-desktop" type="button" onClick={() => setSidebarOpen(true)} aria-label="打开会话栏">
            <Menu size={20} />
          </button>
          <button
            className="icon-button show-desktop"
            type="button"
            onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
            aria-label={sidebarCollapsed ? "展开会话栏" : "收起会话栏"}
          >
            {sidebarCollapsed ? <PanelLeftOpen size={19} /> : <ChevronsLeft size={19} />}
          </button>
          <div className="topbar-title">
            <div>{activeSession?.title ?? "新的会话"}</div>
            <span>{activeSession ? activeSession.session_id : "准备开始"}</span>
          </div>
          <div className={`status-pill status-${connectionState}`}>
            <span />
            {statusLabel(connectionState)}
          </div>
        </header>

        {/*
          <div className="activity-demo">
            <div className="activity-demo-inner">
              <ActivityLabel block={ACTIVITY_DEMO_BLOCK} selected={false} onSelect={() => {}} />
            </div>
          </div>
        */}

        {errorText && (
          <div className="error-banner">
            <AlertCircle size={18} />
            <span>{errorText}</span>
            <button type="button" onClick={() => void bootstrap()}>重试</button>
          </div>
        )}

        <section
          className="messages-view"
          ref={messagesViewRef}
          onScroll={handleMessagesScroll}
          onClick={handleMessagesViewClick}
        >
          {visibleItems.length === 0 && isSessionLoading ? (
            <ChatLoadingState label={statusLabel(connectionState)} />
          ) : visibleItems.length === 0 ? (
            <EmptyChat onPrompt={(value) => setInputValue(value)} />
          ) : (
            visibleItems.map((item, index) => (
              <TaskItemView
                key={item.id}
                item={item}
                mergeCompletionIntoInterruption={isInterruptedThenRecovered(item, visibleItems[index + 1])}
                selectedId={detailSelection?.id ?? null}
                onSelectBlock={(block) => {
                  setDetailSelection(buildDetailSelection(item, block));
                  setDetailPanelOpen(true);
                }}
                onToggleHistory={(expanded) => {
                  // 用户主动查看历史时，保留当前阅读位置，不再跟随流式输出到底部。
                  shouldFollowOutputRef.current = false;
                  pendingHistoryScrollTopRef.current = messagesViewRef.current?.scrollTop ?? null;
                  setItems((current) => setHistoryExpanded(current, item.id, expanded));
                }}
                onInterruptionAction={(block, action) => {
                  void handleInterruptionAction(block, action);
                }}
              />
            ))
          )}
        </section>

        <form className="composer-wrap" onSubmit={handleSubmit}>
          <div className="composer">
            <textarea
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={handleInputKeyDown}
              onCompositionStart={() => {
                isComposingRef.current = true;
              }}
              onCompositionEnd={() => {
                isComposingRef.current = false;
              }}
              placeholder={isBusy ? "正在同步会话状态..." : "给 TurtleMap 发送消息"}
              rows={1}
              disabled={isInputDisabled}
            />
            <button
              className={`send-button send-${sendButtonState}`}
              type="submit"
              disabled={!canSend}
              aria-label={sendButtonState === "generating" ? "生成中" : "发送"}
            >
              {sendButtonState === "generating" ? <Loader2 size={18} className="spin" /> : <Send size={18} />}
            </button>
            <button className="stop-button" type="button" disabled aria-label="停止生成">
              <CircleStop size={18} />
            </button>
          </div>
          <div className="composer-hint">Enter 发送，Shift + Enter 换行</div>
        </form>
      </main>

      <DetailPanel
        selection={detailSelection}
        onClose={() => setDetailPanelOpen(false)}
        onResizeStart={handleDetailResizeStart}
      />
    </div>
  );
}

function ChatLoadingState({ label }: { label: string }) {
  return (
    <div className="chat-loading-state">
      <Loader2 size={22} className="spin" />
      <span>{label}</span>
    </div>
  );
}

function EmptyChat({ onPrompt }: { onPrompt: (value: string) => void }) {
  const prompts = ["帮我总结当前 TurtleMap 的运行流程", "演示一次带工具调用的回答", "解释会话恢复和 SSE 续接逻辑"];
  return (
    <div className="empty-chat">
      <div className="empty-mark">
        <img src="/images/chatgpt-reference-square.png" alt="" />
      </div>
      <h1>TurtleMap Web</h1>
      <p>一个面向 Agent Runtime 调试和对话体验的工作台。</p>
      <div className="prompt-grid">
        {prompts.map((prompt) => (
          <button type="button" key={prompt} onClick={() => onPrompt(prompt)}>
            {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}

function TaskItemView({
  item,
  mergeCompletionIntoInterruption,
  selectedId,
  onSelectBlock,
  onToggleHistory,
  onInterruptionAction,
}: {
  item: ChatItem;
  mergeCompletionIntoInterruption: boolean;
  selectedId: string | null;
  onSelectBlock: (block: ChatMessageBlock) => void;
  onToggleHistory: (expanded: boolean) => void;
  onInterruptionAction: (
    block: InterruptedBlock,
    action: InterruptedBlock["actions"][number],
  ) => void;
}) {
  const presentation = buildChatItemPresentation(item);
  const historyCount = presentation.historyBlocks.length;
  const completionDurationMs = mergeCompletionIntoInterruption
    ? presentation.currentBlocks.find((block) => block.type === "completion")?.durationMs
    : undefined;
  const currentBlocks = mergeCompletionIntoInterruption
    ? presentation.currentBlocks
      .filter((block) => block.type !== "completion")
      .map((block) => block.type === "interrupted" ? { ...block, durationMs: completionDurationMs } : block)
    : presentation.currentBlocks;

  return (
    <article className="task-item">
      {presentation.inputBlock && (
        <div className="message-row role-user">
          <div className="message-body">
            <MessageBlockView
              block={presentation.inputBlock}
              selected={selectedId === `${item.id}:${presentation.inputBlock.id}`}
              onSelect={() => onSelectBlock(presentation.inputBlock as ChatMessageBlock)}
              onInterruptionAction={onInterruptionAction}
            />
          </div>
        </div>
      )}
      <div className="message-body">
        {historyCount > 0 && (
          <div className="history-section">
            <button
              className="history-toggle"
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onToggleHistory(!item.historyExpanded);
              }}
            >
              {item.historyExpanded ? "收起任务" : `展开任务（${historyCount}）`}
            </button>
            {item.historyExpanded && (
              <div className="history-content">
                {presentation.historyBlocks.map((block) => (
                  <MessageBlockView
                    key={`history:${block.id}`}
                    block={block}
                    selected={selectedId === `${item.id}:${block.id}`}
                    onSelect={() => onSelectBlock(block)}
                    onInterruptionAction={onInterruptionAction}
                  />
                ))}
              </div>
            )}
          </div>
        )}
        {currentBlocks.map((block) => (
          <MessageBlockView
            key={`current:${block.id}`}
            block={block}
            selected={selectedId === `${item.id}:${block.id}`}
            onSelect={() => onSelectBlock(block)}
            onInterruptionAction={onInterruptionAction}
          />
        ))}
      </div>
    </article>
  );
}

function MessageBlockView({
  block,
  selected,
  onSelect,
  onInterruptionAction,
}: {
  block: ChatMessageBlock;
  selected: boolean;
  onSelect: () => void;
  onInterruptionAction: (
    block: InterruptedBlock,
    action: InterruptedBlock["actions"][number],
  ) => void;
}) {
  if (block.type === "text") {
    return (
      <button className={`inspectable-block text-block ${block.display === "history_quote" ? "history-input" : ""} ${selected ? "selected" : ""}`} type="button" onClick={handleBlockClick}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            code({ className, children, ...props }) {
              const language = /language-([^\s]+)/.exec(className ?? "")?.[1];
              if (!language) {
                return <code className={className} {...props}>{children}</code>;
              }

              const code = String(children).replace(/\n$/, "");
              return (
                <code className={`markdown-code language-${language}`} data-language={language} {...props}>
                  {code.split("\n").map((line, index) => (
                    <span className="markdown-code-line" key={`${index}:${line}`}>
                      <span className="markdown-code-line-number">{index + 1}</span>
                      <span className="markdown-code-line-content">{highlightCodeLine(line)}</span>
                    </span>
                  ))}
                </code>
              );
            },
          }}
        >
          {block.content || " "}
        </ReactMarkdown>
      </button>
    );
  }

  if (block.type === "completion") {
    return (
      <button className={`inspectable-block completion-block ${selected ? "selected" : ""}`} type="button" onClick={handleBlockClick}>
        用时 {formatDuration(block.durationMs)}
      </button>
    );
  }

  if (block.type === "interrupted") {
    return (
      <InterruptedCard
        block={block}
        selected={selected}
        onSelect={onSelect}
        onAction={onInterruptionAction}
      />
    );
  }

  return <ActivityLabel block={block} selected={selected} onSelect={handleBlockClick} />;

  function handleBlockClick(event: MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    onSelect();
  }
}

function InterruptedCard({
  block,
  selected,
  onSelect,
  onAction,
}: {
  block: InterruptedBlock;
  selected: boolean;
  onSelect: () => void;
  onAction: (
    block: InterruptedBlock,
    action: InterruptedBlock["actions"][number],
  ) => void;
}) {
  return (
    <section
      className={`inspectable-block interruption-card ${block.resolvedTitle ? "interruption-card-resolved" : ""} ${selected ? "selected" : ""}`}
      onClick={handleCardClick}
    >
      <div className={`interruption-title ${block.resolvedTitle ? "interruption-resolved" : ""}`}>
        {block.resolvedTitle ?? block.title}
        {block.durationMs !== undefined && (
          <span className="interruption-duration"> · {formatDuration(block.durationMs)}</span>
        )}
      </div>
      {block.actions.length > 0 && (
        <div className="interruption-actions">
          {block.actions.map((action) => (
            <button
              className={`interruption-action interruption-action-${action}`}
              key={action}
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onAction(block, action);
              }}
            >
              {interruptionActionLabel(action)}
            </button>
          ))}
        </div>
      )}
    </section>
  );

  function handleCardClick(event: MouseEvent<HTMLElement>) {
    event.stopPropagation();
    onSelect();
  }
}

const ActivityLabel = memo(function ActivityLabel({
  block,
  selected,
  onSelect,
}: {
  block: ActivityLabelBlock;
  selected: boolean;
  onSelect: (event: MouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <button className={`inspectable-block activity-label activity-${block.state} activity-${block.source} ${block.icon ? `activity-icon-${block.icon}` : ""} ${selected ? "selected" : ""}`} type="button" onClick={onSelect}>
      {block.icon && <div className="activity-icon">{activityIcon(block)}</div>}
      <div className="activity-copy">
        <div className="activity-title">{block.title}</div>
        {block.approvalPending && <div className="activity-approval">待审批</div>}
        {block.subtitle && <div className="activity-subtitle">{block.subtitle}</div>}
        {block.durationMs !== undefined && <div className="activity-duration">{formatDuration(block.durationMs)}</div>}
      </div>
    </button>
  );
}, (prevProps, nextProps) => (
  prevProps.block === nextProps.block
  && prevProps.selected === nextProps.selected
));

function DetailPanel({
  selection,
  onClose,
  onResizeStart,
}: {
  selection: DetailSelection | null;
  onClose: () => void;
  onResizeStart: (event: MouseEvent<HTMLDivElement>) => void;
}) {
  return (
    <aside className="detail-panel">
      <div className="detail-resize-handle" onMouseDown={onResizeStart} />
      <div className="detail-header">
        <div className="detail-kicker">字段详情</div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="关闭详情">
          <X size={18} />
        </button>
      </div>
      {selection === null ? (
        <div className="detail-empty">
          <FileJson size={22} />
          <div>选择一段消息查看字段</div>
          <span>文本、工具状态和完成标记都可以打开详情。</span>
        </div>
      ) : (
        <>
          <div className="detail-title">{selection.title}</div>
          <div className="detail-subtitle">
            <div>{selection.role}</div>
            <div>
              <code>{selection.taskId ?? "null"}</code>
            </div>
          </div>
          <div className="detail-list">
            {selection.items.map((item) => (
              <section className="detail-item" key={item.id}>
                <div className="detail-item-title">
                  {item.eventType ? (
                    <>
                      事件类型：<code>{item.eventType}</code>
                    </>
                  ) : item.title}
                </div>
                {(item.status || item.durationMs !== undefined) && (
                  <div className="detail-badges">
                    {item.status && (
                      <div className={`detail-status detail-status-${normalizeDetailStatus(item.status)}`}>{item.status}</div>
                    )}
                    {item.durationMs !== undefined && (
                      <div className="detail-duration">耗时 {formatDuration(item.durationMs)}</div>
                    )}
                  </div>
                )}
                <JsonViewer data={item.data} />
                <ContextCompressionMemoryBox item={item} />
              </section>
            ))}
          </div>
        </>
      )}
    </aside>
  );
}

function ContextCompressionMemoryBox({ item }: { item: DetailItem }) {
  if (item.eventType !== "context_compression_final") {
    return null;
  }

  const payload = item.data.data;
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }

  const memory = readString((payload as Record<string, unknown>).compressed_mid_term_memory);
  if (!memory) {
    return null;
  }

  return (
    <div className="detail-memory-box">
      <div className="detail-memory-title">会话记忆</div>
      <div className="detail-memory-content">{memory}</div>
    </div>
  );
}

function JsonViewer({ data }: { data: Record<string, unknown> }) {
  const jsonText = JSON.stringify(data, null, 2);
  const lines = jsonText.split("\n");
  return (
    <div className="json-viewer">
      <div className="json-toolbar">
        <span>JSON · {lines.length} 行</span>
        <button type="button" onClick={() => void navigator.clipboard.writeText(jsonText)}>
          <Copy size={14} />
          复制
        </button>
      </div>
      <pre>
        {lines.map((line, index) => (
          <div className="json-line" key={`${index}:${line}`}>
            <span className="json-line-number">{index + 1}</span>
            <code>{highlightJsonLine(line)}</code>
          </div>
        ))}
      </pre>
    </div>
  );
}

function buildDetailSelection(item: ChatItem, block: ChatMessageBlock): DetailSelection {
  const fallbackData = {
    itemId: item.id,
    taskId: item.taskId,
    status: item.status,
    createdAt: item.createdAt,
    block,
  };
  const items = getDetailItems(block, fallbackData);

  return {
    id: `${item.id}:${block.id}`,
    title: detailTitle(block),
    role: block.type === "text" && block.id.endsWith(":input") ? "user" : "assistant",
    taskId: item.taskId,
    items,
  };
}

function getDurationMs(data: Record<string, unknown>): number | undefined {
  const payload = data.data;
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return undefined;
  }

  const durationMs = (payload as Record<string, unknown>).duration_ms;
  return typeof durationMs === "number" && Number.isFinite(durationMs) ? durationMs : undefined;
}

function getDetailItems(
  block: ChatMessageBlock,
  fallbackData: Record<string, unknown>,
): DetailItem[] {
  const metadata = block.metadata;
  const eventData = getMetadataEvent(metadata);
  if (block.type === "activity_label" && block.source === "tool") {
    const resultEvent = getMetadataResultEvent(metadata);
    return [
      eventData
        ? buildDetailItem(readString(eventData.type) || "tool_call", eventData)
        : buildDetailItem("tool_call", fallbackData),
      ...(resultEvent ? [buildDetailItem("tool_result", resultEvent)] : []),
    ];
  }

  if (eventData) {
    return [buildDetailItem(detailTitle(block), eventData)];
  }
  return [buildDetailItem(detailTitle(block), fallbackData)];
}

function buildDetailItem(title: string, data: Record<string, unknown>): DetailItem {
  const eventType = readString(data.type);
  return {
    id: `${eventType || title}:${readString(data.event_id) || eventType || "local"}`,
    title,
    eventType: eventType || undefined,
    status: getDetailStatus(eventType, data),
    durationMs: getDurationMs(data),
    data,
  };
}

function getDetailStatus(eventType: string, data: Record<string, unknown>): string | undefined {
  if (eventType === "tool_result") {
    return getToolResultStatus(data);
  }

  if (eventType === "context_compression_final") {
    return getContextCompressionStatus(data);
  }

  return undefined;
}

function getToolResultStatus(data: Record<string, unknown>): string | undefined {
  const payload = data.data;
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return undefined;
  }

  const status = readString((payload as Record<string, unknown>).status);
  return status || undefined;
}

function getContextCompressionStatus(data: Record<string, unknown>): string | undefined {
  const payload = data.data;
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return undefined;
  }

  const compressionData = payload as Record<string, unknown>;
  if (compressionData.success === true) {
    return "success";
  }

  const error = readString(compressionData.error);
  return error ? "error" : undefined;
}

function normalizeDetailStatus(status: string): "success" | "error" {
  return status === "success" ? "success" : "error";
}

function getMetadataEvent(metadata: Record<string, unknown> | undefined): Record<string, unknown> | null {
  if (metadata?.event !== null && typeof metadata?.event === "object" && !Array.isArray(metadata.event)) {
    return metadata.event as Record<string, unknown>;
  }
  return null;
}

function getMetadataResultEvent(metadata: Record<string, unknown> | undefined): Record<string, unknown> | null {
  if (metadata?.resultEvent !== null && typeof metadata?.resultEvent === "object" && !Array.isArray(metadata.resultEvent)) {
    return metadata.resultEvent as Record<string, unknown>;
  }
  return null;
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function objectRecordValue(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function highlightJsonLine(line: string) {
  const parts = line.split(/("(?:\\.|[^"\\])*"(?=\s*:)|"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?|true|false|null)/g);
  return parts.map((part, index) => {
    if (!part) {
      return null;
    }

    if (/^"(?:\\.|[^"\\])*"$/.test(part) && parts[index + 1]?.trimStart().startsWith(":")) {
      return <span className="json-key" key={`${index}:${part}`}>{part}</span>;
    }

    if (/^"(?:\\.|[^"\\])*"$/.test(part)) {
      return <span className="json-string" key={`${index}:${part}`}>{part}</span>;
    }

    if (/^-?\d+(?:\.\d+)?$/.test(part)) {
      return <span className="json-number" key={`${index}:${part}`}>{part}</span>;
    }

    if (part === "true" || part === "false") {
      return <span className="json-boolean" key={`${index}:${part}`}>{part}</span>;
    }

    if (part === "null") {
      return <span className="json-null" key={`${index}:${part}`}>{part}</span>;
    }

    return <span key={`${index}:${part}`}>{part}</span>;
  });
}

function highlightCodeLine(line: string) {
  const tokenPattern = /(\/\/.*$|#.*$|\/\*.*?\*\/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\b(?:abstract|and|as|async|await|break|case|catch|class|const|continue|def|default|else|enum|except|export|extends|false|final|finally|for|from|function|if|import|in|interface|let|match|new|nil|none|not|null|or|pass|private|protected|public|raise|return|self|static|struct|switch|throw|true|try|type|undefined|var|void|while|with|yield)\b|\b\d+(?:\.\d+)?\b)/gi;
  return line.split(tokenPattern).map((part, index) => {
    if (!part) {
      return null;
    }

    if (/^(\/\/|#|\/\*)/.test(part)) {
      return <span className="code-comment" key={`${index}:${part}`}>{part}</span>;
    }

    if (/^["'`]/.test(part)) {
      return <span className="code-string" key={`${index}:${part}`}>{part}</span>;
    }

    if (/^\d/.test(part)) {
      return <span className="code-number" key={`${index}:${part}`}>{part}</span>;
    }

    if (/^(true|false|null|none|nil|undefined)$/i.test(part)) {
      return <span className="code-literal" key={`${index}:${part}`}>{part}</span>;
    }

    if (/^[a-z]+$/i.test(part)) {
      return <span className="code-keyword" key={`${index}:${part}`}>{part}</span>;
    }

    return <span key={`${index}:${part}`}>{part}</span>;
  });
}

function detailTitle(block: ChatMessageBlock): string {
  if (block.type === "text") {
    return "文本块";
  }
  if (block.type === "interrupted") {
    return "任务中断";
  }
  if (block.type === "completion") {
    return "完成信息";
  }
  if (block.source === "tool") {
    return "工具调用";
  }
  if (block.source === "context_compression") {
    return "上下文整理";
  }
  if (block.source === "thinking") {
    return "思考状态";
  }
  if (block.source === "error") {
    return "错误信息";
  }
  return "活动状态";
}

function isInterruptedThenRecovered(item: ChatItem, nextItem: ChatItem | undefined): boolean {
  if (!nextItem || !item.taskId || item.taskId !== nextItem.taskId) {
    return false;
  }

  const hasInterruptedEvent = item.events.some(
    (event) => !event.is_history_event && event.type === "interrupted",
  );
  const hasRecoveryInput = nextItem.events.some(
    (event) => !event.is_history_event
      && event.type === "input"
      && event.data.event_type === "interruption_response",
  );
  return hasInterruptedEvent && hasRecoveryInput;
}

function interruptionActionLabel(action: InterruptedBlock["actions"][number]): string {
  if (action === "retry") {
    return "重试";
  }
  if (action === "approve") {
    return "同意";
  }
  return "拒绝";
}

function buildInterruptionResponse(
  block: InterruptedBlock,
  action: InterruptedBlock["actions"][number],
): {
  request_id: string;
  request_type: string;
  response: Record<string, unknown>;
} | null {
  if (!block.requestId || !block.interruptionType) {
    return null;
  }

  if (block.interruptionType === "_os_exception_resume" && action === "retry") {
    return {
      request_id: block.requestId,
      request_type: block.interruptionType,
      response: {},
    };
  }

  if (block.interruptionType !== "_os_async_tool_request") {
    return null;
  }

  const optionIndex = action === "approve" ? 0 : action === "reject" ? 1 : -1;
  const option = block.responseOptions[optionIndex];
  if (!option) {
    return null;
  }

  return {
    request_id: block.requestId,
    request_type: block.interruptionType,
    response: {
      data: {
        option,
      },
    },
  };
}

function activityIcon(block: ActivityLabelBlock) {
  if (block.icon === "tool") {
    return <Wrench size={16} />;
  }
  if (block.icon === "database") {
    return <Database size={16} />;
  }
  if (block.icon === "check") {
    return <Check size={16} />;
  }
  if (block.icon === "alert") {
    return <AlertCircle size={16} />;
  }
  return <Sparkles size={16} />;
}

function statusLabel(state: ConnectionState): string {
  if (state === "booting") {
    return "初始化";
  }
  if (state === "loading") {
    return "加载历史";
  }
  if (state === "recovering") {
    return "恢复中";
  }
  if (state === "streaming") {
    return "生成中";
  }
  if (state === "error") {
    return "连接失败";
  }
  return "就绪";
}
