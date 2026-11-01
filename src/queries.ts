// Every piece of server / native state the UI reads, with its cache key and refresh policy
// in one place. Views call these hooks; they never fetch or poll on their own.
import { api, DEFAULT_PERSONAS, FALLBACK_CONFIG, type AppConfig } from "./api";
import { getQueryData, setQueryState, useQuery } from "./query";
import { inTauri, native } from "./tauri";

export const KEYS = {
  health: "health",
  personas: "personas",
  config: "config",
  drafts: "drafts",
  scores: "scores",
  graph: (persona: string) => `graph:${persona}`,
  stats: "stats",
  sources: "sources",
  perm: "native:perm",
  hotkey: "native:hotkey",
  capture: "native:capture",
  runningApps: "native:apps",
  audit: "audit",
} as const;

/** What a finished generation can change. */
export const AFTER_GENERATION = [KEYS.drafts, KEYS.scores, "graph:", KEYS.stats, KEYS.hotkey];
/** What learning from new writing can change. */
export const AFTER_INGEST = ["graph:", KEYS.stats, KEYS.sources];

// Poll intervals while the window is visible and focused (x4 when unfocused, paused when hidden).
// Everything else refreshes on focus and on native events (see App).
const HEALTH_POLL = 10_000;
const DRAFTS_POLL = 30_000; // generations also push a refresh via "generation-finished"
const NATIVE_POLL = 5_000; // IPC, no network; the tray can pause the hotkey behind our back

export const useHealth = () => useQuery(KEYS.health, api.health, { pollMs: HEALTH_POLL });

export function usePersonas() {
  const q = useQuery(KEYS.personas, api.personas);
  return q.data?.length ? q.data : DEFAULT_PERSONAS;
}

/** The persona list with its loading / error state (Settings → Personas). */
export const usePersonaList = () => useQuery(KEYS.personas, api.personas);

/** What adding, rewording or deleting a persona can change (a delete moves samples to Default). */
export const AFTER_PERSONA_CHANGE = [KEYS.personas, KEYS.config, "graph:", KEYS.stats];

export const useConfig = () => useQuery(KEYS.config, api.getConfig);

/** Optimistic partial config update; rolls back and surfaces the error on failure. */
export async function updateConfig(patch: Partial<AppConfig>): Promise<AppConfig> {
  const prev = getQueryData<AppConfig>(KEYS.config);
  setQueryState<AppConfig>(KEYS.config, { data: { ...(prev ?? FALLBACK_CONFIG), ...patch } });
  try {
    const saved = await api.putConfig(patch);
    setQueryState(KEYS.config, { data: saved, error: null });
    return saved;
  } catch (e) {
    setQueryState(KEYS.config, { data: prev, error: e });
    throw e;
  }
}

/** Apply a config change that already happened elsewhere (e.g. the tray's persona menu). */
export function patchConfigLocally(patch: Partial<AppConfig>) {
  const prev = getQueryData<AppConfig>(KEYS.config);
  setQueryState<AppConfig>(KEYS.config, { data: { ...(prev ?? FALLBACK_CONFIG), ...patch } });
}

export const useDrafts = () => useQuery(KEYS.drafts, () => api.drafts(50), { pollMs: DRAFTS_POLL });
export const useScoresSummary = () => useQuery(KEYS.scores, api.scoresSummary, { pollMs: DRAFTS_POLL });

export const useGraph = (persona: string) =>
  useQuery(KEYS.graph(persona), () => api.graph(60, persona), { keepPrevious: true });
export const useStats = () => useQuery(KEYS.stats, api.stats);
export const useSources = () => useQuery(KEYS.sources, api.ingestSources);
export const useAudit = () => useQuery(KEYS.audit, () => api.audit(50));

// ---------- Native (Tauri IPC). Disabled outside the app. ----------

const tauri = inTauri();

export const usePermissions = () => useQuery(tauri ? KEYS.perm : null, native.checkPermissions);
export const useHotkeyStatus = () => useQuery(tauri ? KEYS.hotkey : null, native.getHotkeyStatus, { pollMs: NATIVE_POLL });
export const useCaptureStatus = (enabled = true) =>
  useQuery(tauri && enabled ? KEYS.capture : null, native.getCaptureStatus, { pollMs: NATIVE_POLL });
export const useRunningApps = (enabled: boolean) =>
  useQuery(tauri && enabled ? KEYS.runningApps : null, native.listRunningApps);
