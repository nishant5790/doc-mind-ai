import { useEffect, useState } from "react";
import { Documents } from "../api/documents";
import type { DocumentMeta } from "../types";

interface Props {
  selected: string[];
  onToggle: (id: string) => void;
}

export function Sidebar({ selected, onToggle }: Props) {
  const [docs, setDocs] = useState<DocumentMeta[]>([]);
  const [busy, setBusy] = useState(false);
  const [adminBusy, setAdminBusy] = useState<string | null>(null);

  const refresh = () => Documents.list().then(setDocs).catch(() => {});

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, []);

  const onUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setBusy(true);
    try {
      await Documents.upload(f);
      await refresh();
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  };

  const onDeleteDoc = async (e: React.MouseEvent, doc: DocumentMeta) => {
    e.stopPropagation();
    if (!confirm(`Delete "${doc.filename}"? This removes the blob and all of its index chunks.`)) return;
    try {
      await Documents.remove(doc.id);
    } catch (err) {
      alert(`Delete failed: ${err}`);
    }
    await refresh();
  };

  const onWipeIndex = async () => {
    if (!confirm("Wipe the entire AI Search index? All chunks for all documents will be lost.")) return;
    setAdminBusy("index");
    try {
      await Documents.wipeIndex();
    } catch (err) {
      alert(`Wipe index failed: ${err}`);
    } finally {
      setAdminBusy(null);
      refresh();
    }
  };

  const onWipeBlobs = async () => {
    if (!confirm("Delete ALL of your uploaded blobs? Source PDFs/images will be removed.")) return;
    setAdminBusy("blobs");
    try {
      await Documents.wipeBlobs();
    } catch (err) {
      alert(`Wipe blobs failed: ${err}`);
    } finally {
      setAdminBusy(null);
      refresh();
    }
  };

  const onWipeAll = async () => {
    if (!confirm("WIPE EVERYTHING (index + all blobs + document metadata)? This cannot be undone.")) return;
    setAdminBusy("all");
    try {
      await Documents.wipeAll();
    } catch (err) {
      alert(`Wipe all failed: ${err}`);
    } finally {
      setAdminBusy(null);
      refresh();
    }
  };

  const statusBadge = (s: string) => {
    const map: Record<string, string> = {
      pending: "bg-slate-200 text-slate-700",
      processing: "bg-amber-100 text-amber-800",
      indexed: "bg-emerald-100 text-emerald-800",
      failed: "bg-red-100 text-red-800",
    };
    return map[s] || "bg-slate-200";
  };

  return (
    <aside className="w-72 border-r border-slate-200 bg-white flex flex-col">
      <div className="p-4 border-b">
        <h2 className="font-semibold text-slate-700 mb-2">Documents</h2>
        <label className="block">
          <input type="file" accept="application/pdf,image/*" onChange={onUpload} className="hidden" />
          <span className="block text-center w-full px-3 py-2 rounded-md text-sm font-medium bg-indigo-600 text-white hover:bg-indigo-700 cursor-pointer">
            {busy ? "Uploading…" : "+ Upload PDF / Image"}
          </span>
        </label>
      </div>
      <div className="flex-1 overflow-y-auto">
        {docs.length === 0 && (
          <p className="p-4 text-sm text-slate-500">No documents yet.</p>
        )}
        {docs.map((d) => (
          <div
            key={d.id}
            className={`group flex items-start border-b border-slate-100 hover:bg-slate-50 ${
              selected.includes(d.id) ? "bg-indigo-50" : ""
            }`}
          >
            <button
              onClick={() => onToggle(d.id)}
              className="flex-1 text-left px-4 py-3"
            >
              <div className="text-sm font-medium text-slate-800 truncate">{d.filename}</div>
              <div className="flex items-center gap-2 mt-1">
                <span className={`text-[10px] uppercase px-1.5 py-0.5 rounded ${statusBadge(d.status)}`}>
                  {d.status}
                </span>
                <span className="text-xs text-slate-500">
                  {d.total_pages} pg · {d.total_chunks} chunks
                </span>
              </div>
            </button>
            <button
              title="Delete document (blob + index chunks)"
              onClick={(e) => onDeleteDoc(e, d)}
              className="px-3 py-3 text-slate-400 hover:text-red-600 opacity-0 group-hover:opacity-100 transition-opacity"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
      <div className="border-t p-3 space-y-2 bg-slate-50">
        <p className="text-[11px] uppercase tracking-wide text-slate-500">Danger zone</p>
        <button
          onClick={onWipeIndex}
          disabled={adminBusy !== null}
          className="w-full text-xs px-2 py-1.5 rounded border border-amber-300 text-amber-800 bg-white hover:bg-amber-50 disabled:opacity-50"
        >
          {adminBusy === "index" ? "Wiping…" : "Wipe Index"}
        </button>
        <button
          onClick={onWipeBlobs}
          disabled={adminBusy !== null}
          className="w-full text-xs px-2 py-1.5 rounded border border-amber-300 text-amber-800 bg-white hover:bg-amber-50 disabled:opacity-50"
        >
          {adminBusy === "blobs" ? "Wiping…" : "Wipe Blobs"}
        </button>
        <button
          onClick={onWipeAll}
          disabled={adminBusy !== null}
          className="w-full text-xs px-2 py-1.5 rounded border border-red-400 text-red-700 bg-white hover:bg-red-50 disabled:opacity-50"
        >
          {adminBusy === "all" ? "Wiping…" : "Wipe Everything"}
        </button>
      </div>
    </aside>
  );
}

