import { errorMessage, type Health } from "../../api";
import { useAction } from "../../hooks";
import { invalidateQueries } from "../../query";
import { KEYS, useHotkeyStatus } from "../../queries";
import { native, type PermissionStatus, type RunResult } from "../../tauri";
import { StatusDot, prettyHotkey } from "../../components/bits";
import { Row } from "./Row";

/** Test run, engine status, permissions. */
export function AdvancedGroup({
  tauri,
  online,
  health,
  perm,
  permError,
  lastRunFromEvent,
  generating,
}: {
  tauri: boolean;
  online: boolean;
  health: Health | null;
  perm: PermissionStatus | null;
  permError: unknown;
  lastRunFromEvent: RunResult | null;
  generating: boolean;
}) {
  const status = useHotkeyStatus().data;
  const lastRun = status?.last_run ?? lastRunFromEvent;
  const trigger = useAction(async () => {
    await native.triggerNow();
    setTimeout(() => invalidateQueries(KEYS.hotkey), 600);
  });
  const err = trigger.error != null ? errorMessage(trigger.error) : null;

  const runSubtitle = !tauri
    ? "Only inside the app"
    : lastRun
      ? `${lastRun.ok ? "Last run succeeded" : "Last run failed"} · ${lastRun.latency_ms.toLocaleString()} ms${lastRun.latency_ms > 5000 ? " (over the 5 s budget)" : ""}`
      : `No runs yet${status ? `. Press ${prettyHotkey(status.hotkey)} with text selected.` : ""}`;

  const permSubtitle = !tauri
    ? "Only inside the app"
    : permError != null
      ? errorMessage(permError)
      : perm
        ? perm.accessibility
          ? "Granted"
          : perm.launched_from_other_app
            ? `Not granted for ${perm.grant_to}`
            : "Not granted"
        : "Checking…";

  return (
    <div className="space-y-2">
      <div className="inset">
        <Row title="Test the shortcut" subtitle={runSubtitle}>
          <button type="button" className="btn" disabled={!tauri || trigger.busy || generating} onClick={() => void trigger.run()}>
            {generating ? "Running…" : "Run now"}
          </button>
        </Row>
        {(lastRun?.error || err) && (
          <div className="row">
            <p className="selectable text-[12px] text-bad">{err ?? lastRun?.error}</p>
          </div>
        )}
        <Row title="Accessibility" subtitle={permSubtitle}>
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
