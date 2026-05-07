import { useEffect, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatArea } from "./components/ChatArea";
import { SourcePanel } from "./components/SourcePanel";
import { IngestionView } from "./components/IngestionView";
import { LearningView } from "./components/LearningView";
import type { Message, Source } from "./types";
import { ChatHistory } from "./api/chat";

type Tab = "ingestion" | "chat" | "learning";

const SESSION_KEY = "docmind.sessionId";
const MESSAGES_KEY = "docmind.messages";
const SELECTED_KEY = "docmind.selectedDocs";

function loadMessages(): Message[] {
  try {
    const raw = localStorage.getItem(MESSAGES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Message[]) : [];
  } catch {
    return [];
  }
}

function loadSelected(): string[] {
  try {
    const raw = localStorage.getItem(SELECTED_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as string[]) : [];
  } catch {
    return [];
  }
}

function loadOrCreateSessionId(): string {
  const existing = localStorage.getItem(SESSION_KEY);
  if (existing) return existing;
  const sid = crypto.randomUUID();
  localStorage.setItem(SESSION_KEY, sid);
  return sid;
}

export default function App() {
  const [tab, setTab] = useState<Tab>("ingestion");
  const [selected, setSelected] = useState<string[]>(() => loadSelected());
  const [sources, setSources] = useState<Source[]>([]);

  // Chat session state lives at the App level so switching tabs does NOT
  // unmount it. We also persist it to localStorage so reloads + tab swaps
  // keep the conversation visible. The server-side history lives in Redis.
  const [sessionId, setSessionId] = useState<string>(() => loadOrCreateSessionId());
  const [messages, setMessages] = useState<Message[]>(() => loadMessages());

  // On first mount (and whenever sessionId changes), hydrate messages from
  // the server (Redis) so chats started elsewhere show up too.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const turns = await ChatHistory.get(sessionId);
        if (cancelled || !turns.length) return;
        const hydrated: Message[] = turns.map((t) => ({
          id: t.role === "assistant" ? t.id : undefined,
          role: t.role,
          content: t.content,
          sources: t.sources || [],
        }));
        // Prefer server history if local cache is empty, else keep local (it
        // may include in-flight streaming tokens).
        setMessages((prev) => (prev.length ? prev : hydrated));
      } catch {
        /* ignore — Redis may be empty for a new session */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  useEffect(() => {
    localStorage.setItem(MESSAGES_KEY, JSON.stringify(messages));
  }, [messages]);

  useEffect(() => {
    localStorage.setItem(SELECTED_KEY, JSON.stringify(selected));
  }, [selected]);

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const newSession = () => {
    const sid = crypto.randomUUID();
    localStorage.setItem(SESSION_KEY, sid);
    setSessionId(sid);
    setMessages([]);
    setSources([]);
  };

  const tabBtn = (id: Tab, label: string) => (
    <button
      onClick={() => setTab(id)}
      className={`px-4 h-12 text-sm font-medium border-b-2 transition-colors ${
        tab === id
          ? "border-indigo-600 text-indigo-700"
          : "border-transparent text-slate-500 hover:text-slate-700"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="h-full flex flex-col">
      <header className="h-12 border-b bg-white px-4 flex items-center gap-6">
        <span className="font-semibold text-indigo-700">📄 DocMind AI</span>
        <nav className="flex h-12">
          {tabBtn("ingestion", "Ingestion")}
          {tabBtn("chat", "Chat (RAG)")}
          {tabBtn("learning", "Self-Improvement")}
        </nav>
        {tab === "chat" && (
          <button
            onClick={newSession}
            className="ml-auto text-xs px-3 py-1 rounded border border-slate-300 text-slate-600 hover:bg-slate-50"
            title="Start a new chat session"
          >
            + New chat
          </button>
        )}
      </header>
      <div className="flex-1 flex overflow-hidden">
        {tab === "ingestion" && <IngestionView />}
        {tab === "learning" && <LearningView />}
        {/*
          Keep the chat panel mounted at all times so its in-flight streaming
          state and message list survive tab switches. We just hide it
          visually when another tab is active.
        */}
        <div className={`flex-1 flex overflow-hidden ${tab === "chat" ? "" : "hidden"}`}>
          <Sidebar selected={selected} onToggle={toggle} />
          <ChatArea
            sessionId={sessionId}
            messages={messages}
            setMessages={setMessages}
            selectedDocs={selected}
            onSourcesChange={setSources}
          />
          <SourcePanel sources={sources} />
        </div>
      </div>
    </div>
  );
}
