import { useEffect, useState } from "react";
import { Learning } from "../api/learning";
import type { FeedbackRecord, GoldenPair, LearnedRule, LearningStats } from "../types";

type LoadState = "idle" | "loading" | "error";

export function LearningView() {
  const [rules, setRules] = useState<LearnedRule[]>([]);
  const [golden, setGolden] = useState<GoldenPair[]>([]);
  const [feedback, setFeedback] = useState<FeedbackRecord[]>([]);
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [lastRun, setLastRun] = useState<LearningStats | null>(null);
  const [wiping, setWiping] = useState(false);
  const [wipeMsg, setWipeMsg] = useState<string | null>(null);

  const refresh = async () => {
    setState("loading");
    setError(null);
    try {
      const [r, g, f] = await Promise.all([
        Learning.rules(),
        Learning.golden(),
        Learning.feedback(),
      ]);
      setRules(r);
      setGolden(g);
      setFeedback(f);
      setState("idle");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setState("error");
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const onRun = async () => {
    setRunning(true);
    setError(null);
    try {
      const stats = await Learning.run();
      setLastRun(stats);
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  const onWipe = async () => {
    if (
      !confirm(
        "Wipe ALL self-improvement state?\n\nThis deletes every feedback event, learned rule, golden Q&A pair, and chunk-quality score. Documents and chat history are NOT affected."
      )
    )
      return;
    setWiping(true);
    setError(null);
    setWipeMsg(null);
    try {
      const res = await Learning.wipe();
      const total = Object.values(res.deleted).reduce((a, b) => a + b, 0);
      setWipeMsg(
        `Cleared ${total} learning record${total === 1 ? "" : "s"}: ` +
          Object.entries(res.deleted)
            .map(([k, v]) => `${k}=${v}`)
            .join(", ")
      );
      setLastRun(null);
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWiping(false);
    }
  };

  const upCount = feedback.filter((f) => f.rating === "up").length;
  const downCount = feedback.filter((f) => f.rating === "down").length;
  const corrections = feedback.filter((f) => f.rating === "down" && f.correction);

  return (
    <main className="flex-1 overflow-y-auto bg-slate-50 p-6">
      <div className="max-w-5xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-semibold text-slate-800">Self-Improvement</h1>
            <p className="text-sm text-slate-500 mt-1">
              The assistant learns from your 👍/👎 feedback. Three signals are aggregated:
              learned <strong>rules</strong> (from 👎 + corrections),
              <strong> golden Q&amp;A pairs</strong> (from 👍), and per-chunk
              <strong> quality scores</strong> used to re-rank retrieval.
            </p>
          </div>
          <div className="flex flex-col items-end gap-2">
            <div className="flex gap-2">
              <button
                onClick={onRun}
                disabled={running || wiping}
                className="px-4 py-2 rounded-md bg-indigo-600 text-white font-medium hover:bg-indigo-700 disabled:opacity-50"
              >
                {running ? "Running…" : "▶ Run Learning Loop"}
              </button>
              <button
                onClick={onWipe}
                disabled={running || wiping}
                title="Delete all feedback, rules, golden pairs and chunk-quality scores"
                className="px-4 py-2 rounded-md border border-red-400 text-red-700 bg-white font-medium hover:bg-red-50 disabled:opacity-50"
              >
                {wiping ? "Wiping…" : "🗑 Reset Learning"}
              </button>
            </div>
            <button
              onClick={refresh}
              disabled={state === "loading"}
              className="text-xs text-indigo-600 hover:underline"
            >
              {state === "loading" ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>

        {error && (
          <div className="rounded border border-red-300 bg-red-50 text-red-800 text-sm px-3 py-2">
            {error}
          </div>
        )}

        {wipeMsg && (
          <div className="rounded border border-slate-300 bg-slate-50 text-slate-700 text-sm px-3 py-2">
            {wipeMsg}
          </div>
        )}

        {lastRun && (
          <div className="rounded border border-emerald-300 bg-emerald-50 text-emerald-800 text-sm px-3 py-2">
            Last run: processed <strong>{lastRun.feedback_count}</strong> feedback events ·
            added <strong>{lastRun.rules_added}</strong> rules ·
            promoted <strong>{lastRun.golden_added}</strong> golden pairs ·
            updated <strong>{lastRun.chunk_updates}</strong> chunk scores.
          </div>
        )}

        {/* Stats cards */}
        <div className="grid grid-cols-4 gap-4">
          <Stat label="👍 Up-votes" value={upCount} color="text-emerald-700" />
          <Stat label="👎 Down-votes" value={downCount} color="text-red-700" />
          <Stat label="Corrections" value={corrections.length} color="text-amber-700" />
          <Stat label="Total feedback" value={feedback.length} color="text-slate-700" />
        </div>

        {/* Learned rules */}
        <Section
          title="Learned Rules"
          subtitle="Distilled guidelines injected into the system prompt at query time."
          empty={rules.length === 0 ? "No rules yet — submit 👎 feedback with a correction, then click Run Learning Loop." : null}
        >
          {rules.map((r) => (
            <div key={r.id} className="border-b border-slate-100 last:border-0 px-4 py-3">
              <div className="flex items-start gap-3">
                <span className="text-xs uppercase tracking-wide text-indigo-600 mt-0.5">
                  {r.category}
                </span>
                <p className="flex-1 text-sm text-slate-800">{r.rule}</p>
                <span className="text-xs text-slate-400 whitespace-nowrap">
                  evidence × {r.evidence_count}
                </span>
              </div>
            </div>
          ))}
        </Section>

        {/* Golden pairs */}
        <Section
          title="Golden Q&A Pairs"
          subtitle="High-quality answers (from 👍 feedback) re-used as few-shot examples."
          empty={golden.length === 0 ? "No golden pairs yet — give a 👍 to a good answer, then click Run Learning Loop." : null}
        >
          {golden.map((g) => (
            <div key={g.id} className="border-b border-slate-100 last:border-0 px-4 py-3">
              <div className="text-xs uppercase tracking-wide text-emerald-600 mb-1">
                {g.topic}
              </div>
              <div className="text-sm font-medium text-slate-800">Q: {g.question}</div>
              <div className="text-sm text-slate-600 mt-1 line-clamp-3">A: {g.answer}</div>
            </div>
          ))}
        </Section>

        {/* Recent feedback */}
        <Section
          title="Recent Feedback"
          subtitle="Raw 👍 / 👎 signal collected from chat turns."
          empty={feedback.length === 0 ? "No feedback yet — open the Chat tab and rate an answer." : null}
        >
          {feedback.slice(0, 30).map((f) => (
            <div key={f.id} className="border-b border-slate-100 last:border-0 px-4 py-3">
              <div className="flex items-start gap-2">
                <span className="text-lg leading-none">
                  {f.rating === "up" ? "👍" : "👎"}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-slate-800 truncate">
                    {f.question || <em className="text-slate-400">(no question)</em>}
                  </div>
                  <div className="text-xs text-slate-500 mt-0.5 line-clamp-2">
                    {f.answer}
                  </div>
                  {f.correction && (
                    <div className="mt-1 text-xs bg-amber-50 border border-amber-200 text-amber-900 rounded px-2 py-1">
                      <strong>correction:</strong> {f.correction}
                    </div>
                  )}
                </div>
                <span className="text-xs text-slate-400 whitespace-nowrap">
                  {new Date(f.created_at).toLocaleString()}
                </span>
              </div>
            </div>
          ))}
        </Section>
      </div>
    </main>
  );
}

function Stat({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="bg-white border border-slate-200 rounded-lg px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${color}`}>{value}</div>
    </div>
  );
}

function Section({
  title,
  subtitle,
  empty,
  children,
}: {
  title: string;
  subtitle: string;
  empty: string | null;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white border border-slate-200 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b bg-slate-50">
        <h2 className="font-semibold text-slate-800">{title}</h2>
        <p className="text-xs text-slate-500 mt-0.5">{subtitle}</p>
      </div>
      {empty ? <p className="p-4 text-sm text-slate-500">{empty}</p> : children}
    </div>
  );
}
