// The UI lock. Starts locked; unlocking goes through Rust (Touch ID / Mac password, then
// POST /session). The session token lives only in api.ts's memory. Any 401 locks again.
import { useSyncExternalStore } from "react";
import { onSessionRejected, setSessionToken } from "./api";
import { invalidateQueries, resetQueries } from "./query";
import { native, type LockReason } from "./tauri";

let locked = true;
const listeners = new Set<() => void>();

function set(v: boolean) {
  if (locked === v) return;
  locked = v;
  listeners.forEach((l) => l());
}

/** Everything the engine returned, except health (unauthenticated, no user content). */
const engineData = (k: string) => k !== "health" && !k.startsWith("native:");

/** Lock the window now: drop the session token and every cached response. */
export function lockUi() {
  setSessionToken(null);
  set(true);
  resetQueries(engineData);
}

/** Lock the window and the app (revokes sessions, audits). */
export function lockApp(reason: LockReason) {
  lockUi();
  void native.lock(reason);
}

export function unlockWith(token: string) {
  setSessionToken(token);
  invalidateQueries(engineData);
  set(false);
}

export const isUiLocked = () => locked;

export function useLocked(): boolean {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => locked,
  );
}

// The engine rejected our session (idle timeout, 12 h cap, revoked): lock, and tell Rust so
// its state matches (nothing left to revoke).
onSessionRejected(() => {
  if (!locked) lockApp("expired");
});
