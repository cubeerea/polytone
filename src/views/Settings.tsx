import { useCallback, useEffect, useState, type KeyboardEvent, type ReactNode } from "react";
import { errorMessage, type AppConfig, type Health, type InjectMode } from "../api";
import { ONBOARDING_KEY, relativeTime, useLocalStorage } from "../hooks";
import {
  inTauri,
  native,
  nativeErrorMessage,
  type CaptureStatus,
  type HotkeyStatus,
  type PermissionKind,
  type PermissionStatus,
  type RunResult,
  type RunningApp,
} from "../tauri";
import { Disclosure, ErrorLine, StatusDot, Switch, prettyHotkey } from "../components/bits";
import { MinusIcon } from "../components/Icons";

interface Props {
  config: AppConfig | null;
  configError: unknown;
  online: boolean;
  health: Health | null;
  onUpdate: (patch: Partial<AppConfig>) => Promise<AppConfig>;
  lastRunFromEvent: RunResult | null;
  generating: boolean;
}

export function Settings({ config, configError, online, health, onUpdate, lastRunFromEvent, generating }: Props) {
  const tauri = inTauri();
  const [perm, setPerm] = useState<PermissionStatus | null>(null);
  const [permError, setPermError] = useState<string | null>(null);
  const [status, setStatus] = useState<HotkeyStatus | null>(null);

  const refreshPerm = useCallback(() => {
    native
      .checkPermissions()
      .then((p) => {
        setPerm(p);
        setPermError(null);
      })
      .catch((e) => setPermError(nativeErrorMessage(e)));
  }, []);

  const refreshStatus = useCallback(() => {
    native
      .getHotkeyStatus()
      .then(setStatus)
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshPerm();
    refreshStatus();
    // re-check when the user comes back from System Settings
    const onFocus = () => {
      refreshPerm();
      refreshStatus();
    };
    window.addEventListener("focus", onFocus);
    const t = setInterval(refreshStatus, 4000);
    return () => {
      window.removeEventListener("focus", onFocus);
      clearInterval(t);
    };
  }, [refreshPerm, refreshStatus]);

  useEffect(() => {
    if (lastRunFromEvent) refreshStatus();
  }, [lastRunFromEvent, refreshStatus]);

  return (
    <div className="space-y-7 pt-1">
      {tauri ? (
        perm && !perm.accessibility && <PermissionsCard perm={perm} onRecheck={refreshPerm} />
      ) : (
        <p className="rounded-lg bg-fill px-3 py-2 text-[12px] text-text-2">
          Running in a browser. The shortcut, permissions and typing capture only work inside the Polytone app.
        </p>
      )}

      <ShortcutGroup tauri={tauri} online={online} config={config} status={status} onUpdate={onUpdate} onSaved={refreshStatus} />
      {configError != null && <ErrorLine error={configError} />}

      <CaptureGroup tauri={tauri} online={online} config={config} onUpdate={onUpdate} />

      <Disclosure title="Advanced">
        <AdvancedGroup
          tauri={tauri}
          online={online}
          health={health}
          perm={perm}
          permError={permError}
          status={status}
          lastRun={status?.last_run ?? lastRunFromEvent}
          generating={generating}
          onTriggered={refreshStatus}
        />
      </Disclosure>
    </div>
  );
}

function Row({ title, subtitle, children }: { title: ReactNode; subtitle?: ReactNode; children?: ReactNode }) {
  return (
    <div className="row">
      <div className="min-w-0 flex-1">
        <div className="text-[13px]">{title}</div>
        {subtitle && <div className="text-[11.5px] leading-snug text-text-3">{subtitle}</div>}
      </div>
      {children}
    </div>
  );
}

/* ---------------- Permissions (first launch) ---------------- */

