import { useCallback, useEffect, useRef, useState } from "react";

export const ONBOARDING_KEY = "polytone.onboarding.dismissed";

export interface Resource<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/**
 * Fetch a resource, optionally polling. Keeps the last good data on error so
 * the UI doesn't flash empty when the backend hiccups.
 */
export function useResource<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  pollMs?: number,
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const seq = useRef(0);

  const run = useCallback(() => {
    const id = ++seq.current;
    setLoading(true);
    fetcherRef
      .current()
      .then((d) => {
        if (id !== seq.current) return;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (id !== seq.current) return;
        setError(e);
      })
      .finally(() => {
        if (id === seq.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    run();
    if (!pollMs) return;
    const t = setInterval(run, pollMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, pollMs, ...deps]);

  return { data, error, loading, reload: run };
}

export function useLocalStorage(key: string, initial: string | null): [string | null, (v: string | null) => void] {
  const [value, setValue] = useState<string | null>(() => {
    try {
      const v = localStorage.getItem(key);
      return v ?? initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: string | null) => {
      setValue(v);
      try {
        if (v === null) localStorage.removeItem(key);
        else localStorage.setItem(key, v);
      } catch {
        /* storage unavailable */
      }
    },
    [key],
  );
  return [value, set];
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(t).toLocaleDateString();
}
