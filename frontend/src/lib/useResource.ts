/**
 * Small data hooks for the State panel.
 *
 * `useResource` fetches when its inputs change, ignores a response that arrives after
 * a newer request started (the model changing twice in a second must not leave the
 * older answer on screen), keeps the last good data while a refresh is in flight so
 * the panel does not blank on every journal bump, and exposes `reload`.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

export function useResource<T>(
  fetcher: () => Promise<T>,
  deps: readonly unknown[],
  enabled = true,
  debounceMs = 0,
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);
  const latest = useRef(0);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;

  useEffect(() => {
    if (!enabled) return undefined;
    const ticket = ++latest.current;
    setLoading(true);
    const timer = window.setTimeout(() => {
      fetchRef.current().then(
        (value) => {
          if (ticket !== latest.current) return;
          setData(value);
          setError(null);
          setLoading(false);
        },
        (e: unknown) => {
          if (ticket !== latest.current) return;
          setError(e instanceof Error ? e.message : 'Request failed.');
          setLoading(false);
        },
      );
    }, debounceMs);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, nonce, debounceMs]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

/** Whether the tab is in front: polling a hidden tab only burns the engine's time. */
export function useVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden');
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', on);
    return () => document.removeEventListener('visibilitychange', on);
  }, []);
  return visible;
}

/** `true` while the media query matches; follows resizes. */
export function useMediaQuery(query: string): boolean {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatch(mq.matches);
    on();
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [query]);
  return match;
}

/** A value that ticks once a second while `active` (for live elapsed timers). */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

/**
 * localStorage as a versioned blob. A shape that changes between releases must not
 * hand a returning user an object the code no longer understands: anything that
 * does not carry the current version is ignored, and the default is used.
 */
export function loadBlob<T>(key: string, version: number, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as { v?: number; data?: T };
    return parsed.v === version && parsed.data !== undefined ? parsed.data : fallback;
  } catch {
    return fallback;
  }
}

export function saveBlob<T>(key: string, version: number, data: T): void {
  try {
    window.localStorage.setItem(key, JSON.stringify({ v: version, data }));
  } catch {
    /* storage full or disabled: the layout simply will not persist */
  }
}
