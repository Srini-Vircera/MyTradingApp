/**
 * The operator's display name for the audit trail (not a secret). Kept in
 * sessionStorage for the tab only, like the token, so forms can prefill it.
 */
const KEY = "aq.operator-name";
let memory = "";

export function loadOperator(): string {
  try {
    return window.sessionStorage.getItem(KEY) ?? memory;
  } catch {
    return memory;
  }
}

export function saveOperator(name: string): void {
  memory = name;
  try {
    window.sessionStorage.setItem(KEY, name);
  } catch {
    /* in-memory only */
  }
}
