/**
 * Operator token storage.
 *
 * The token is typed in by the operator at runtime and kept in sessionStorage
 * (cleared when the tab closes), with an in-memory fallback when storage is
 * unavailable. It is never built into the bundle, written to localStorage or
 * cookies, put in a URL, or logged.
 */
const KEY = "aq.operator-token";
let memory: string | null = null;

export function loadToken(): string | null {
  try {
    return window.sessionStorage.getItem(KEY) ?? memory;
  } catch {
    return memory;
  }
}

export function saveToken(token: string): void {
  memory = token;
  try {
    window.sessionStorage.setItem(KEY, token);
  } catch {
    /* in-memory only */
  }
}

export function clearToken(): void {
  memory = null;
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    /* nothing stored */
  }
}
