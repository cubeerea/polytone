import { useCallback, useEffect, useState, type KeyboardEvent } from "react";
import { errorMessage, type AppConfig, type Health, type InjectMode } from "../api";
import { ONBOARDING_KEY, useLocalStorage } from "../hooks";
import {
  inTauri,
  native,
  nativeErrorMessage,
  type HotkeyStatus,
  type PermissionKind,
  type PermissionStatus,
  type RunResult,
} from "../tauri";
import { ErrorLine, OfflineNotice, SectionHead, Spinner, StatusDot } from "../components/bits";

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
    <div className="space-y-6">
      <PermissionsCard tauri={tauri} perm={perm} error={permError} onRecheck={refreshPerm} />
      <HotkeySection
        tauri={tauri}
        online={online}
        config={config}
        status={status}
        onUpdate={onUpdate}
        onSaved={refreshStatus}
      />
      <InjectSection online={online} config={config} onUpdate={onUpdate} />
      {configError != null && <ErrorLine error={configError} />}
      <RunSection
        tauri={tauri}
        status={status}
        lastRun={status?.last_run ?? lastRunFromEvent}
        generating={generating}
        onTriggered={refreshStatus}
      />
      <BackendSection online={online} health={health} />
    </div>
  );
}

/* ---------------- Permissions / first-launch onboarding ---------------- */

function PermissionsCard({
  tauri,
  perm,
  error,
  onRecheck,
}: {
  tauri: boolean;
  perm: PermissionStatus | null;
  error: string | null;
  onRecheck: () => void;
}) {
  const [dismissed, setDismissed] = useLocalStorage(ONBOARDING_KEY, null);
  const missing = perm != null && !perm.accessibility;
  const [expanded, setExpanded] = useState(false);
  const showGuide = missing && (!dismissed || expanded);
  const [openErr, setOpenErr] = useState<string | null>(null);

  const open = async (kind: PermissionKind) => {
    setOpenErr(null);
    try {
      await native.openPermissionSettings(kind);
    } catch (e) {
      setOpenErr(nativeErrorMessage(e));
    }
  };

  return (
    <section>
      <SectionHead
        kicker="Permissions"
        title="Let Polytone reach the keys"
        right={
          tauri && (
            <button type="button" className="keycap keycap-sm" onClick={onRecheck}>
              Re-check
            </button>
          )
        }
      />

      {!tauri ? (
        <p className="text-ink-soft">
          Running in a plain browser — permission checks, the hotkey and injection only work inside the Polytone app (
          <code className="text-ink">pnpm tauri dev</code>).
        </p>
      ) : error ? (
        <p className="text-ribbon">Couldn't check permissions: {error}</p>
      ) : perm == null ? (
        <Spinner label="checking" />
      ) : perm.accessibility ? (
        <div className="flex items-center gap-2">
          <StatusDot state="ok" />
          <span className="text-ink">Accessibility granted.</span>
          <span className="text-ink-faint">Polytone can copy & paste in other apps.</span>
        </div>
      ) : !showGuide ? (
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <StatusDot state="down" />
            <span className="text-ink">Accessibility not granted.</span>
          </div>
          <button type="button" className="keycap keycap-sm" onClick={() => setExpanded(true)}>
            Show guide
          </button>
        </div>
      ) : (
        <div className="card strike-in border-l-2 py-3 pl-[42px] pr-4" style={{ borderLeftColor: "var(--color-ribbon)" }}>
          <div className="label text-ribbon">First launch · 3 steps</div>
          <p className="mt-1 font-display text-[15px] leading-snug text-ink">
            Polytone reads your selection and types the rewrite back, so macOS needs to trust it.
          </p>
          <ol className="mt-3 space-y-3 text-[12px] leading-[18px] text-ink-soft">
            <Step n={1}>
              <div>
                Open <b className="font-medium text-ink">Privacy & Security → Accessibility</b> and switch{" "}
                <b className="font-medium text-ink">Polytone</b> on.
              </div>
              <button type="button" className="keycap keycap-ink keycap-sm mt-1.5" onClick={() => open("accessibility")}>
                Open System Settings
              </button>
            </Step>
            <Step n={2}>
              <div>
                If the hotkey doesn't fire, also enable it under{" "}
                <b className="font-medium text-ink">Input Monitoring</b>.
              </div>
              <button type="button" className="keycap keycap-sm mt-1.5" onClick={() => open("input_monitoring")}>
                Open Input Monitoring
              </button>
            </Step>
            <Step n={3}>
              <div>Come back here — this card re-checks automatically. You may need to relaunch Polytone.</div>
            </Step>
          </ol>
          {openErr && <p className="mt-2 text-[11px] text-ribbon">{openErr}</p>}
          <div className="mt-3 flex justify-end gap-2 border-t hairline pt-2">
            <button
              type="button"
              className="keycap keycap-sm"
              onClick={() => {
                setDismissed("1");
                setExpanded(false);
              }}
            >
              Later
            </button>
            <button type="button" className="keycap keycap-sm" onClick={onRecheck}>
              I've done it
            </button>
          </div>
        </div>
      )}
      {perm && <p className="label mt-1.5 normal-case tracking-normal">platform: {perm.platform}</p>}
    </section>
  );
}

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <li className="relative">
      <span
        className="absolute -left-[26px] top-0 flex h-[18px] w-[18px] items-center justify-center rounded-full border border-[var(--color-rule-strong)] bg-paper text-[10px] font-semibold text-ink"
        aria-hidden
      >
        {n}
      </span>
      {children}
    </li>
  );
}

