"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError, getJson, type ReadPath, type ReadQuery, type ReadResponse } from "./api";
import { clearToken, loadToken, saveToken } from "./token";

interface Session {
  token: string | null;
  notice: string | null;
  signIn: (token: string) => void;
  signOut: (notice?: string) => void;
}

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setToken(loadToken());
    setReady(true);
  }, []);

  const signIn = useCallback((t: string) => {
    saveToken(t);
    setNotice(null);
    setToken(t);
  }, []);
  const signOut = useCallback((n?: string) => {
    clearToken();
    setNotice(n ?? null);
    setToken(null);
  }, []);

  if (!ready) return null;
  return (
    <SessionContext.Provider value={{ token, notice, signIn, signOut }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): Session {
  const s = useContext(SessionContext);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}

export interface Loaded<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Load a read endpoint, refreshing every ``refreshMs``. A 401 signs the
 * operator out (the token is wrong or was rotated).
 */
export function useApi<P extends ReadPath>(
  path: P,
  query?: ReadQuery<P>,
  refreshMs = 60_000,
): Loaded<ReadResponse<P>> {
  const { token, signOut } = useSession();
  const [data, setData] = useState<ReadResponse<P> | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const key = JSON.stringify(query ?? {});
  const queryRef = useRef(query);
  queryRef.current = query;

  useEffect(() => {
    if (!token) return;
    const ctl = new AbortController();
    setLoading(true);
    getJson(token, path, queryRef.current, ctl.signal)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((exc: unknown) => {
        if (ctl.signal.aborted) return;
        const err =
          exc instanceof ApiError ? exc : new ApiError(0, "unexpected error loading data");
        if (err.unauthorized) signOut("The API rejected the token. Enter it again.");
        setError(err);
      })
      .finally(() => {
        if (!ctl.signal.aborted) setLoading(false);
      });
    return () => ctl.abort();
  }, [token, path, key, tick, signOut]);

  useEffect(() => {
    if (refreshMs <= 0) return;
    const id = window.setInterval(() => setTick((t) => t + 1), refreshMs);
    return () => window.clearInterval(id);
  }, [refreshMs]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
