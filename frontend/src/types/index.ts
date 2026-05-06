/**
 * Shared TypeScript types — mirror the backend Pydantic models.
 */
export interface StageEvent {
  name: string;
  status: "pending" | "running" | "done" | "failed";
  started_at?: string | null;
  finished_at?: string | null;
  detail?: string | null;
}

export interface DocumentMeta {
  id: string;
  user_id: string;
  filename: string;
  blob_name: string;
  container?: string | null;
  status: "pending" | "processing" | "indexed" | "failed";
  total_pages: number;
  total_chunks: number;
  total_images: number;
  total_tables: number;
  error?: string | null;
  stages?: StageEvent[];
  created_at: string;
  indexed_at?: string | null;
}

export interface Source {
  chunk_id: string;
  doc_id: string;
  page: number;
  type: string;
  snippet: string;
  image_url?: string | null;
}

export interface Message {
  id?: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
}

export interface LearnedRule {
  id: string;
  category: string;
  rule: string;
  evidence_count: number;
  created_at: string;
  updated_at: string;
}

export interface GoldenPair {
  id: string;
  topic: string;
  question: string;
  answer: string;
  chunk_ids: string[];
  confirmed_at: string;
}

export interface FeedbackRecord {
  id: string;
  session_id: string;
  turn_id: string;
  user_id: string;
  rating: "up" | "down";
  correction?: string | null;
  question: string;
  answer: string;
  chunk_ids: string[];
  created_at: string;
}

export interface LearningStats {
  feedback_count: number;
  rules_added: number;
  golden_added: number;
  chunk_updates: number;
}
