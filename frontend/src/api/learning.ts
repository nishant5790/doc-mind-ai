import { client } from "./client";
import type { FeedbackRecord, GoldenPair, LearnedRule, LearningStats } from "../types";

export const Learning = {
  rules: () => client.get<LearnedRule[]>("/admin/rules").then((r) => r.data),
  golden: () => client.get<GoldenPair[]>("/admin/golden").then((r) => r.data),
  feedback: () => client.get<FeedbackRecord[]>("/admin/feedback").then((r) => r.data),
  run: () => client.post<LearningStats>("/admin/learn").then((r) => r.data),
  wipe: () =>
    client
      .delete<{ status: string; deleted: Record<string, number> }>("/admin/learning")
      .then((r) => r.data),
};
