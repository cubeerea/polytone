// A tiny shared data layer: one cache entry per key, deduped in-flight requests, and ONE
// timer for every poller. Polling stops while the window is hidden, slows down while it is
// visible but unfocused, and active queries revalidate when the window comes back.
// Polytone sits in the menu bar for hours, so idle cost matters more than freshness.
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";

export interface QueryState<T> {
  data: T | null;
  error: unknown;
  /** True only for the first load: a fetch is running and there is no data or error yet. */
  loading: boolean;
  /** When the last fetch settled (ms epoch); 0 = never. */
  updatedAt: number;
}

export interface Query<T> extends QueryState<T> {
  /** Refetch now (or right after the request already in flight). */
  reload: () => void;
}

interface Entry {
  state: QueryState<unknown>;
  fetcher: (() => Promise<unknown>) | null;
  inflight: Promise<void> | null;
  startedAt: number;
  invalidatedAt: number;
  /** Bumped by setQueryState; responses that started before it are dropped. */
  version: number;
  listeners: Set<() => void>;
  /** Mounted useQuery hooks (id → poll interval, 0 = no polling). */
  subscribers: Map<number, number>;
}

/** While visible but unfocused, pollers run this many times slower. */
const UNFOCUSED_FACTOR = 4;
/** On focus / mount, data younger than this is reused instead of refetched. */
const STALE_MS = 10_000;

const IDLE: QueryState<never> = { data: null, error: null, loading: false, updatedAt: 0 };
const cache = new Map<string, Entry>();

function entry(key: string): Entry {
  let e = cache.get(key);
  if (!e) {
    e = {
      state: IDLE,
      fetcher: null,
      inflight: null,
      startedAt: 0,
      invalidatedAt: 0,
      version: 0,
      listeners: new Set(),
      subscribers: new Map(),
    };
    cache.set(key, e);
  }
  return e;
}

function commit(e: Entry, patch: Partial<QueryState<unknown>>) {
  const next = { ...e.state, ...patch };
  if (next.data === e.state.data && next.error === e.state.error && next.loading === e.state.loading) {
    e.state = next; // updatedAt only: nobody re-renders for that
    return;
  }
  e.state = next;
  e.listeners.forEach((l) => l());
}

/** Structural sharing: an unchanged poll result keeps the old reference, so nothing re-renders. */
function deepEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  const ra = a as Record<string, unknown>;
  const rb = b as Record<string, unknown>;
  return ka.every((k) => k in rb && deepEqual(ra[k], rb[k]));
}

/** Same failure as last time? Keep the old error object so repeated offline polls don't re-render. */
function sameError(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (!(a instanceof Error) || !(b instanceof Error)) return false;
  return a.name === b.name && a.message === b.message;
}

const visible = () => typeof document === "undefined" || document.visibilityState !== "hidden";
const focused = () => typeof document === "undefined" || document.hasFocus();
const isStale = (e: Entry, maxAge: number) =>
  e.state.updatedAt === 0 || e.invalidatedAt > e.startedAt || Date.now() - e.state.updatedAt > maxAge;

/** Fetch `key` now unless a request is already in flight (then share it). */
function fetchQuery(key: string): Promise<void> {
  const e = entry(key);
  if (e.inflight) return e.inflight;
  if (!e.fetcher) return Promise.resolve();
  const version = e.version;
  e.startedAt = Date.now();
  if (e.state.data == null && e.state.error == null) commit(e, { loading: true });
  e.inflight = e
    .fetcher()
    .then(
      (data) => {
        if (e.version !== version) return;
        commit(e, {
          data: deepEqual(e.state.data, data) ? e.state.data : data,
          error: null,
          updatedAt: Date.now(),
        });
      },
      (error: unknown) => {
        if (e.version !== version) return;
        commit(e, { error: sameError(e.state.error, error) ? e.state.error : error, updatedAt: Date.now() });
      },
    )
    .finally(() => {
      e.inflight = null;
      if (e.state.loading) commit(e, { loading: false });
      // Invalidated while this request was running: its answer may already be old.
      if (e.invalidatedAt > e.startedAt && e.subscribers.size > 0 && visible()) void fetchQuery(key);
      else schedule();
    });
  return e.inflight;
}

/** Register a fetcher for `key` and load it, deduped with any request in flight. */
export function prefetchQuery<T>(key: string, fetcher: () => Promise<T>): Promise<T | null> {
  const e = entry(key);
  e.fetcher ??= fetcher;
  return fetchQuery(key).then(() => e.state.data as T | null);
}

export function getQueryData<T>(key: string): T | null {
  return (cache.get(key)?.state.data as T | null | undefined) ?? null;
}

