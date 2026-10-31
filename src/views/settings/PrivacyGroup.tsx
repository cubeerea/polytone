import { useState } from "react";
import { api, errorMessage, WIPE_PHRASE, type AppConfig, type AuditEntry, type AuditVerify } from "../../api";
import { relativeTime, useAction } from "../../hooks";
import { invalidateQueries } from "../../query";
import { KEYS, updateConfig, useAudit } from "../../queries";
import { Disclosure, ErrorLine, Loading } from "../../components/bits";
import { Row } from "./Row";

const LOCK_OPTIONS = [1, 5, 15, 30, 60, 120, 240];
const RETENTION_OPTIONS = [0, 7, 30, 90, 365];

const minutesLabel = (m: number) => (m < 60 ? `${m} min` : `${m / 60} hour${m === 60 ? "" : "s"}`);
const daysLabel = (d: number) => (d === 0 ? "Never" : d === 365 ? "1 year" : `${d} days`);
/** The preset list, plus the current value if it was set to something else. */
const withCurrent = (opts: number[], v: number) => (opts.includes(v) ? opts : [...opts, v].sort((a, b) => a - b));

/** Auto-lock, retention, the activity log and "delete everything". */
export function PrivacyGroup({ online, config }: { online: boolean; config: AppConfig | null }) {
  const supported = config != null && typeof config.lock_idle_minutes === "number";
  const lockMin = config?.lock_idle_minutes ?? 15;
  const retention = config?.retention_days ?? 0;
  const save = useAction((patch: Partial<AppConfig>) => updateConfig(patch));
  const disabled = !online || !supported || save.busy;

  return (
    <section>
      <h2 className="group-title">Privacy &amp; security</h2>
      <div className="inset">
        <Row title="Auto-lock after" subtitle="With no activity in this window. Screen lock and sleep lock at once.">
          <select
            className="select"
            aria-label="Auto-lock after"
            value={lockMin}
            disabled={disabled}
            onChange={(e) => void save.run({ lock_idle_minutes: Number(e.target.value) })}
          >
            {withCurrent(LOCK_OPTIONS, lockMin).map((m) => (
              <option key={m} value={m}>
                {minutesLabel(m)}
              </option>
            ))}
          </select>
        </Row>
        <Row title="Delete captured text after" subtitle="Typed and dictated samples and drafts. Your learned style stays.">
          <select
            className="select"
            aria-label="Delete captured text after"
            value={retention}
            disabled={disabled}
            onChange={(e) => void save.run({ retention_days: Number(e.target.value) })}
          >
            {withCurrent(RETENTION_OPTIONS, retention).map((d) => (
              <option key={d} value={d}>
                {daysLabel(d)}
              </option>
            ))}
          </select>
        </Row>
        <WipeRow disabled={!online || !supported} />
      </div>
      {save.error != null && (
        <p className="footnote !text-bad" role="alert">
          {errorMessage(save.error)}
        </p>
      )}
      <p className="footnote">
        {online && config && !supported
          ? "This engine version doesn't support these settings yet."
          : "Everything Polytone learns is encrypted on this Mac."}
      </p>

      {supported && (
        <div className="mt-5">
          <ActivityLog />
        </div>
      )}
    </section>
  );
}

