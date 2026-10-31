import { useCallback, useEffect, useRef, useState } from "react";
import { FALLBACK_CONFIG, isOffline } from "./api";
import { ONBOARDING_KEY, useLocalStorage } from "./hooks";
import { invalidateQueries, prefetchQuery } from "./query";
import {
  AFTER_GENERATION,
  KEYS,
  patchConfigLocally,
  updateConfig,
  useConfig,
  useHealth,
  usePersonas,
} from "./queries";
import { inTauri, native, onNative, type RunResult } from "./tauri";
import { Header, type ViewId } from "./components/Header";
import { Drafts } from "./views/Drafts";
import { Style } from "./views/Style";
import { Settings } from "./views/Settings";
import { BrowserOnly, LockScreen } from "./components/LockScreen";
import { lockApp, lockUi, useLocked } from "./lock";

const TAB_KEY = "polytone.tab";
const VIEWS: readonly ViewId[] = ["drafts", "style", "settings"];
// Older builds stored feed/scoreboard/graph; map them onto the merged views.
const LEGACY: Record<string, ViewId> = { feed: "style", graph: "style", scoreboard: "drafts" };

/** Load the d3 graph chunk once the first paint is done, so the Style tab opens instantly. */
function prefetchGraphChunk() {
  const load = () => void import("./components/StyleGraph");
  if (typeof window.requestIdleCallback === "function") window.requestIdleCallback(load, { timeout: 3000 });
  else setTimeout(load, 1500);
}

const DEFAULT_IDLE_MINUTES = 15;
const ACTIVITY = ["pointermove", "pointerdown", "keydown", "wheel", "touchstart"] as const;

/**
 * Lock after `minutes` without mouse / keyboard activity in this window (the engine has its
 * own sliding timeout; this one locks the moment it is due, even while polls keep that alive).
 */
function useIdleLock(minutes: number) {
  useEffect(() => {
    const ms = Math.min(240, Math.max(1, minutes)) * 60_000;
    let last = Date.now();
    const bump = () => {
      last = Date.now();
    };
    const check = () => {
      if (Date.now() - last >= ms) lockApp("idle");
    };
    for (const e of ACTIVITY) window.addEventListener(e, bump, { passive: true, capture: true });
    // Timers are throttled while hidden: also check the moment the window comes back.
    const timer = setInterval(check, 15_000);
    document.addEventListener("visibilitychange", check);
    window.addEventListener("focus", check);
    return () => {
      for (const e of ACTIVITY) window.removeEventListener(e, bump, { capture: true });
      clearInterval(timer);
      document.removeEventListener("visibilitychange", check);
      window.removeEventListener("focus", check);
    };
  }, [minutes]);
}

export default function App() {
  // A plain browser can reach the engine but has no way to unlock: show none of the data.
  if (!inTauri()) return <BrowserOnly />;
  return <Gate />;
}

function Gate() {
  const locked = useLocked();
  // Rust locked (screen lock, sleep, the tray's "Lock Polytone").
  useEffect(() => onNative("locked", () => lockUi()), []);
  return locked ? <LockScreen /> : <Main />;
}

function Main() {
  const [storedTab, setStoredTab] = useLocalStorage(TAB_KEY, "drafts");
  const tab: ViewId = VIEWS.includes(storedTab as ViewId) ? (storedTab as ViewId) : (LEGACY[storedTab ?? ""] ?? "drafts");
  const setTab = useCallback((t: ViewId) => setStoredTab(t), [setStoredTab]);

  const health = useHealth();
  const online = health.data != null && !health.error;
  const personas = usePersonas();
  const config = useConfig();
  useIdleLock(config.data?.lock_idle_minutes ?? DEFAULT_IDLE_MINUTES);

  // Engine came back after being offline: everything we hold may be out of date.
  const wasOffline = useRef(false);
  useEffect(() => {
    if (isOffline(health.error)) wasOffline.current = true;
    else if (online && wasOffline.current) {
      wasOffline.current = false;
      invalidateQueries((k) => k !== KEYS.health && !k.startsWith("native:"));
    }
  }, [online, health.error]);

  // Native events: persona changes from the tray, generation lifecycle.
  const [generating, setGenerating] = useState(false);
  const [lastRun, setLastRun] = useState<RunResult | null>(null);
  useEffect(() => {
    const offs = [
      onNative("persona-changed", ({ persona }) => patchConfigLocally({ active_persona: persona })),
      onNative("generation-started", () => setGenerating(true)),
      onNative("generation-finished", (r) => {
        setGenerating(false);
        setLastRun(r);
        invalidateQueries(AFTER_GENERATION);
      }),
    ];
    return () => offs.forEach((off) => off());
  }, []);

  // First launch: jump to Settings if accessibility is missing and the guide wasn't dismissed.
  const [dismissed] = useLocalStorage(ONBOARDING_KEY, null);
  useEffect(() => {
    prefetchGraphChunk();
    if (dismissed || !inTauri()) return;
    prefetchQuery(KEYS.perm, native.checkPermissions)
      .then((p) => {
        if (p && !p.accessibility) setStoredTab("settings");
      })
      .catch(() => {});
    // run once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const active = config.data?.active_persona ?? null;
  const onPersona = useCallback((p: string) => void updateConfig({ active_persona: p }).catch(() => {}), []);

  return (
    <div className="mx-auto flex h-full max-w-[560px] flex-col">
      <Header
        view={tab}
        onView={setTab}
        health={health.data}
        healthError={health.error}
        healthLoading={health.loading}
        personas={personas}
        active={active}
        onPersona={onPersona}
        personaDisabled={!online}
        generating={generating}
      />
      <main className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-4 pb-6" key={tab}>
        {tab === "drafts" && <Drafts hotkey={config.data?.hotkey ?? FALLBACK_CONFIG.hotkey} />}
        {tab === "style" && <Style personas={personas} activePersona={active} online={online} />}
        {tab === "settings" && (
          <Settings online={online} health={health.data} lastRunFromEvent={lastRun} generating={generating} />
        )}
      </main>
    </div>
  );
}
