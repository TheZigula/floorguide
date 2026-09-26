// types.ts — the API contract from CLAUDE.md, transcribed into TypeScript.
// Owns: the shape of every request and response the front end sends or receives; no logic.

/** Which kind of source, rule, or worker handled the turn. Drives the "Routed to" chip. */
export type RoutedTo =
  | "safety"
  | "maintenance"
  | "quality"
  | "rule"
  | "work_order"
  | "memory"
  | "refused";

export type Worker = "retriever" | "analyst" | "actor" | "remember";

export type Category = "safety" | "maintenance" | "quality" | "asset_record";

/** Which embedding index the chunk came out of: the OpenAI one or the local fallback. */
export type SourceBackend = "openai" | "local";

export type RuleStatus = "OK" | "DUE_SOON" | "OVERDUE";

export type Priority = "low" | "normal" | "high";

export interface Source {
  source_id: string;
  title: string;
  category: string;
  snippet: string;
  source_backend: SourceBackend;
}

export interface RuleResult {
  asset_id: string;
  hours_since_service: number;
  hours_remaining: number;
  status: RuleStatus;
  overdue_by: number;
}

export interface WorkOrder {
  asset_id: string;
  task: string;
  priority: Priority;
  source_refs: string[];
}

export interface PendingApproval {
  draft_id: string;
  /** Approve is disabled until this is present: it is what ties the click to this exact draft. */
  fingerprint: string;
  work_order: WorkOrder;
}

export interface ChatRequest {
  thread_id: string;
  user_id: string;
  message: string;
}

export interface ChatResponse {
  thread_id: string;
  worker: Worker;
  routed_to: RoutedTo;
  answer: string;
  sources: Source[];
  rule_result: RuleResult | null;
  pending_approval: PendingApproval | null;
  dropped_chunks: number;
  refused: boolean;
  memory: { home_line: string | null };
}

export interface ApproveRequest {
  thread_id: string;
  draft_id: string;
  fingerprint: string;
}

export interface ApproveResponse {
  status: "approved";
  confirmation_id: string;
  answer: string;
}

export interface RejectRequest {
  thread_id: string;
  draft_id: string;
}

export interface RejectResponse {
  status: "rejected";
  answer: string;
}

export interface HealthResponse {
  status: string;
  service: string;
  commit: string;
  vector_backend: SourceBackend;
}

/** Thrown for any non-2xx reply, carrying the HTTP status so a 409 can be told apart. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}
