/**
 * Streaming chat over Server-Sent Events.
 * The backend yields events of the form:
 *   {"type": "sources", "sources": [...]}
 *   {"type": "token", "content": "..."}
 *   {"type": "done", "turn_id": "..."}
 */
import type { Source } from "../types";
import { msalInstance, apiScopes } from "../auth/msal";

const AUTH_DISABLED = import.meta.env.VITE_DISABLE_AUTH === "true";

export interface ChatCallbacks {
  onSources?: (sources: Source[]) => void;
  onToken?: (token: string) => void;
  onDone?: (turnId: string) => void;
  onError?: (msg: string) => void;
}

async function getAuthHeader(): Promise<string | null> {
  if (AUTH_DISABLED) return null;
  const account = msalInstance.getAllAccounts()[0];
  if (!account) return null;
  try {
    const r = await msalInstance.acquireTokenSilent({ scopes: apiScopes, account });
    return `Bearer ${r.accessToken}`;
  } catch {
    return null;
  }
}

export async function streamChat(
  sessionId: string,
  message: string,
  docIds: string[] | undefined,
  cb: ChatCallbacks
): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const auth = await getAuthHeader();
  if (auth) headers.Authorization = auth;

  const res = await fetch("/api/chat", {
    method: "POST",
    headers,
    body: JSON.stringify({ session_id: sessionId, message, doc_ids: docIds }),
  });
  if (!res.ok || !res.body) {
    cb.onError?.(`HTTP ${res.status}`);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const events = buf.split("\n\n");
    buf = events.pop() || "";
    for (const evt of events) {
      const line = evt.trim();
      if (!line.startsWith("data: ")) continue;
      try {
        const payload = JSON.parse(line.slice(6));
        if (payload.type === "sources") cb.onSources?.(payload.sources);
        else if (payload.type === "token") cb.onToken?.(payload.content);
        else if (payload.type === "done") cb.onDone?.(payload.turn_id);
        else if (payload.type === "error") cb.onError?.(payload.message);
      } catch {
        /* ignore */
      }
    }
  }
}

export const Feedback = {
  submit: async (sessionId: string, turnId: string, rating: "up" | "down", correction?: string) => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const auth = await getAuthHeader();
    if (auth) headers.Authorization = auth;
    await fetch("/api/feedback", {
      method: "POST",
      headers,
      body: JSON.stringify({ session_id: sessionId, turn_id: turnId, rating, correction }),
    });
  },
};
