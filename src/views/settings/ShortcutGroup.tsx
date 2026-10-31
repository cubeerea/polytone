import { useState, type KeyboardEvent } from "react";
import { errorMessage, FALLBACK_CONFIG, type AppConfig, type DraftSpeed, type InjectMode } from "../../api";
import { invalidateQueries, setQueryState } from "../../query";
import { KEYS, updateConfig, useHotkeyStatus } from "../../queries";
import { native, type HotkeyStatus } from "../../tauri";
import { Switch, prettyHotkey } from "../../components/bits";
import { Row } from "./Row";

const MOD_ORDER = ["CmdOrCtrl", "Ctrl", "Alt", "Shift"] as const;
const MODIFIER_KEYS = new Set(["Meta", "Control", "Alt", "Shift", "CapsLock", "Tab"]);

/** Turn a keydown into a Tauri accelerator ("CmdOrCtrl+Shift+P"), or null for a bare modifier. */
function accelFromEvent(e: KeyboardEvent): string | null {
  const mods: string[] = [];
  if (e.metaKey) mods.push("CmdOrCtrl");
  if (e.ctrlKey) mods.push(e.metaKey ? "Ctrl" : "CmdOrCtrl");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  const k = e.key;
  if (MODIFIER_KEYS.has(k) || mods.length === 0) return null;
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

const MODES: { id: InjectMode; label: string; desc: string }[] = [
  { id: "paste", label: "Paste", desc: "Pastes the rewrite, then restores your clipboard." },
  { id: "type", label: "Type", desc: "Types it out key by key, for apps that block paste." },
];

const SPEEDS: { id: DraftSpeed; label: string; desc: string }[] = [
  { id: "quality", label: "Best", desc: "Your configured model (Claude Sonnet by default). Sticks closest to what you wrote." },
  { id: "fast", label: "Fast", desc: "Claude Haiku. About 40% quicker, but may add a few words of its own." },
];

const setHotkeyStatus = (s: HotkeyStatus) => setQueryState(KEYS.hotkey, { data: s, error: null });

export function ShortcutGroup({ tauri, online, config }: { tauri: boolean; online: boolean; config: AppConfig | null }) {
  const status = useHotkeyStatus().data;
  const current = status?.hotkey ?? config?.hotkey ?? FALLBACK_CONFIG.hotkey;
  const [recording, setRecording] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [pausing, setPausing] = useState(false);

  // The app and the engine each keep the hotkey; save to whichever is reachable and report both.
  const save = async (accel: string) => {
    if (!accel || accel === current) return;
    setBusy(true);
    setMsg(null);
    const errs: string[] = [];
    if (tauri) {
      try {
        setHotkeyStatus(await native.setHotkey(accel));
      } catch (e) {
        errs.push(`App: ${errorMessage(e)}`);
      }
    }
    if (online) {
      try {
        await updateConfig({ hotkey: accel });
      } catch (e) {
        errs.push(`Engine: ${errorMessage(e)}`);
      }
    }
    setBusy(false);
    if (!tauri && !online) setMsg({ ok: false, text: "Couldn't save: the app and engine are both unavailable." });
    else if (errs.length) setMsg({ ok: false, text: errs.join(" · ") });
    else setMsg({ ok: true, text: `Saved. Press ${prettyHotkey(accel)} in any app.` });
    invalidateQueries(KEYS.hotkey);
  };

  const togglePause = async () => {
    if (!status) return;
    setPausing(true);
    setMsg(null);
    try {
      setHotkeyStatus(await native.setHotkeyPaused(!status.paused));
    } catch (e) {
      setMsg({ ok: false, text: `App: ${errorMessage(e)}` });
      invalidateQueries(KEYS.hotkey);
    } finally {
      setPausing(false);
    }
  };

  const mode = config?.inject_mode ?? FALLBACK_CONFIG.inject_mode;
  const speed = config?.draft_speed;

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
        <Row title="Insert by" subtitle={MODES.find((m) => m.id === mode)?.desc}>
          <div className="seg" role="radiogroup" aria-label="Insert by">
            {MODES.map((m) => (
              <button
                key={m.id}
                type="button"
                role="radio"
                aria-checked={mode === m.id}
                disabled={!online || !config}
                onClick={() => mode !== m.id && updateConfig({ inject_mode: m.id }).catch(() => {})}
              >
                {m.label}
              </button>
            ))}
          </div>
        </Row>
        {speed && (
          <Row title="Draft quality" subtitle={SPEEDS.find((s) => s.id === speed)?.desc}>
            <div className="seg" role="radiogroup" aria-label="Draft quality">
              {SPEEDS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  role="radio"
                  aria-checked={speed === s.id}
                  disabled={!online || !config}
                  onClick={() => speed !== s.id && updateConfig({ draft_speed: s.id }).catch(() => {})}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </Row>
        )}
      </div>
      {msg && <p className={`footnote fade-in ${msg.ok ? "" : "!text-bad"}`}>{msg.text}</p>}
    </section>
  );
}