/** "Delete all learned data" with an inline typed confirmation. */
function WipeRow({ disabled }: { disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [done, setDone] = useState(false);
  const wipe = useAction(async () => {
    await api.wipe();
    invalidateQueries((k) => k !== KEYS.health && !k.startsWith("native:"));
    setOpen(false);
    setPhrase("");
    setDone(true);
  });
  const close = () => {
    setOpen(false);
    setPhrase("");
    wipe.setError(null);
  };

  return (
    <>
      <Row
        title="Delete all learned data"
        subtitle={done ? "Deleted. Polytone starts learning from scratch." : "Samples, drafts and your style graph."}
      >
        {!open && (
          <button
            type="button"
            className="btn !text-bad"
            disabled={disabled}
            onClick={() => {
              setDone(false);
              setOpen(true);
            }}
          >
            Delete…
          </button>
        )}
      </Row>
      {open && (
        <div className="row fade-in flex-col !items-stretch !gap-2 !py-2.5">
          <p className="text-[12px] text-text-2">
            This can't be undone. Type <span className="selectable font-medium text-text">{WIPE_PHRASE}</span> to confirm.
          </p>
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (phrase.trim() === WIPE_PHRASE) void wipe.run();
            }}
          >
            <input
              className="field !py-1 text-[12px]"
              aria-label={`Type "${WIPE_PHRASE}" to confirm`}
              placeholder={WIPE_PHRASE}
              value={phrase}
              autoFocus
              spellCheck={false}
              autoComplete="off"
              onChange={(e) => setPhrase(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && close()}
            />
            <button type="button" className="btn" onClick={close} disabled={wipe.busy}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn !bg-bad !text-on-accent"
              disabled={phrase.trim() !== WIPE_PHRASE || wipe.busy}
            >
              {wipe.busy ? "Deleting…" : "Delete"}
            </button>
          </form>
          {wipe.error != null && <ErrorLine error={wipe.error} />}
        </div>
      )}
    </>
  );
}

/** Recent audit entries (fetched only while open) and a tamper check. */
function ActivityLog() {
  const [result, setResult] = useState<AuditVerify | null>(null);
  const verify = useAction(async () => setResult(await api.auditVerify()));

  return (
    <Disclosure
      title="Activity log"
      right={
        <span className="flex items-center gap-2">
          {result && <VerifyBadge r={result} />}
          {verify.error != null && <span className="text-[11.5px] text-bad">{errorMessage(verify.error)}</span>}
          <button type="button" className="btn" disabled={verify.busy} onClick={() => void verify.run()}>
            {verify.busy ? "Verifying…" : "Verify"}
          </button>
        </span>
      }
    >
      <AuditList />
    </Disclosure>
  );
}

function VerifyBadge({ r }: { r: AuditVerify }) {
  return r.ok ? (
    <span className="text-[11.5px] text-ok" title={`${r.count.toLocaleString()} entries checked`}>
      ✓ Intact · {r.count.toLocaleString()}
    </span>
  ) : (
    <span className="text-[11.5px] text-bad" title={r.reason ?? undefined}>
      ✗ Tampered{r.first_bad_seq != null ? ` at #${r.first_bad_seq}` : ""}
    </span>
  );
}

function AuditList() {
  const audit = useAudit();
  if (audit.loading) return <Loading />;
  if (audit.error != null) return <ErrorLine error={audit.error} />;
  const entries = audit.data?.entries ?? [];
  if (entries.length === 0) return <p className="footnote">Nothing recorded yet.</p>;
  return (
    <div className="inset max-h-[260px] overflow-y-auto">
      {entries.map((e) => (
        <div className="row !min-h-[32px] !py-1" key={e.seq}>
          <span className="tnum w-[36px] flex-none text-[11px] text-text-3">#{e.seq}</span>
          <span className="min-w-0 flex-1 truncate text-[12px]" title={detailText(e)}>
            {e.event}
            {detailText(e) && <span className="text-text-3"> · {detailText(e)}</span>}
          </span>
          <span className="flex-none text-[11px] text-text-3" title={e.ts}>
            {relativeTime(e.ts)}
          </span>
        </div>
      ))}
    </div>
  );
}

/** "persona Boss, chars_in 42" — the first few scalar fields of the detail. */
function detailText(e: AuditEntry): string {
  let d: unknown = e.detail;
  if (typeof e.detail === "string") {
    try {
      d = JSON.parse(e.detail);
    } catch {
      return e.detail.slice(0, 60);
    }
  }
  if (typeof d !== "object" || d === null) return "";
  return Object.entries(d as Record<string, unknown>)
    .filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v) || Array.isArray(v))
    .slice(0, 3)
    .map(([k, v]) => `${k.replace(/_/g, " ")} ${Array.isArray(v) ? (v.length <= 3 ? v.join(", ") : v.length) : String(v)}`)
    .join(", ");
}
