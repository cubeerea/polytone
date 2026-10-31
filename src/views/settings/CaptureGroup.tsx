import { useState } from "react";
import { errorMessage, type AppConfig } from "../../api";
import { relativeTime } from "../../hooks";
import { setQueryState } from "../../query";
import { KEYS, updateConfig, useCaptureStatus, useRunningApps } from "../../queries";
import { native } from "../../tauri";
import { StatusDot, Switch } from "../../components/bits";
import { MinusIcon } from "../../components/Icons";
import { Row } from "./Row";

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

/** `"com.tinyspeck.slackmacgap: fewer than 3 words"` → `"Slack: fewer than 3 words"`. */
function appLabel(skip: string, name: (id: string) => string): string {
  const i = skip.indexOf(": ");
  return i > 0 ? `${name(skip.slice(0, i))}: ${skip.slice(i + 2)}` : skip;
}

/** "Learn from what I type": the opt-in switch and its app allowlist. */
export function CaptureGroup({ tauri, online, config }: { tauri: boolean; online: boolean; config: AppConfig | null }) {
  const supported = config != null && typeof config.capture_enabled === "boolean";
  const enabled = config?.capture_enabled ?? false;
  const apps = config?.capture_apps ?? [];
  const cap = useCaptureStatus(enabled).data;
  const running = useRunningApps(enabled).data ?? [];
  const [capErr, setCapErr] = useState<string | null>(null);
  const [manualId, setManualId] = useState("");
  const [busy, setBusy] = useState(false);

  // Engine first (it's the source of truth), then tell the app so it applies at once.
  const apply = async (nextEnabled: boolean, nextApps: string[]) => {
    setBusy(true);
    setCapErr(null);
    try {
      await updateConfig({ capture_enabled: nextEnabled, capture_apps: nextApps });
    } catch (e) {
      setCapErr(errorMessage(e));
      setBusy(false);
      return;
    }
    const r = await native.setCapture(nextEnabled, nextApps);
    if (r.status) setQueryState(KEYS.capture, { data: r.status, error: null });
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
  const skip = cap?.last_skip ? `Last message skipped (${appLabel(cap.last_skip, (id) => nameOf(id))})` : null;
  const statusLine = !enabled
    ? "Off"
    : tauri && cap
      ? cap.running
        ? `${cap.captured_this_session.toLocaleString()} sent to the engine this session${cap.last_captured_at ? ` · last ${relativeTime(cap.last_captured_at)}` : ""}`
        : "On, not running"
      : "On";

  return (
    <section>
      <h2 className="group-title">Learning</h2>
      <div className="inset">
        <Row
          title="Learn from what I type"
          subtitle={
            err ? (
              <span className="text-bad">{err}</span>
            ) : enabled && skip ? (
              <>
                {statusLine}
                <br />
                <span className="text-text-3">{skip}</span>
              </>
            ) : (
              statusLine
            )
          }
        >
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
