import { useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Message, Source } from "../types";
import { streamChat, Feedback } from "../api/chat";

interface Props {
  sessionId: string;
  messages: Message[];
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  selectedDocs: string[];
  onSourcesChange: (sources: Source[]) => void;
}

export function ChatArea({ sessionId, messages, setMessages, selectedDocs, onSourcesChange }: Props) {
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text },
      { role: "assistant", content: "", sources: [] },
    ]);
    setBusy(true);

    let buffer = "";

    await streamChat(sessionId, text, selectedDocs.length ? selectedDocs : undefined, {
      onSources: (srcs) => {
        onSourcesChange(srcs);
        setMessages((m) => {
          const out = [...m];
          out[out.length - 1] = { ...out[out.length - 1], sources: srcs };
          return out;
        });
      },
      onToken: (tok) => {
        buffer += tok;
        setMessages((m) => {
          const out = [...m];
          out[out.length - 1] = { ...out[out.length - 1], content: buffer };
          return out;
        });
      },
      onDone: (id) => {
        setMessages((m) => {
          const out = [...m];
          out[out.length - 1] = { ...out[out.length - 1], id };
          return out;
        });
      },
      onError: (e) => {
        setMessages((m) => {
          const out = [...m];
          out[out.length - 1] = { ...out[out.length - 1], content: `Error: ${e}` };
          return out;
        });
      },
    });
    setBusy(false);
  };

  const onFeedback = async (turnId: string | undefined, rating: "up" | "down") => {
    if (!turnId) return;
    const correction = rating === "down" ? prompt("Optional correction (helps the system learn):") || undefined : undefined;
    await Feedback.submit(sessionId, turnId, rating, correction);
  };

  return (
    <main className="flex-1 flex flex-col bg-slate-50">
      <div className="flex-1 overflow-y-auto p-6 space-y-4">
        {messages.length === 0 && (
          <div className="text-center text-slate-400 mt-20">
            <p className="text-lg">Ask anything about your documents.</p>
            <p className="text-sm mt-2">Upload PDFs or images on the left, then start chatting.</p>
          </div>
        )}
        {messages.map((m, i) => {
          const imgSources = (m.sources || []).filter((s) => s.image_url);
          return (
          <div key={i} className={m.role === "user" ? "text-right" : ""}>
            <div
              className={`inline-block max-w-3xl text-left rounded-lg px-4 py-3 ${
                m.role === "user"
                  ? "bg-indigo-600 text-white"
                  : "bg-white border border-slate-200 text-slate-800"
              }`}
            >
              {m.role === "assistant" ? (
                <ReactMarkdown>{m.content || "…"}</ReactMarkdown>
              ) : (
                <span>{m.content}</span>
              )}
              {m.role === "assistant" && imgSources.length > 0 && (
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {imgSources.map((s) => (
                    <a
                      key={s.chunk_id}
                      href={s.image_url || "#"}
                      target="_blank"
                      rel="noreferrer"
                      className="block border border-slate-200 rounded overflow-hidden hover:ring-2 hover:ring-indigo-400"
                      title={`page ${s.page} — ${s.snippet?.slice(0, 120) || ""}`}
                    >
                      <img
                        src={s.image_url || ""}
                        alt={`page ${s.page}`}
                        className="w-full h-32 object-contain bg-slate-50"
                        onError={(e) => {
                          const img = e.currentTarget;
                          img.style.display = "none";
                          const parent = img.parentElement;
                          if (parent && !parent.querySelector(".img-fallback")) {
                            const div = document.createElement("div");
                            div.className =
                              "img-fallback w-full h-32 flex items-center justify-center text-[11px] text-slate-500 bg-slate-100 px-2 text-center";
                            div.textContent = `Image on page ${s.page} (preview unavailable)`;
                            parent.insertBefore(div, img);
                          }
                        }}
                      />
                      <div className="text-[10px] text-slate-500 px-2 py-1 border-t">
                        page {s.page}
                      </div>
                    </a>
                  ))}
                </div>
              )}
              {m.role === "assistant" && m.id && (
                <div className="mt-2 flex gap-2">
                  <button onClick={() => onFeedback(m.id, "up")} className="text-sm hover:text-emerald-600">👍</button>
                  <button onClick={() => onFeedback(m.id, "down")} className="text-sm hover:text-red-600">👎</button>
                </div>
              )}
            </div>
          </div>
          );
        })}
      </div>
      <div className="border-t border-slate-200 p-4 bg-white">
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            disabled={busy}
            placeholder="Ask about your documents…"
            className="flex-1 border border-slate-300 rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
          <button
            onClick={send}
            disabled={busy}
            className="px-4 py-2 bg-indigo-600 text-white rounded-md disabled:opacity-50"
          >
            {busy ? "…" : "Send"}
          </button>
        </div>
      </div>
    </main>
  );
}