function PermissionsCard({ perm, onRecheck }: { perm: PermissionStatus; onRecheck: () => void }) {
  const [dismissed, setDismissed] = useLocalStorage(ONBOARDING_KEY, null);
  const host = perm.launched_from_other_app ? perm.grant_to : undefined;
  const [openErr, setOpenErr] = useState<string | null>(null);

  const open = async (kind: PermissionKind) => {
    setOpenErr(null);
    try {
      await native.openPermissionSettings(kind);
    } catch (e) {
      setOpenErr(nativeErrorMessage(e));
    }
  };

  if (dismissed) {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-fill px-3 py-2 text-[12px]">
        <StatusDot state="down" />
        <span className="flex-1 text-text-2">
          {host ? `Accessibility is off for ${host}, so the shortcut can't paste.` : "Accessibility access is off, so the shortcut can't paste."}
        </span>
        <button type="button" className="btn btn-plain" onClick={() => setDismissed(null)}>
          Set up
        </button>
      </div>
    );
  }

  return (
    <section className="fade-in">
      <h2 className="text-[15px] font-semibold">Allow Polytone to paste for you</h2>
      <p className="mt-1 text-[13px] text-text-2">
        Polytone reads your selection and types the rewrite back, so macOS needs to trust it.
      </p>
      <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-[13px] text-text-2 marker:text-text-3">
        {host ? (
          <li>
            Polytone was started from <span className="text-text">{host}</span>, so macOS checks {host}&apos;s
            permission, not Polytone&apos;s. In{" "}
            <span className="text-text">Privacy &amp; Security → Accessibility</span>, switch{" "}
            <span className="text-text">{host}</span> on (add it with + if it isn&apos;t listed).
          </li>
        ) : (
          <li>
            In <span className="text-text">Privacy &amp; Security → Accessibility</span>, switch Polytone on.
          </li>
        )}
        <li>
          If the shortcut still does nothing, also allow it under{" "}
          <button type="button" className="btn-plain btn h-auto p-0 text-[13px]" onClick={() => open("input_monitoring")}>
            Input Monitoring
          </button>
          .
        </li>
        <li>{host ? `Quit and reopen ${host}, then start Polytone again.` : "Come back here. You may need to relaunch Polytone."}</li>
      </ol>
      <div className="mt-3 flex items-center gap-2">
        <button type="button" className="btn btn-primary" onClick={() => open("accessibility")}>
          Open System Settings
        </button>
        <button type="button" className="btn" onClick={onRecheck}>
          Check again
        </button>
        <button type="button" className="btn btn-plain ml-auto text-text-3" onClick={() => setDismissed("1")}>
          Later
        </button>
      </div>
      {openErr && <p className="mt-2 text-[12px] text-bad">{openErr}</p>}
    </section>
  );
}

/* ---------------- Shortcut + delivery ---------------- */

const MOD_ORDER = ["CmdOrCtrl", "Ctrl", "Alt", "Shift"] as const;

function accelFromEvent(e: KeyboardEvent): string | null {
  const mods: string[] = [];
  if (e.metaKey) mods.push("CmdOrCtrl");
  if (e.ctrlKey) mods.push(e.metaKey ? "Ctrl" : "CmdOrCtrl");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  const k = e.key;
  if (["Meta", "Control", "Alt", "Shift", "CapsLock", "Tab"].includes(k)) return null;
  if (mods.length === 0) return null;
  let key: string;
  if (/^Key[A-Z]$/.test(e.code)) key = e.code.slice(3);
  else if (/^Digit\d$/.test(e.code)) key = e.code.slice(5);
  else if (/^F\d{1,2}$/.test(k)) key = k;
  else if (k === " ") key = "Space";
  else if (k.length === 1) key = k.toUpperCase();
  else key = k;
  const ordered = MOD_ORDER.filter((m) => mods.includes(m));
  return [...ordered, key].join("+");
}

