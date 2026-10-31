import { useCallback, useEffect, useState } from "react";
import { api, DEFAULT_PERSONAS, type AppConfig, type Health, type Persona } from "./api";
import { ONBOARDING_KEY, useLocalStorage, useResource } from "./hooks";
import { native, onNative, type RunResult } from "./tauri";
import { Header } from "./components/Header";
import { Tabs, type TabId } from "./components/Tabs";
import { Feed } from "./views/Feed";
import { Scoreboard } from "./views/Scoreboard";
import { GraphView } from "./views/Graph";
import { Settings } from "./views/Settings";

const TAB_KEY = "polytone.tab";

const FALLBACK_CONFIG: AppConfig = { active_persona: "Default", hotkey: "CmdOrCtrl+Shift+P", inject_mode: "paste" };

export default function App() {
  const [storedTab, setStoredTab] = useLocalStorage(TAB_KEY, "feed");
  const tab = (["feed", "scoreboard", "graph", "settings"].includes(storedTab ?? "") ? storedTab : "feed") as TabId;
  const setTab = (t: TabId) => setStoredTab(t);

  const health = useResource<Health>(api.health, [], 5000);
  const online = health.data != null && !health.error;

  const personasRes = useResource<Persona[]>(api.personas, [online]);
  const personas = personasRes.data?.length ? personasRes.data : DEFAULT_PERSONAS;

  const [config, setConfig] = useState<AppConfig | null>(null);
  const [configError, setConfigError] = useState<unknown>(null);
  const loadConfig = useCallback(() => {
    api
      .getConfig()
      .then((c) => {
        setConfig(c);
        setConfigError(null);
      })
      .catch(setConfigError);
  }, []);
  useEffect(() => {
    if (online) loadConfig();
  }, [online, loadConfig]);

  const updateConfig = useCallback(
    async (patch: Partial<AppConfig>) => {
      const next = { ...(config ?? FALLBACK_CONFIG), ...patch };
      const prev = config;
      setConfig(next); // optimistic
      try {
        const saved = await api.putConfig(next);
        setConfig(saved);
        setConfigError(null);
        return saved;
      } catch (e) {
        setConfig(prev);
        setConfigError(e);
        throw e;
      }
    },
    [config],
  );

  // Native events: persona changes from the tray, generation lifecycle.
  const [generating, setGenerating] = useState(false);
  const [lastRun, setLastRun] = useState<RunResult | null>(null);
  const [dataTick, setDataTick] = useState(0);
  useEffect(() => {
    const offs = [
      onNative("persona-changed", ({ persona }) =>
        setConfig((c) => (c ? { ...c, active_persona: persona } : { ...FALLBACK_CONFIG, active_persona: persona })),
      ),
      onNative("generation-started", () => setGenerating(true)),
      onNative("generation-finished", (r) => {
        setGenerating(false);
        setLastRun(r);
        setDataTick((n) => n + 1);
      }),
    ];
    return () => offs.forEach((off) => off());
  }, []);

  // First launch: jump to Settings if accessibility is missing and the guide wasn't dismissed.
  const [dismissed] = useLocalStorage(ONBOARDING_KEY, null);
  useEffect(() => {
    if (dismissed) return;
    native
      .checkPermissions()
      .then((p) => {
        if (!p.accessibility) setStoredTab("settings");
      })
      .catch(() => {});
    // run once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="mx-auto flex h-full max-w-[560px] flex-col">
      <Header
        health={health.data}
        healthError={health.error}
        healthLoading={health.loading && !health.data && !health.error}
        personas={personas}
        active={config?.active_persona ?? null}
        onPersona={(p) => updateConfig({ active_persona: p }).catch(() => {})}
        personaDisabled={!online}
        generating={generating}
      />
      <Tabs value={tab} onChange={setTab} />
      <main className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-4 pb-5 pt-3" key={tab}>
        {tab === "feed" && (
          <Feed personas={personas} activePersona={config?.active_persona ?? null} online={online} />
        )}
        {tab === "scoreboard" && <Scoreboard tick={dataTick} hotkey={config?.hotkey ?? FALLBACK_CONFIG.hotkey} />}
        {tab === "graph" && <GraphView personas={personas} tick={dataTick} />}
        {tab === "settings" && (
          <Settings
            config={config}
            configError={configError}
            online={online}
            health={health.data}
            onUpdate={updateConfig}
            lastRunFromEvent={lastRun}
            generating={generating}
          />
        )}
      </main>
    </div>
  );
}
