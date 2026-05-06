import { useEffect, useRef, useState } from "react";
import { Documents } from "../api/documents";
import type { DocumentMeta, StageEvent } from "../types";

const STAGE_ORDER = [
  "download",
  "extract_text",
  "extract_images",
  "chunk",
  "embed",
  "index",
  "complete",
];

const STAGE_LABEL: Record<string, string> = {
  download: "1. Upload to Blob",
  extract_text: "2. Document Intelligence",
  extract_images: "3. Image extraction (Vision)",
  chunk: "4. Smart chunking",
  embed: "5. Embeddings",
  index: "6. Index in AI Search",
  complete: "7. Done",
};

function statusIcon(status: string) {
  switch (status) {
    case "done":
      return <span className="text-emerald-600">✓</span>;
    case "running":
      return <span className="inline-block animate-spin text-indigo-600">◐</span>;
    case "failed":
      return <span className="text-red-600">✕</span>;
    default:
      return <span className="text-slate-400">○</span>;
  }
}

function StageList({ stages }: { stages: StageEvent[] }) {
  const byName: Record<string, StageEvent> = {};
  for (const s of stages || []) byName[s.name] = s;
  return (
    <ol className="space-y-1 mt-3">
      {STAGE_ORDER.map((name) => {
        const s = byName[name] || { name, status: "pending" };
        return (
          <li key={name} className="flex items-center gap-2 text-sm">
            <span className="w-4 text-center">{statusIcon(s.status)}</span>
            <span
              className={
                s.status === "done"
                  ? "text-slate-700"
                  : s.status === "running"
                  ? "text-indigo-700 font-medium"
                  : s.status === "failed"
                  ? "text-red-700"
                  : "text-slate-400"
              }
            >
              {STAGE_LABEL[name] || name}
            </span>
            {s.detail && (
              <span className="text-xs text-slate-500">— {s.detail}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export function IngestionView() {
  const [docs, setDocs] = useState<DocumentMeta[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = () =>
    Documents.list()
      .then((d) => setDocs(d.sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))))
      .catch(() => {});

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2500);
    return () => clearInterval(t);
  }, []);

  const onUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setBusy(true);
    setErr(null);
    try {
      await Documents.upload(f);
      await refresh();
    } catch (ex: any) {
      setErr(ex?.message || "Upload failed");
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  };

  const onDelete = async (id: string) => {
    if (!confirm("Delete this document and its index entries?")) return;
    await Documents.remove(id);
    await refresh();
  };

  return (
    <div className="flex-1 overflow-y-auto bg-slate-50 p-6">
      <div className="max-w-4xl mx-auto">
        <div className="bg-white border border-slate-200 rounded-lg p-6 shadow-sm">
          <h2 className="text-lg font-semibold text-slate-800">Upload a document</h2>
          <p className="text-sm text-slate-500 mt-1">
            PDFs are uploaded to the <code className="px-1 bg-slate-100 rounded">user-input</code>{" "}
            blob container, then automatically processed: Document Intelligence → image
            extraction → chunking → embeddings → AI Search index.
          </p>
          <div className="mt-4 flex items-center gap-3">
            <input
              ref={fileRef}
              type="file"
              accept="application/pdf,image/*"
              onChange={onUpload}
              className="hidden"
            />
            <button
              onClick={() => fileRef.current?.click()}
              disabled={busy}
              className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
            >
              {busy ? "Uploading…" : "+ Upload PDF / Image"}
            </button>
            {err && <span className="text-sm text-red-600">{err}</span>}
          </div>
        </div>

        <h3 className="mt-8 mb-3 text-sm font-semibold uppercase tracking-wide text-slate-600">
          Pipeline runs
        </h3>

        {docs.length === 0 && (
          <div className="bg-white border border-dashed border-slate-300 rounded-lg p-8 text-center text-slate-500">
            No documents uploaded yet.
          </div>
        )}

        <div className="space-y-3">
          {docs.map((d) => (
            <div key={d.id} className="bg-white border border-slate-200 rounded-lg p-4 shadow-sm">
              <div className="flex items-start justify-between">
                <div>
                  <div className="font-medium text-slate-800">{d.filename}</div>
                  <div className="text-xs text-slate-500 mt-0.5">
                    {d.container || "—"} · {d.total_pages} pages · {d.total_chunks} chunks ·{" "}
                    {d.total_images} images · {d.total_tables} tables
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span
                    className={`text-[10px] uppercase px-2 py-0.5 rounded ${
                      d.status === "indexed"
                        ? "bg-emerald-100 text-emerald-800"
                        : d.status === "processing"
                        ? "bg-amber-100 text-amber-800"
                        : d.status === "failed"
                        ? "bg-red-100 text-red-800"
                        : "bg-slate-200 text-slate-700"
                    }`}
                  >
                    {d.status}
                  </span>
                  <button
                    onClick={() => onDelete(d.id)}
                    className="text-xs text-slate-500 hover:text-red-600"
                  >
                    Delete
                  </button>
                </div>
              </div>
              <StageList stages={d.stages || []} />
              {d.error && (
                <div className="mt-2 text-xs text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">
                  {d.error}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
