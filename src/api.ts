// Typed client for the Polytone Python backend. Mirrors CONTRACTS.md exactly.

// VITE_POLYTONE_API overrides the engine URL for development against a stub (never in the app).
export const API_BASE: string = import.meta.env.VITE_POLYTONE_API || "http://127.0.0.1:8765";
export const OFFLINE_HINT = "Backend offline — run `uv run polytone-api`";

// ---------- Types ----------

export interface Health {
  status: "ok";
  neo4j: boolean;
  llm: "anthropic" | "mock";
  embeddings: "qwen" | "none";
  graph_store: "neo4j" | "memory";
  /** false → the configured LLM can't run (e.g. anthropic without a key); /generate will 502 */
  llm_ready?: boolean;
}

export interface IngestResult {
  accepted: number;
  rejected: number;
  reasons: string[];
  sentences: number;
  words_merged: number;
}

export type SourceName = "manual" | "gmail" | "wispr" | "watcher" | "typed";

export interface IngestSource {
  /** One of SourceName today; typed as string so a newer backend's sources still render. */
  name: SourceName | (string & {});
  enabled: boolean;
  status: string;
  last_run: string | null;
  items_ingested: number;
}

export interface Persona {
  name: string;
  description: string;
  /** Default, Boss, Friend, Investor: can be reworded and reset, never deleted. Optional: older engines omit the next three. */
  builtin?: boolean;
  /** The description is the user's own wording. */
  edited?: boolean;
  /** Writing samples learned under this persona. */
  samples?: number;
}

export interface PersonaDeleted {
  name: string;
  moved_samples: number;
  active_persona: string;
}

export type InjectMode = "paste" | "type";

export interface AppConfig {
  active_persona: string;
  hotkey: string;
  inject_mode: InjectMode;
  /** "Learn from what I type" — opt-in, default off. Optional: older backends omit it. */
  capture_enabled?: boolean;
  /** macOS bundle ids the typing capture is allowed to read from. */
  capture_apps?: string[];
  /** Auto-lock after this many idle minutes (1-240, default 15). Optional: older engines omit it. */
  lock_idle_minutes?: number;
  /** Delete typed/dictated samples, drafts and draft edits older than N days; 0 = keep forever. */
  retention_days?: number;
  /** "quality" (Claude Sonnet) or "fast" (Claude Haiku: ~40% quicker, a little less literal). Optional: older engines omit it. */
  draft_speed?: DraftSpeed;
  /** Days between automatic learning evaluations (0 = only "Evaluate now"). Optional: older engines omit it. */
  learning_eval_days?: number;
}

export type DraftSpeed = "quality" | "fast";

export interface AuditEntry {
  seq: number;
  ts: string;
  event: string;
  actor: string;
  /** JSON object (the engine may send it parsed or as its JSON text). Never user text. */
  detail: Record<string, unknown> | string | null;
}

export interface AuditPage {
  entries: AuditEntry[];
  head_seq: number;
}

export interface AuditVerify {
  ok: boolean;
  count: number;
  first_bad_seq: number | null;
  reason: string | null;
}

export const WIPE_PHRASE = "delete everything";

/** Each component is a 0-1 DISTANCE (0 = identical to your baseline). */
export interface ScoreComponents {
  sentence_length: number;
  punctuation: number;
  lexical: number;
  function_words: number;
  embedding: number | null;
}

export interface ScoreBreakdown {
  /** 0-100, 100 = identical style */
  score: number;
  /** 0-1 */
  distance: number;
  components: ScoreComponents;
  /** trust in the number given baseline size + draft length (additive; older rows may lack it) */
  confidence?: "low" | "medium" | "high";
}

export interface Draft {
  draft_id: string;
  created_at: string;
  persona: string;
  intent: string;
  text: string;
  latency_ms: number;
  score: ScoreBreakdown | null;
  /** What happened to it after it was shown. Optional: older engines omit it. */
  outcome?: DraftOutcome | null;
}

export type DraftOutcome = "accepted" | "regenerated" | "discarded" | "edited";

export type LearningMode = "normal" | "pinned" | "corrections_paused";

