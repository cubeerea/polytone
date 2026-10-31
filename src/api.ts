// Typed client for the Polytone Python backend. Mirrors CONTRACTS.md exactly.

export const API_BASE = "http://127.0.0.1:8765";
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

export interface Constraints {
  avg_sentence_len: number;
  sentence_len_p90: number;
  top_openers: string[];
  top_transitions: string[];
  top_bigrams: [string, string, number][];
  punctuation_rates: Record<string, number>;
  structures: { signature: string; weight: number }[];
  lexicon: string[];
  casing: "lower" | "sentence" | "mixed";
  contractions_rate: number;
  sample_count: number;
}

export interface GenerateRequest {
  intent: string;
  persona: string | null;
  app_context: string | null;
}

export interface GenerateResponse {
  draft_id: string;
  text: string;
  persona: string;
  latency_ms: number;
  constraints: Constraints;
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
  name: SourceName | string;
  enabled: boolean;
  status: string;
  last_run: string | null;
  items_ingested: number;
}

export interface Persona {
  name: string;
  description: string;
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
}

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

export function errorMessage(err: unknown): string {
  if (isOffline(err)) return OFFLINE_HINT;
  if (err instanceof Error) return err.message;
  return String(err);
}

// ---------- Core request ----------

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), init?.timeoutMs ?? 10_000);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(OFFLINE_HINT, null, true);
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    let code: string | null = null;
    try {
      const body = await res.json();
      if (body && typeof body.error === "string") code = body.error;
      if (body && typeof body.detail === "string") detail = body.detail;
      else if (body && body.detail) detail = JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    if (code === "llm_timeout") detail = `The model timed out — ${detail}`;
    else if (code === "llm_failed") detail = `The model failed — ${detail}`;
    throw new ApiError(detail, res.status, false, code);
  }
  return (await res.json()) as T;
}

const json = (body: unknown): string => JSON.stringify(body);

// ---------- Endpoints ----------

export const api = {
  health: () => request<Health>("/health", { timeoutMs: 3000 }),

  generate: (req: GenerateRequest) =>
    request<GenerateResponse>("/generate", { method: "POST", body: json(req), timeoutMs: 15_000 }),

  ingestManual: (text: string, persona: string | null) =>
    request<IngestResult>("/ingest/manual", {
      method: "POST",
      body: json({ text, persona }),
      timeoutMs: 30_000,
    }),

  ingestSources: () => request<IngestSource[]>("/ingest/sources"),

  runSource: (name: string) =>
    request<IngestResult>(`/ingest/sources/${encodeURIComponent(name)}/run`, {
      method: "POST",
      timeoutMs: 120_000,
    }),

  personas: () => request<Persona[]>("/personas"),

  getConfig: () => request<AppConfig>("/config"),

  putConfig: (cfg: AppConfig) => request<AppConfig>("/config", { method: "PUT", body: json(cfg) }),

  drafts: (limit = 50) => request<Draft[]>(`/drafts?limit=${limit}`),

  scoresSummary: () => request<ScoresSummary>("/scores/summary"),

  graph: (limit = 60, persona = "") =>
    request<GraphData>(`/graph?limit=${limit}&persona=${encodeURIComponent(persona)}`),

  stats: () => request<Stats>("/stats"),
};

export const DEFAULT_PERSONAS: Persona[] = [
  { name: "Boss", description: "" },
  { name: "Friend", description: "" },
  { name: "Investor", description: "" },
  { name: "Default", description: "" },
];