function ShortcutGroup({
  tauri,
  online,
  config,
  status,
  onUpdate,
  onSaved,
}: {
  tauri: boolean;
  online: boolean;
  config: AppConfig | null;
  status: HotkeyStatus | null;
  onUpdate: Props["onUpdate"];
  onSaved: () => void;
}) {
  const current = status?.hotkey ?? config?.hotkey ?? "CmdOrCtrl+Shift+P";
  const [recording, setRecording] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [pausing, setPausing] = useState(false);

  const save = async (accel: string) => {
    if (!accel || accel === current) return;
    setBusy(true);
    setMsg(null);
    const errs: string[] = [];
    if (tauri) {
      try {
        await native.setHotkey(accel);
      } catch (e) {
        errs.push(`App: ${nativeErrorMessage(e)}`);
      }
    }
    if (online) {
      try {
        await onUpdate({ hotkey: accel });
      } catch (e) {
        errs.push(`Engine: ${errorMessage(e)}`);
      }
    }
    setBusy(false);
    if (!tauri && !online) setMsg({ ok: false, text: "Couldn't save: the app and engine are both unavailable." });
    else if (errs.length) setMsg({ ok: false, text: errs.join(" · ") });
    else setMsg({ ok: true, text: `Saved. Press ${prettyHotkey(accel)} in any app.` });
    onSaved();
  };

  const togglePause = async () => {
    if (!status) return;
    setPausing(true);
    setMsg(null);
    try {
      await native.setHotkeyPaused(!status.paused);
    } catch (e) {
      setMsg({ ok: false, text: `App: ${nativeErrorMessage(e)}` });
    } finally {
      setPausing(false);
      onSaved();
    }
  };

  const mode = config?.inject_mode ?? "paste";
  const modes: { id: InjectMode; label: string; desc: string }[] = [
    { id: "paste", label: "Paste", desc: "Pastes the rewrite, then restores your clipboard." },
    { id: "type", label: "Type", desc: "Types it out key by key, for apps that block paste." },
  ];

  return (
    <section>
      <h2 className="group-title">Shortcut</h2>
      <div className="inset">
        <Row title="Rewrite selection" subtitle={recording ? "Press the new shortcut, or Esc to cancel" : undefined}>
          <button
            type="button"
            className={`btn min-w-[64px] ${recording ? "ring-2 ring-accent" : ""}`}
            aria-label={`Shortcut ${prettyHotkey(current)}. Click to record a new one.`}
            title="Click to record a new shortcut"
            disabled={busy}
            onClick={(e) => {
              e.currentTarget.focus(); // WebKit doesn't focus buttons on click
              setRecording((r) => !r);
            }}
            onBlur={() => setRecording(false)}
            onKeyDown={(e) => {
              if (!recording) return;
              e.preventDefault();
              if (e.key === "Escape") {
                setRecording(false);
                return;
              }
              const a = accelFromEvent(e);
              if (a) {
                setRecording(false);
                void save(a);
              }
            }}
          >
            <span className="kbd">{recording ? "…" : prettyHotkey(current)}</span>
          </button>
        </Row>
        {tauri && status && (
          <Row title="Pause shortcut" subtitle={status.paused ? "Paused. Also stops typing capture." : undefined}>
            <Switch checked={status.paused} onChange={togglePause} disabled={pausing} label="Pause shortcut" />
          </Row>
        )}
        <Row title="Insert by" subtitle={modes.find((m) => m.id === mode)?.desc}>
          <div className="seg" role="radiogroup" aria-label="Insert by">
            {modes.map((m) => (
              <button
                key={m.id}
                type="button"
                role="radio"
                aria-checked={mode === m.id}
                disabled={!online || !config}
                onClick={() => mode !== m.id && onUpdate({ inject_mode: m.id }).catch(() => {})}
              >
                {m.label}
              </button>
            ))}
          </div>
        </Row>
      </div>
      {msg && <p className={`footnote fade-in ${msg.ok ? "" : "!text-bad"}`}>{msg.text}</p>}
    </section>
  );
}

/* ---------------- Learn from what I type ---------------- */

const KNOWN_APPS: Record<string, string> = {
  "com.tinyspeck.slackmacgap": "Slack",
  "com.apple.mail": "Mail",
  "com.apple.Notes": "Notes",
  "com.apple.MobileSMS": "Messages",
  "com.apple.TextEdit": "TextEdit",
  "com.google.Chrome": "Chrome",
  "com.apple.Safari": "Safari",
  "notion.id": "Notion",
  "com.linear": "Linear",
  "com.microsoft.Outlook": "Outlook",
};