export interface LearningState {
  mode: LearningMode;
  /** The pinned snapshot (mode "pinned"). */
  snapshot_id: number | null;
  since: string | null;
}

export type EvaluationStatus = "accepted" | "rolled_back" | "corrections_paused";

export interface Evaluation {
  id: number;
  created_at: string;
  status: EvaluationStatus;
  decision: "current" | "no_corrections" | "previous" | null;
  incumbent: string | null;
  /** 0-100 style match of the rules generation uses after this evaluation. */
  score: number | null;
  scores: { current?: number; no_corrections?: number; previous?: number };
  holdout: number | null;
  trigger: string | null;
}

export interface EvaluationResult {
  persona: string;
  status: EvaluationStatus | "skipped" | "failed";
  reason: string | null;
  scores: Evaluation["scores"];
  state?: LearningState;
}

export interface LearningDelta {
  value: number;
  edits: number;
}

/** GET /learning: what edit learning corrects, plus the evaluation state (additive fields). */
export interface LearningSummary {
  persona: string;
  edits_learned: number;
  min_edits: number;
  sentence_len_delta: LearningDelta | null;
  contractions_delta: LearningDelta | null;
  lowercase_delta: LearningDelta | null;
  punctuation: Record<string, LearningDelta>;
  avoid_words: string[];
  state?: LearningState;
  last_evaluation?: Evaluation | null;
  eval_every_days?: number;
  min_holdout?: number;
}

export interface ScoresSummary {
  mean: number | null;
  count: number;
  trend: { t: string; score: number }[];
}

export type NodeLabel = "Word" | "SentenceStructure" | "PunctuationMark" | "IntentContext";
export type EdgeType = "FOLLOWED_BY" | "PART_OF_STRUCTURE" | "USED_IN";

export interface GraphNode {
  id: string;
  label: NodeLabel;
  name: string;
  weight: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: EdgeType;
  weight: number;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface Stats {
  words: number;
  structures: number;
  samples: number;
  avg_sentence_len: number | null;
}

// ---------- Errors ----------

export class ApiError extends Error {
  readonly status: number | null;
  readonly offline: boolean;
  /** machine-readable error code from the body, e.g. "llm_failed" (502) / "llm_timeout" (504) */
  readonly code: string | null;
  constructor(message: string, status: number | null, offline: boolean, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.offline = offline;
    this.code = code;
  }
}

export function isOffline(err: unknown): boolean {
  return err instanceof ApiError && err.offline;
}

/** The engine said 401: the UI session is missing, revoked or expired. */
export function isLocked(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

// ---------- UI session (memory only: never localStorage) ----------

let sessionToken: string | null = null;
const lockListeners = new Set<() => void>();

/** Set by the unlock flow; "" = an engine without sessions (no header sent). */
export function setSessionToken(token: string | null) {
  sessionToken = token;
}

/** Called once for every 401 (the token is dropped first). */
export function onSessionRejected(fn: () => void): () => void {
  lockListeners.add(fn);
  return () => lockListeners.delete(fn);
}

/** One human-readable line for any error: backend (ApiError), Tauri command (string/object) or JS. */
export function errorMessage(err: unknown): string {
  if (isOffline(err)) return OFFLINE_HINT;
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

// ---------- Core request ----------

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  timeoutMs?: number;
}

async function request<T>(path: string, { method = "GET", body, timeoutMs = 10_000 }: RequestOptions = {}): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const headers: Record<string, string> = {};
  if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
  // Content-Type only with a body (no need to make a GET any less simple than auth already does).
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const sentWith = sessionToken;
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      signal: ctrl.signal,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new ApiError(OFFLINE_HINT, null, true);
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 401) {
    // Only a rejection of the token we still hold locks; a stale request from before an
    // unlock must not throw away the new session.
    if (sentWith === sessionToken) {
      sessionToken = null;
      lockListeners.forEach((fn) => fn());
    }
    throw new ApiError("Locked", 401, false, "locked");
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    let code: string | null = null;
    try {
      const err: unknown = await res.json();
      if (isRecord(err)) {
        if (typeof err.error === "string") code = err.error;
        if (typeof err.detail === "string") detail = err.detail;
        else if (err.detail != null) detail = JSON.stringify(err.detail);
      }
    } catch {
      /* non-JSON error body */
    }
    if (code === "llm_timeout") detail = `The model timed out — ${detail}`;
    else if (code === "llm_failed") detail = `The model failed — ${detail}`;
    throw new ApiError(detail, res.status, false, code);
  }
  return (await res.json()) as T;
}