/** Write a query's state directly (optimistic updates). Drops any response already in flight. */
export function setQueryState<T>(key: string, patch: Partial<Pick<QueryState<T>, "data" | "error">>) {
  const e = entry(key);
  e.version++;
  commit(e, { ...patch, loading: false, updatedAt: Date.now() });
}

/**
 * Mark queries stale. Mounted ones refetch now (or when the window is next shown);
 * unmounted ones refetch the next time something mounts them.
 */
export function invalidateQueries(match: string | string[] | ((key: string) => boolean)) {
  const test =
    typeof match === "function"
      ? match
      : (k: string) => (Array.isArray(match) ? match : [match]).some((p) => k === p || k.startsWith(p));
  const now = Date.now();
  for (const [key, e] of cache) {
    if (!test(key)) continue;
    e.invalidatedAt = now;
    if (e.subscribers.size > 0 && visible()) void fetchQuery(key);
  }
}

/**
 * Forget queries' data and errors (on lock: nothing the engine returned stays in memory).
 * Responses in flight are dropped; whatever mounts next fetches afresh.
 */
export function resetQueries(match: (key: string) => boolean) {
  for (const [key, e] of cache) {
    if (!match(key)) continue;
    e.version++;
    e.state = IDLE;
    e.listeners.forEach((l) => l());
  }
}

// ---------- The one polling timer ----------

let timer: ReturnType<typeof setTimeout> | null = null;

const pollInterval = (e: Entry): number => {
  let ms = 0;
  for (const p of e.subscribers.values()) if (p > 0 && (ms === 0 || p < ms)) ms = p;
  return ms;
};

function schedule() {
  if (timer) clearTimeout(timer);
  timer = null;
  if (!visible()) return;
  const factor = focused() ? 1 : UNFOCUSED_FACTOR;
  let next = Infinity;
  for (const e of cache.values()) {
    const p = pollInterval(e);
    if (p > 0 && !e.inflight) next = Math.min(next, e.state.updatedAt + p * factor);
  }
  if (next === Infinity) return;
  timer = setTimeout(tick, Math.max(250, next - Date.now()));
}

function tick() {
  timer = null;
  const factor = focused() ? 1 : UNFOCUSED_FACTOR;
  // Anything due within the next second goes now, so pollers share wake-ups.
  const horizon = Date.now() + 1000;
  for (const [key, e] of cache) {
    const p = pollInterval(e);
    if (p > 0 && !e.inflight && e.state.updatedAt + p * factor <= horizon) void fetchQuery(key);
  }
  schedule();
}

function revalidateMounted() {
  for (const [key, e] of cache) if (e.subscribers.size > 0 && isStale(e, STALE_MS)) void fetchQuery(key);
  schedule();
}

if (typeof window !== "undefined") {
  document.addEventListener("visibilitychange", () => (visible() ? revalidateMounted() : schedule()));
  window.addEventListener("focus", revalidateMounted);
  window.addEventListener("blur", schedule);
}

// ---------- React hook ----------

let nextId = 1;

/**
 * Subscribe to a shared query. `key` = null disables it. Hooks with the same key share one
 * cache entry and one request; `pollMs` keeps it fresh while the window is visible.
 */
export function useQuery<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  opts: { pollMs?: number; keepPrevious?: boolean } = {},
): Query<T> {
  const { pollMs = 0, keepPrevious = false } = opts;
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const subscribe = useCallback(
    (cb: () => void) => {
      if (!key) return () => {};
      const ls = entry(key).listeners;
      ls.add(cb);
      return () => ls.delete(cb);
    },
    [key],
  );
  const snapshot = useCallback(() => (key ? entry(key).state : IDLE), [key]);
  const state = useSyncExternalStore(subscribe, snapshot) as QueryState<T>;

  useEffect(() => {
    if (!key) return;
    const e = entry(key);
    e.fetcher = () => fetcherRef.current();
    const id = nextId++;
    e.subscribers.set(id, pollMs);
    if (isStale(e, STALE_MS) && visible()) void fetchQuery(key);
    else schedule();
    return () => {
      e.subscribers.delete(id);
      schedule();
    };
  }, [key, pollMs]);

  const reload = useCallback(() => {
    if (key) invalidateQueries((k) => k === key);
  }, [key]);

  // keepPrevious: while a new key loads (e.g. another persona), keep showing the old data.
  const prev = useRef<T | null>(null);
  if (state.data != null) prev.current = state.data;
  const data = state.data ?? (keepPrevious ? prev.current : null);

  return useMemo(() => ({ ...state, data, reload }), [state, data, reload]);
}
