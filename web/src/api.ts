import type { ApiResponse, AppInfo, HistoryMessage, Session, SseFrame } from "./types";

type CompletionRequestBody = {
  event_type?: "user_input" | "interruption_response";
  query: string | null;
  interruption_response?: {
    request_id: string;
    request_type: string;
    response: Record<string, unknown>;
  };
  client_event_id: string;
  last_event_id: string | null;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const COMPLETION_NETWORK_RETRY_COUNT = 2;
const COMPLETION_NETWORK_RETRY_DELAY_MS = 10_000;

class SseNetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SseNetworkError";
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const payload = (await response.json()) as ApiResponse<T>;
  if (!payload.success) {
    throw new Error(payload.message || `API ${payload.code}`);
  }
  return payload.data;
}

export async function getAppInfo(limit = 20): Promise<AppInfo> {
  return requestJson<AppInfo>(`/api/v1/info?limit=${limit}`);
}

export async function createSession(title = "新的会话"): Promise<Session> {
  return requestJson<Session>("/api/v1/sessions", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export async function deleteSession(sessionId: string): Promise<void> {
  await requestJson<null>(`/api/v1/sessions/${sessionId}`, {
    method: "DELETE",
  });
}

export async function getHistory(sessionId: string): Promise<HistoryMessage[]> {
  return requestJson<HistoryMessage[]>(`/api/v1/sessions/${sessionId}/history`);
}

export async function streamCompletion(
  sessionId: string,
  body: CompletionRequestBody,
  onFrame: (frame: SseFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  let lastEventId = body.last_event_id;

  for (let retryCount = 0; ; retryCount += 1) {
    try {
      await streamPost(
        `/api/v1/sessions/${sessionId}/completion`,
        {
          ...body,
          last_event_id: lastEventId,
        },
        (frame) => {
          if (frame.id && frame.id !== "null") {
            lastEventId = frame.id;
          }
          onFrame(frame);
        },
        signal,
      );
      return;
    } catch (error) {
      if (isAbortError(error) || !(error instanceof SseNetworkError) || retryCount >= COMPLETION_NETWORK_RETRY_COUNT) {
        throw error;
      }

      await delay(COMPLETION_NETWORK_RETRY_DELAY_MS, signal);
    }
  }
}

export async function streamResume(
  sessionId: string,
  onFrame: (frame: SseFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  await streamPost(`/api/v1/sessions/${sessionId}/completion/resume`, undefined, onFrame, signal);
}

async function streamPost(
  path: string,
  body: unknown,
  onFrame: (frame: SseFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: "POST",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new SseNetworkError(getErrorMessage(error, "SSE 网络连接失败"));
  }

  if (!response.ok || response.body === null) {
    throw new Error(`SSE HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let receivedTerminalFrame = false;

  while (true) {
    let result: ReadableStreamReadResult<Uint8Array>;
    try {
      result = await reader.read();
    } catch (error) {
      if (isAbortError(error)) {
        throw error;
      }
      throw new SseNetworkError(getErrorMessage(error, "SSE 网络连接中断"));
    }

    const { done, value } = result;
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const rawFrame of frames) {
      const frame = parseSseFrame(rawFrame);
      if (frame !== null) {
        receivedTerminalFrame ||= frame.event === "end" || frame.event === "error";
        onFrame(frame);
      }
    }
  }

  const rest = buffer.trim();
  if (rest) {
    const frame = parseSseFrame(rest);
    if (frame !== null) {
      receivedTerminalFrame ||= frame.event === "end" || frame.event === "error";
      onFrame(frame);
    }
  }

  if (!receivedTerminalFrame) {
    throw new SseNetworkError("SSE 网络连接意外断开");
  }
}

function delay(durationMs: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      return;
    }

    const timeoutId = window.setTimeout(resolve, durationMs);
    signal?.addEventListener("abort", () => {
      window.clearTimeout(timeoutId);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

function getErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function parseSseFrame(rawFrame: string): SseFrame | null {
  const lines = rawFrame.split(/\r?\n/);
  let id: string | null = null;
  let event = "message";
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith("id:")) {
      id = line.slice(3).trim();
    } else if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  if (!event && dataLines.length === 0) {
    return null;
  }

  return {
    id,
    event,
    data: dataLines.join("\n"),
  };
}