// ---------- Endpoints ----------
// POST /generate and /ingest/typed are called by the Rust side only.

export const api = {
  health: () => request<Health>("/health", { timeoutMs: 3000 }),

  ingestManual: (text: string, persona: string | null) =>
    request<IngestResult>("/ingest/manual", { method: "POST", body: { text, persona }, timeoutMs: 30_000 }),

  ingestSources: () => request<IngestSource[]>("/ingest/sources"),

  runSource: (name: string) =>
    request<IngestResult>(`/ingest/sources/${encodeURIComponent(name)}/run`, { method: "POST", timeoutMs: 120_000 }),

  personas: () => request<Persona[]>("/personas"),

  createPersona: (name: string, description: string) =>
    request<Persona>("/personas", { method: "POST", body: { name, description } }),

  /** `description: null` resets a built-in to Polytone's wording. */
  updatePersona: (name: string, description: string | null) =>
    request<Persona>(`/personas/${encodeURIComponent(name)}`, { method: "PUT", body: { description } }),

  /** Its samples (and what the style graph learned from them) move to Default. */
  deletePersona: (name: string) =>
    request<PersonaDeleted>(`/personas/${encodeURIComponent(name)}`, { method: "DELETE", timeoutMs: 30_000 }),

  getConfig: () => request<AppConfig>("/config"),

  /** PUT is a partial update: omitted fields stay unchanged on the backend. */
  putConfig: (patch: Partial<AppConfig>) => request<AppConfig>("/config", { method: "PUT", body: patch }),

  drafts: (limit = 50) => request<Draft[]>(`/drafts?limit=${limit}`),

  scoresSummary: () => request<ScoresSummary>("/scores/summary"),

  learning: (persona: string) => request<LearningSummary>(`/learning?persona=${encodeURIComponent(persona)}`),

  evaluations: (persona: string, limit = 20) =>
    request<{ persona: string; evaluations: Evaluation[] }>(
      `/learning/evaluations?persona=${encodeURIComponent(persona)}&limit=${limit}`,
    ),

  /** Up to 3 × 6 model calls with a real model: allow time. */
  evaluateLearning: (persona: string) =>
    request<EvaluationResult>("/learning/evaluate", { method: "POST", body: { persona }, timeoutMs: 180_000 }),

  /** Undo a rollback pin or a corrections pause. */
  clearLearningState: (persona: string) =>
    request<{ persona: string; state: LearningState }>(`/learning/state?persona=${encodeURIComponent(persona)}`, {
      method: "DELETE",
    }),

  graph: (limit = 60, persona = "") =>
    request<GraphData>(`/graph?limit=${limit}&persona=${encodeURIComponent(persona)}`),

  stats: () => request<Stats>("/stats"),

  audit: (limit = 50, beforeSeq?: number) =>
    request<AuditPage>(`/audit?limit=${limit}${beforeSeq != null ? `&before_seq=${beforeSeq}` : ""}`),

  auditVerify: () => request<AuditVerify>("/audit/verify", { timeoutMs: 30_000 }),

  /** Deletes all samples, drafts, draft edits and the style graph. */
  wipe: () => request<unknown>("/data/wipe", { method: "POST", body: { confirm: WIPE_PHRASE }, timeoutMs: 60_000 }),
};

export const DEFAULT_PERSONAS: Persona[] = [
  { name: "Default", description: "", builtin: true },
  { name: "Boss", description: "", builtin: true },
  { name: "Friend", description: "", builtin: true },
  { name: "Investor", description: "", builtin: true },
];

export const FALLBACK_CONFIG: AppConfig = { active_persona: "Default", hotkey: "CmdOrCtrl+Shift+P", inject_mode: "paste" };
