import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGet, ApiError } from "./api";

export type Route = {
  path: string;
  page: string;
  section: string;
};

function readRoute(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "").split("?")[0] || "dashboard";
  const [page, section = ""] = raw.split("/");
  return { path: raw, page: page || "dashboard", section };
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(readRoute);
  useEffect(() => {
    const update = () => setRoute(readRoute());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}

export function navigate(path: string): void {
  const next = path.replace(/^#\/?/, "");
  if (window.location.hash === `#/${next}`) return;
  window.location.hash = `#/${next}`;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

export type ApiState<T> = {
  data: T | null;
  loading: boolean;
  refreshing: boolean;
  error: ApiError | null;
  refresh: () => void;
  setData: (value: T | null) => void;
};

export function useApi<T = any>(
  path: string | null,
  options: {
    refreshMs?: number;
    refreshKey?: number;
    enabled?: boolean;
    operatorToken?: string;
  } = {},
): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(Boolean(path && options.enabled !== false));
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const version = useRef(0);
  const active = useRef(true);

  const load = useCallback(
    async (silent = false) => {
      if (!path || options.enabled === false) {
        setLoading(false);
        return;
      }
      const current = ++version.current;
      if (silent && data) setRefreshing(true);
      else if (!data) setLoading(true);
      setError(null);
      try {
        const body = await apiGet<T>(path, undefined, undefined, {
          operatorToken: options.operatorToken,
        });
        if (active.current && current === version.current) {
          setData(body);
        }
      } catch (caught: any) {
        if (caught?.name === "AbortError") return;
        if (active.current && current === version.current) {
          setError(caught instanceof ApiError ? caught : new ApiError(String(caught?.message || caught), 0));
        }
      } finally {
        if (active.current && current === version.current) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [path, options.enabled, options.operatorToken],
  );

  useEffect(() => {
    active.current = true;
    setData(null);
    setError(null);
    setLoading(Boolean(path && options.enabled !== false));
    void load(false);
    return () => {
      active.current = false;
      version.current += 1;
    };
  }, [path, options.enabled, load]);

  useEffect(() => {
    if (!path || !options.refreshMs) return;
    const timer = window.setInterval(() => void load(true), options.refreshMs);
    return () => window.clearInterval(timer);
  }, [path, options.refreshMs, load]);

  useEffect(() => {
    if (!path || !options.refreshKey) return;
    void load(true);
    // `load` is keyed by the request path; the revision is the refresh trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.refreshKey]);

  return {
    data,
    loading,
    refreshing,
    error,
    refresh: () => void load(true),
    setData,
  };
}

export function useDocumentTitle(value: string): void {
  useEffect(() => {
    document.title = `${value} · NovaMarket`;
  }, [value]);
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

export function useLocalState<T>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = window.localStorage.getItem(key);
      return stored ? (JSON.parse(stored) as T) : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (next: T) => {
      setValue(next);
      try {
        window.localStorage.setItem(key, JSON.stringify(next));
      } catch {
        /* storage can be disabled */
      }
    },
    [key],
  );
  return [value, set];
}

export function useDebounced<T>(value: T, delay = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function useInterval(callback: () => void, ms: number | null): void {
  const ref = useRef(callback);
  useEffect(() => {
    ref.current = callback;
  }, [callback]);
  useEffect(() => {
    if (!ms) return;
    const timer = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(timer);
  }, [ms]);
}

export function safeErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error ?? "Unknown error");
}

export function useMemoized<T>(factory: () => T, deps: any[]): T {
  return useMemo(factory, deps);
}
