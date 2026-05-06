import type { Source } from "../types";

export function SourcePanel({ sources }: { sources: Source[] }) {
  if (!sources?.length) {
    return (
      <aside className="w-80 border-l border-slate-200 bg-white p-4">
        <h2 className="font-semibold text-slate-700 mb-2">Sources</h2>
        <p className="text-sm text-slate-500">Sources used by the assistant will appear here.</p>
      </aside>
    );
  }
  return (
    <aside className="w-80 border-l border-slate-200 bg-white p-4 overflow-y-auto">
      <h2 className="font-semibold text-slate-700 mb-3">Sources ({sources.length})</h2>
      <ul className="space-y-3">
        {sources.map((s) => (
          <li key={s.chunk_id} className="border border-slate-200 rounded-md p-3">
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-medium text-indigo-600 uppercase">{s.type}</span>
              <span className="text-xs text-slate-500">page {s.page}</span>
            </div>
            {s.image_url ? (
              <img src={s.image_url} alt="source" className="rounded mb-2 max-h-40 object-contain" />
            ) : null}
            <p className="text-xs text-slate-700 leading-relaxed line-clamp-6">{s.snippet}</p>
          </li>
        ))}
      </ul>
    </aside>
  );
}