/* ---------------- Hotkey ---------------- */

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

function HotkeySection({
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
  const [value, setValue] = useState(current);
  const [recording, setRecording] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!recording) setValue(current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  const save = async () => {
    const accel = value.trim();
    if (!accel) return;
    setBusy(true);
    setMsg(null);
    const errs: string[] = [];
    if (tauri) {
      try {
        await native.setHotkey(accel);
      } catch (e) {
        errs.push(`app: ${nativeErrorMessage(e)}`);
      }
    }
    if (online) {
      try {
        await onUpdate({ hotkey: accel });
      } catch (e) {
        errs.push(`backend: ${errorMessage(e)}`);
      }
    }
    setBusy(false);
    if (!tauri && !online) setMsg({ ok: false, text: "Nothing to save to — app and backend are both unavailable." });
    else if (errs.length) setMsg({ ok: false, text: errs.join(" · ") });
    else setMsg({ ok: true, text: `Saved. Press ${accel} in any app.` });
    onSaved();
  };

  const dirty = value.trim() !== current;

  const [pausing, setPausing] = useState(false);
  const togglePause = async () => {
    if (!status) return;
    setPausing(true);
    setMsg(null);
    try {
      await native.setHotkeyPaused(!status.paused);
    } catch (e) {
      setMsg({ ok: false, text: `app: ${nativeErrorMessage(e)}` });
    } finally {
      setPausing(false);
      onSaved();
    }
  };

  return (
    <section>
      <SectionHead
        kicker="Trigger"
        title="Hotkey"
        right={
          tauri && status ? (
            <div className="flex items-center gap-2">
              {status.paused && <span className="label text-warn">paused</span>}
              <button
                type="button"
                className="keycap keycap-sm"
                role="switch"
                aria-checked={status.paused}
                data-pressed={status.paused}
                disabled={pausing}
                onClick={togglePause}
                title="Same as “Pause hotkey” in the menu-bar icon"
              >
                {status.paused ? "Resume" : "Pause"}
              </button>
            </div>
          ) : null
        }
      />
      <div className="flex items-center gap-2">
        <input
          aria-label="Global hotkey accelerator"
          className={`field flex-1 ${recording ? "border-ribbon" : ""}`}
          value={recording ? "press keys…" : value}
          readOnly={recording}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (!recording) {
              if (e.key === "Enter") void save();
              return;
            }
            e.preventDefault();
            if (e.key === "Escape") {
              setRecording(false);
              return;
            }
            const a = accelFromEvent(e);
            if (a) {
              setValue(a);
              setRecording(false);
            }
          }}
          onBlur={() => setRecording(false)}
          spellCheck={false}
        />
        <button
          type="button"
          className="keycap keycap-sm"
          data-pressed={recording}
          onMouseDown={(e) => e.preventDefault()}
          onClick={(e) => {
            setRecording((r) => !r);
            (e.currentTarget.previousElementSibling as HTMLInputElement | null)?.focus();
          }}
        >
          {recording ? "Esc" : "Record"}
        </button>
        <button type="button" className="keycap keycap-ink keycap-sm" disabled={busy || !dirty} onClick={save}>
          {busy ? "…" : "Set"}
        </button>
      </div>
      <p className="mt-1.5 text-[11px] text-ink-faint">
        Tauri accelerator syntax, e.g. <code className="text-ink-soft">CmdOrCtrl+Shift+P</code>.
      </p>
      {msg && <p className={`strike-in mt-1 text-[11px] ${msg.ok ? "text-ok" : "text-ribbon"}`}>{msg.text}</p>}
    </section>
  );
}

/* ---------------- Inject mode ---------------- */