function CaptureGroup({
  tauri,
  online,
  config,
  onUpdate,
}: {
  tauri: boolean;
  online: boolean;
  config: AppConfig | null;
  onUpdate: Props["onUpdate"];
}) {
  const supported = config != null && typeof config.capture_enabled === "boolean";
  const enabled = config?.capture_enabled ?? false;
  const apps = config?.capture_apps ?? [];
  const [cap, setCap] = useState<CaptureStatus | null>(null);
  const [capErr, setCapErr] = useState<string | null>(null);
  const [running, setRunning] = useState<RunningApp[]>([]);
  const [manualId, setManualId] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    native.getCaptureStatus().then((s) => s && setCap(s));
  }, []);

  useEffect(() => {
    if (!tauri) return;
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [tauri, refresh]);

  useEffect(() => {
    if (enabled && tauri) native.listRunningApps().then(setRunning);
  }, [enabled, tauri]);

  const apply = async (nextEnabled: boolean, nextApps: string[]) => {
    setBusy(true);
    setCapErr(null);
    try {
      await onUpdate({ capture_enabled: nextEnabled, capture_apps: nextApps });
    } catch (e) {
      setCapErr(errorMessage(e));
      setBusy(false);
      return;
    }
    const r = await native.setCapture(nextEnabled, nextApps);
    if (r.status) setCap(r.status);
    if (r.error) setCapErr(r.error);
    setBusy(false);
  };

  const nameOf = (id: string) => running.find((a) => a.bundle_id === id)?.name ?? KNOWN_APPS[id] ?? id;
  const addable = running.filter((a) => !apps.includes(a.bundle_id)).sort((a, b) => a.name.localeCompare(b.name));
  const add = (id: string) => {
    const v = id.trim();
    if (v && !apps.includes(v)) void apply(enabled, [...apps, v]);
  };

  const err = capErr ?? cap?.last_error ?? null;
  const statusLine = !enabled
    ? "Off"
    : tauri && cap
      ? cap.running
        ? `${cap.captured_this_session.toLocaleString()} captured this session${cap.last_captured_at ? ` · last ${relativeTime(cap.last_captured_at)}` : ""}`
        : "On, not running"
      : "On";

  return (
    <section>
      <h2 className="group-title">Learning</h2>
      <div className="inset">
        <Row title="Learn from what I type" subtitle={err ? <span className="text-bad">{err}</span> : statusLine}>
          {enabled && tauri && cap && <StatusDot state={err ? "down" : cap.running ? "ok" : "warn"} />}
          <Switch
            checked={enabled}
            onChange={(v) => void apply(v, apps)}
            disabled={!online || !supported || busy}
            label="Learn from what I type"
          />
        </Row>

        {enabled &&
          apps.map((id) => (
            <div className="row fade-in !min-h-[34px] !py-1" key={id}>
              <div className="min-w-0 flex-1 truncate text-[13px]" title={id}>
                {nameOf(id)}
              </div>
              <button
                type="button"
                className="icon-btn !h-[22px] !w-[22px]"
                aria-label={`Stop learning from ${nameOf(id)}`}
                title="Remove"
                disabled={busy}
                onClick={() => void apply(enabled, apps.filter((a) => a !== id))}
              >
                <MinusIcon width={13} height={13} />
              </button>
            </div>
          ))}

        {enabled && (
          <div className="row !min-h-[38px]">
            {addable.length > 0 ? (
              <select
                className="select"
                value=""
                disabled={busy}
                aria-label="Add an app"
                onChange={(e) => add(e.target.value)}
              >
                <option value="">{apps.length ? "Add app…" : "Choose apps…"}</option>
                {addable.map((a) => (
                  <option key={a.bundle_id} value={a.bundle_id}>
                    {a.name}
                  </option>
                ))}
              </select>
            ) : (
              <form
                className="flex flex-1 items-center gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  add(manualId);
                  setManualId("");
                }}
              >
                <input
                  className="field !py-1 text-[12px]"
                  placeholder="App bundle id, e.g. com.tinyspeck.slackmacgap"
                  aria-label="App bundle id"
                  value={manualId}
                  onChange={(e) => setManualId(e.target.value)}
                  spellCheck={false}
                />
                <button type="submit" className="btn" disabled={!manualId.trim() || busy}>
                  Add
                </button>
              </form>
            )}
            {apps.length === 0 && addable.length > 0 && (
              <span className="text-[11.5px] text-text-3">Nothing is read until you add an app.</span>
            )}
          </div>
        )}
      </div>
      <p className="footnote">
        {online && config && !supported
          ? "This engine version doesn't support typing capture yet."
          : "Stays on this Mac. Password fields are never read, and pausing the shortcut stops it."}
      </p>
    </section>
  );
}

