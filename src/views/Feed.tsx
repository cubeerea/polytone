import { useEffect, useState } from "react";
import { api, isOffline, type IngestResult, type IngestSource, type Persona } from "../api";
import { relativeTime, useResource } from "../hooks";
import { ErrorLine, OfflineNotice, SectionHead, Spinner, StatusDot } from "../components/bits";

const SOURCE_BLURB: Record<string, string> = {
  manual: "Text you paste above",
  gmail: "Your sent mail only (in:sent from:me)",
  wispr: "Wispr Flow dictation history",
  watcher: "Watched folder: .txt, .mbox, SQLite dumps",
};

export function Feed({
  personas,
  activePersona,
  online,
}: {
  personas: Persona[];
  activePersona: string | null;
  online: boolean;
}) {
  const [text, setText] = useState("");
  const [persona, setPersona] = useState<string>(activePersona ?? "");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<IngestResult | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (activePersona && !persona) setPersona(activePersona);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePersona]);

  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  const sources = useResource<IngestSource[]>(api.ingestSources, [online]);

  const ingest = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.ingestManual(text, persona || null);
      setResult(r);
      if (r.accepted > 0) setText("");
      sources.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <section>
        <SectionHead kicker="Manual ingestion" title="Feed it your own words" />

        <div className="sheet mt-3">
          <label htmlFor="feed-text" className="sr-only">
            Human-written sample text
          </label>
          <textarea
            id="feed-text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void ingest();
              }
            }}
            placeholder="Paste an email, a Slack message, a note — anything you typed yourself…"
            spellCheck={false}
            className="block h-[188px] w-full resize-none bg-transparent py-[9px] pl-[38px] pr-3 font-mono text-[12.5px] leading-[22px] text-ink placeholder:italic placeholder:text-ink-faint focus:outline-none"
          />
          <div className="flex items-center justify-between rounded-b-[3px] border-t border-[var(--color-rule-strong)] bg-paper-hi px-3 py-1">
            <span className="label tabular-nums">
              {words} {words === 1 ? "word" : "words"}
            </span>
            <span className="label">⌘↵ to ingest</span>
          </div>
        </div>

        <p className="mt-2 flex gap-2 text-[11px] leading-snug text-ink-soft">
          <span className="text-ribbon" aria-hidden>
            ¶
          </span>
          <span>
            Only paste text <em className="font-display text-[13px] text-ink">you wrote yourself</em>. AI-written
            text — including Polytone's own drafts — is firewalled and will be rejected.
          </span>
        </p>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <label className="flex items-center gap-2">
            <span className="label">Tag as</span>
            <select
              value={persona}
              onChange={(e) => setPersona(e.target.value)}
              className="field w-auto py-1 pr-6 text-[11px]"
            >
              <option value="">Untagged</option>
              {personas.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="keycap keycap-ink min-w-[104px]"
            disabled={busy || !text.trim() || !online}
            onClick={ingest}
            title={!online ? "Backend offline" : undefined}
          >
            {busy ? <Spinner label="ingesting" /> : "Ingest ↵"}
          </button>
        </div>

        <div className="mt-3 space-y-2" aria-live="polite">
          {!online && !error && <OfflineNotice compact />}
          <ErrorLine error={error} />
          {result && <IngestReceipt result={result} />}
        </div>
      </section>

      <section>
        <SectionHead
          kicker="Automatic"
          title="Ingestion sources"
          right={
            <button
              type="button"
              className="keycap keycap-sm"
              onClick={sources.reload}
              disabled={sources.loading}
              aria-label="Refresh sources"
            >
              ↻
            </button>
          }
        />
        {sources.error && !sources.data ? (
          isOffline(sources.error) ? (
            <p className="py-2 text-ink-faint">Sources appear once the backend is running.</p>
          ) : (
            <ErrorLine error={sources.error} />
          )
        ) : sources.data && sources.data.length === 0 ? (
          <p className="py-2 text-ink-faint">No sources configured.</p>
        ) : (
          <ul className="divide-y divide-[var(--color-rule)]">
            {(sources.data ?? []).map((s) => (
              <SourceRow key={s.name} source={s} onDone={sources.reload} />
            ))}
            {!sources.data && sources.loading && (
              <li className="py-3">
                <Spinner label="loading" />
              </li>
            )}
          </ul>
        )}
      </section>
    </div>
  );
}

function IngestReceipt({ result }: { result: IngestResult }) {
  const ok = result.accepted > 0;
  return (
    <div className="card strike-in px-3 py-2.5">
      <div className="flex items-baseline justify-between">
        <span className="label">Receipt</span>
        <span className={`label ${ok ? "text-ok" : "text-ribbon"}`}>{ok ? "accepted" : "rejected"}</span>
      </div>
      <dl className="mt-1.5 grid grid-cols-4 gap-2 text-center">
        <Stat k="accepted" v={result.accepted} />
        <Stat k="rejected" v={result.rejected} accent={result.rejected > 0} />
        <Stat k="sentences" v={result.sentences} />
        <Stat k="words" v={result.words_merged} />
      </dl>
      {result.reasons.length > 0 && (
        <ul className="mt-2 space-y-0.5 border-t hairline pt-1.5">
          {result.reasons.map((r, i) => (
            <li key={i} className="selectable flex gap-1.5 text-[11px] text-ink-soft">
              <span className="text-ribbon">×</span>
              {r}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Stat({ k, v, accent = false }: { k: string; v: number; accent?: boolean }) {
  return (
    <div>
      <dd className={`display text-[20px] leading-none tabular-nums ${accent ? "text-ribbon" : "text-ink"}`}>{v}</dd>
      <dt className="label mt-1 text-[8.5px] tracking-[0.1em]">{k}</dt>
    </div>
  );
}

function SourceRow({ source, onDone }: { source: IngestSource; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const status = source.status.toLowerCase();
  // "ready (… created on first run)" must not read as busy: only a leading verb counts.
  const dot = !source.enabled
    ? "unknown"
    : /error|fail/.test(status)
      ? "down"
      : /^(running|busy|syncing)\b/.test(status)
        ? "warn"
        : "ok";

  const run = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.runSource(source.name);
      setMsg({
        ok: true,
        text: `+${r.accepted} accepted · ${r.rejected} rejected${r.reasons[0] ? ` — ${r.reasons[0]}` : ""}`,
      });
      onDone();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="py-2.5">
      <div className="flex items-center gap-3">
        <StatusDot state={dot} />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="font-medium capitalize text-ink">{source.name}</span>
            <span className="label truncate normal-case tracking-normal" title={source.status}>
              {source.enabled ? source.status : "disabled"}
            </span>
          </div>
          <div className="truncate text-[11px] text-ink-faint">
            {SOURCE_BLURB[source.name] ?? "Source"} · {source.items_ingested} items · {relativeTime(source.last_run)}
          </div>
          {!source.enabled && source.status && source.status !== "disabled" && (
            // e.g. Gmail: "needs credentials: … then run `uv run python -m … auth`" — the fix, so show it whole
            <p className="selectable mt-0.5 break-words text-[10.5px] leading-snug text-ink-soft">{source.status}</p>
          )}
        </div>
        <button
          type="button"
          className="keycap keycap-sm"
          disabled={busy || !source.enabled || source.name === "manual"}
          onClick={run}
        >
          {busy ? "…" : "Run"}
        </button>
      </div>
      {msg && (
        <p className={`selectable strike-in mt-1 pl-5 text-[11px] ${msg.ok ? "text-ink-soft" : "text-ribbon"}`}>
          {msg.text}
        </p>
      )}
    </li>
  );
}