function InjectSection({
  online,
  config,
  onUpdate,
}: {
  online: boolean;
  config: AppConfig | null;
  onUpdate: Props["onUpdate"];
}) {
  const mode = config?.inject_mode ?? "paste";
  const opts: { id: InjectMode; title: string; desc: string }[] = [
    { id: "paste", title: "Paste", desc: "Clipboard + ⌘V. Fast; clipboard restored after." },
    { id: "type", title: "Type", desc: "Types each character. For apps that block paste." },
  ];
  return (
    <section>
      <SectionHead kicker="Delivery" title="Inject mode" />
      <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Inject mode">
        {opts.map((o) => {
          const on = mode === o.id;
          return (
            <button
              key={o.id}
              type="button"
              role="radio"
              aria-checked={on}
              disabled={!online || !config}
              onClick={() => !on && onUpdate({ inject_mode: o.id }).catch(() => {})}
              className={`keycap h-auto flex-col items-start gap-0.5 whitespace-normal px-3 py-2 text-left normal-case tracking-normal ${on ? "keycap-ink" : ""}`}
              data-pressed={on}
            >
              <span className="flex w-full items-center justify-between text-[11px] font-semibold uppercase tracking-[0.08em]">
                {o.title}
                <span className={on ? "text-ribbon" : "text-ink-faint"} aria-hidden>
                  {on ? "●" : "○"}
                </span>
              </span>
              <span className={`text-[10.5px] font-normal leading-snug ${on ? "opacity-75" : "text-ink-soft"}`}>
                {o.desc}
              </span>
            </button>
          );
        })}
      </div>
      {!online && <p className="mt-1.5 text-[11px] text-ink-faint">Saved to the backend — start it to change this.</p>}
    </section>
  );
}

/* ---------------- Last run / test trigger ---------------- */

function RunSection({
  tauri,
  status,
  lastRun,
  generating,
  onTriggered,
}: {
  tauri: boolean;
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

  return (
    <section>
      <SectionHead
        kicker="Diagnostics"
        title="Last run"
        right={
          <button
            type="button"
            className="keycap keycap-ribbon keycap-sm"
            disabled={!tauri || busy || generating}
            onClick={trigger}
            title={tauri ? "Run capture → generate → inject now" : "Only available inside the app"}
          >
            {generating ? "running…" : "Test trigger"}
          </button>
        }
      />
      {!tauri ? (
        <p className="text-ink-faint">Run history is available inside the app.</p>
      ) : !lastRun ? (
        <p className="text-ink-faint">
          No runs yet{status ? ` — press ${status.hotkey} with text selected in any app.` : "."}
        </p>
      ) : (
        <div className="flex items-start gap-2.5">
          <span className="mt-1">
            <StatusDot state={lastRun.ok ? "ok" : "down"} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-2">
              <span className="font-medium text-ink">{lastRun.ok ? "Succeeded" : "Failed"}</span>
              <span className={`tabular-nums text-[11px] ${lastRun.latency_ms > 5000 ? "text-warn" : "text-ink-soft"}`}>
                {lastRun.latency_ms} ms {lastRun.latency_ms > 5000 ? "· over 5s budget" : ""}
              </span>
            </div>
            {lastRun.error && <p className="selectable mt-0.5 text-[11px] text-ribbon">{lastRun.error}</p>}
          </div>
        </div>
      )}
      {err && <p className="mt-1.5 text-[11px] text-ribbon">{err}</p>}
      <p className="mt-2 text-[11px] text-ink-faint">
        Test trigger hides Polytone and runs on the app you were in before, so put your cursor in a text field
        there first.
      </p>
    </section>
  );
}

/* ---------------- Backend ---------------- */

function BackendSection({ online, health }: { online: boolean; health: Health | null }) {
  return (
    <section>
      <SectionHead kicker="Engine" title="Backend" />
      {!online || !health ? (
        <OfflineNotice compact />
      ) : (
        <dl className="grid grid-cols-3 gap-2">
          <KV
            k="model"
            v={health.llm === "mock" ? "mock LLM" : health.llm_ready === false ? "Claude · no key" : "Claude"}
            warn={health.llm === "mock" || health.llm_ready === false}
          />
          <KV k="embeddings" v={health.embeddings} warn={health.embeddings === "none"} />
          <KV
            k="graph"
            v={health.neo4j ? "Neo4j" : health.graph_store === "memory" ? "in-memory" : "offline"}
            warn={!health.neo4j}
          />
        </dl>
      )}
      {online && health?.llm_ready === false && (
        <p className="mt-1.5 text-[11px] text-ribbon">
          POLYTONE_LLM=anthropic but no ANTHROPIC_API_KEY — the hotkey will fail until you add one to{" "}
          <code>.env</code> and restart the backend.
        </p>
      )}
    </section>
  );
}

function KV({ k, v, warn }: { k: string; v: string; warn?: boolean }) {
  return (
    <div className="card px-2 py-1.5">
      <dt className="label text-[8.5px]">{k}</dt>
      <dd className={`mt-0.5 text-[12px] ${warn ? "text-warn" : "text-ink"}`}>{v}</dd>
    </div>
  );
}