/* ---------------- Advanced: test run, engine, permissions ---------------- */

function AdvancedGroup({
  tauri,
  online,
  health,
  perm,
  permError,
  status,
  lastRun,
  generating,
  onTriggered,
}: {
  tauri: boolean;
  online: boolean;
  health: Health | null;
  perm: PermissionStatus | null;
  permError: string | null;
  status: HotkeyStatus | null;
  lastRun: RunResult | null;
  generating: boolean;
  onTriggered: () => void;
}) {
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const trigger = async () => {
    setErr(null);
    setBusy(true);
    try {
      await native.triggerNow();
      setTimeout(onTriggered, 600);
    } catch (e) {
      setErr(nativeErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const runSubtitle = !tauri
    ? "Only inside the app"
    : lastRun
      ? `${lastRun.ok ? "Last run succeeded" : "Last run failed"} · ${lastRun.latency_ms.toLocaleString()} ms${lastRun.latency_ms > 5000 ? " (over the 5 s budget)" : ""}`
      : `No runs yet${status ? `. Press ${prettyHotkey(status.hotkey)} with text selected.` : ""}`;

  return (
    <div className="space-y-2">
      <div className="inset">
        <Row title="Test the shortcut" subtitle={runSubtitle}>
          <button type="button" className="btn" disabled={!tauri || busy || generating} onClick={trigger}>
            {generating ? "Running…" : "Run now"}
          </button>
        </Row>
        {(lastRun?.error || err) && (
          <div className="row">
            <p className="selectable text-[12px] text-bad">{err ?? lastRun?.error}</p>
          </div>
        )}
        <Row
          title="Accessibility"
          subtitle={!tauri ? "Only inside the app" : permError ? permError : perm ? (perm.accessibility ? "Granted" : perm.launched_from_other_app ? `Not granted for ${perm.grant_to}` : "Not granted") : "Checking…"}
        >
          {perm && <StatusDot state={perm.accessibility ? "ok" : "down"} />}
        </Row>
        <Row
          title="Model"
          subtitle={
            !online || !health
              ? "Engine offline"
              : health.llm === "mock"
                ? "Mock model (no API key set)"
                : health.llm_ready === false
                  ? "Claude, but ANTHROPIC_API_KEY is missing. Add it to .env and restart the engine."
                  : "Claude"
          }
        >
          <StatusDot state={!online || !health ? "down" : health.llm === "mock" || health.llm_ready === false ? "warn" : "ok"} />
        </Row>
        <Row
          title="Style graph"
          subtitle={
            !online || !health
              ? "Engine offline"
              : `${health.neo4j ? "Neo4j" : health.graph_store === "memory" ? "In memory, not saved" : "Unavailable"} · embeddings ${health.embeddings}`
          }
        >
          <StatusDot state={!online || !health ? "down" : health.neo4j ? "ok" : "warn"} />
        </Row>
      </div>
      <p className="footnote">
        Test runs hide Polytone and act on the app you were just in, so put your cursor in a text field there first.
        {perm?.platform ? ` Platform: ${perm.platform}.` : ""}
      </p>
    </div>
  );
}
