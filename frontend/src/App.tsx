import { useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatArea } from "./components/ChatArea";
import { SourcePanel } from "./components/SourcePanel";
import { IngestionView } from "./components/IngestionView";
import { LearningView } from "./components/LearningView";
import type { Source } from "./types";

type Tab = "ingestion" | "chat" | "learning";

export default function App() {
  const [tab, setTab] = useState<Tab>("ingestion");
  const [selected, setSelected] = useState<string[]>([]);
  const [sources, setSources] = useState<Source[]>([]);

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

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
      </header>
      <div className="flex-1 flex overflow-hidden">
        {tab === "ingestion" ? (
          <IngestionView />
        ) : tab === "learning" ? (
          <LearningView />
        ) : (
          <>
            <Sidebar selected={selected} onToggle={toggle} />
            <ChatArea selectedDocs={selected} onSourcesChange={setSources} />
            <SourcePanel sources={sources} />
          </>
        )}
      </div>
    </div>
  );
}
