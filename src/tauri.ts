// Thin, failure-tolerant wrappers around the Rust side. Every call degrades
// gracefully when the UI runs outside Tauri (e.g. `pnpm dev` in a browser).
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { errorMessage } from "./api";

// Fixed for the page's lifetime, so evaluate once.
const IN_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
export const inTauri = (): boolean => IN_TAURI;

export interface PermissionStatus {
  accessibility: boolean;
  platform: string;
  /** App macOS checks the permission against: a terminal/IDE when Polytone was started from one. */
  grant_to?: string;
  launched_from_other_app?: boolean;
}

export interface RunResult {
  ok: boolean;
  latency_ms: number;
  error: string | null;
}

export interface HotkeyStatus {
  hotkey: string;
  paused: boolean;
  last_run: RunResult | null;
}

export interface CaptureStatus {
  enabled: boolean;
  running: boolean;
  apps: string[];
  captured_this_session: number;
  last_captured_at: string | null;
  last_error: string | null;
}

export interface RunningApp {
  bundle_id: string;
  name: string;
}

export type PermissionKind = "accessibility" | "input_monitoring";

export class NotInTauri extends Error {
  constructor() {
    super("Not running inside the Polytone app — Tauri commands are unavailable in a plain browser.");
    this.name = "NotInTauri";
  }
}

async function call<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (!inTauri()) throw new NotInTauri();
  return invoke<T>(cmd, args);
}

export const native = {
  checkPermissions: () => call<PermissionStatus>("check_permissions"),
  openPermissionSettings: (kind: PermissionKind) => call<void>("open_permission_settings", { kind }),
  // Rust: set_hotkey(accel: String) -> Result<HotkeyStatus, String> (also persists to backend config)
  setHotkey: (accel: string) => call<HotkeyStatus>("set_hotkey", { accel }),
  getHotkeyStatus: () => call<HotkeyStatus>("get_hotkey_status"),
  // Rust: set_hotkey_paused(paused: bool) -> HotkeyStatus (mirrors the tray's "Pause hotkey")
  setHotkeyPaused: (paused: boolean) => call<HotkeyStatus>("set_hotkey_paused", { paused }),
  triggerNow: () => call<RunResult>("trigger_now"),
  // Typing capture ("Learn from what I type"). These never throw: outside Tauri, or
  // on an app build that predates the commands, they resolve to null / [].
  setCapture: (enabled: boolean, apps: string[]): Promise<{ status: CaptureStatus | null; error: string | null }> =>
    call<CaptureStatus>("set_capture", { enabled, apps })
      .then((status) => ({ status, error: null }))
      .catch((e) => ({ status: null, error: e instanceof NotInTauri ? null : errorMessage(e) })),
  getCaptureStatus: () => call<CaptureStatus>("get_capture_status").catch(() => null),
  listRunningApps: () =>
    call<RunningApp[]>("list_running_apps")
      .then((a) => (Array.isArray(a) ? a : []))
      .catch(() => [] as RunningApp[]),
};

export interface NativeEvents {
  "persona-changed": { persona: string };
  "generation-started": RunResult;
  "generation-finished": RunResult;
}

/** Subscribe to a Tauri event; returns a cleanup fn. No-op outside Tauri. */
export function onNative<K extends keyof NativeEvents>(
  name: K,
  handler: (payload: NativeEvents[K]) => void,
): () => void {
  if (!inTauri()) return () => {};
  let un: UnlistenFn | null = null;
  let cancelled = false;
  const safe = (fn: UnlistenFn) => {
    // unlisten may throw or return a rejected promise if the listener is already gone
    Promise.resolve()
      .then(() => fn())
      .catch(() => {});
  };
  listen<NativeEvents[K]>(name, (e) => handler(e.payload))
    .then((fn) => {
      if (cancelled) safe(fn);
      else un = fn;
    })
    .catch(() => {});
  return () => {
    cancelled = true;
    if (un) safe(un);
  };
}
