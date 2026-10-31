import { lazy, Suspense, useEffect, useState } from "react";
import { api, errorMessage, isOffline, type IngestResult, type IngestSource, type NodeLabel, type Persona, type Stats } from "../api";
import { relativeTime, useAction } from "../hooks";
import { invalidateQueries } from "../query";
import { AFTER_INGEST, useGraph, useSources, useStats } from "../queries";
import { ErrorLine, Loading, OfflineNotice } from "../components/bits";
import { RefreshIcon } from "../components/Icons";
import { GlyphMark, LABEL_NAME } from "../components/graphGlyphs";

// d3 (force + zoom + selection) is only needed here: keep it out of the startup bundle.
const StyleGraph = lazy(() => import("../components/StyleGraph"));

const GraphLoading = () => (
  <div style={{ height: 280 }}>
    <Loading />
  </div>
);
const reloadAfterIngest = () => invalidateQueries(AFTER_INGEST);

const SOURCE_META: Record<string, { name: string; blurb: string }> = {
  gmail: { name: "Gmail", blurb: "Sent mail only" },
  wispr: { name: "Wispr Flow", blurb: "Raw dictation" },
  watcher: { name: "Inbox folder", blurb: "data/inbox: .txt, .mbox" },
  typed: { name: "What you type", blurb: "Turn on in Settings" },
};

export function Style({
  personas,
  activePersona,
  online,
}: {
  personas: Persona[];
  activePersona: string | null;
  online: boolean;
}) {
  const [persona, setPersona] = useState("");
  const graph = useGraph(persona);
  const stats = useStats();
  const sources = useSources();

  const offline = isOffline(graph.error) && !graph.data;
  if (offline) return <OfflineNotice />;

  return (
    <div className="space-y-7 pt-1">
      <section>
        <div className="flex items-start justify-between gap-3">
          <StatsLine stats={stats.data} />
          <label className="flex-none">
            <span className="sr-only">Show voice</span>
            <select className="select" value={persona} onChange={(e) => setPersona(e.target.value)}>
              <option value="">All voices</option>
              {personas.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="mt-2">
          {graph.error && !graph.data ? (
            <ErrorLine error={graph.error} />
          ) : graph.data && graph.data.nodes.length === 0 ? (
            <p className="py-16 text-center text-[13px] text-text-2">Nothing learned yet. Add some of your writing below.</p>
          ) : graph.data ? (
            <Suspense fallback={<GraphLoading />}>
              <StyleGraph data={graph.data} />
            </Suspense>
          ) : (
            <GraphLoading />
          )}
        </div>
        {graph.data && graph.data.nodes.length > 0 && <Legend />}
      </section>

      <Sources
        sources={sources.data}
        error={sources.error}
        loading={sources.loading}
        onChanged={reloadAfterIngest}
        personas={personas}
        activePersona={activePersona}
        online={online}
      />
    </div>
  );
}

function StatsLine({ stats }: { stats: Stats | null }) {
  if (!stats) return <p className="text-[13px] text-text-3">&nbsp;</p>;
  return (
    <p
      className="text-[13px] leading-snug text-text-2"
      title={`${stats.words.toLocaleString()} distinct words · ${stats.structures.toLocaleString()} sentence shapes`}
    >
      Learned from <span className="tnum font-medium text-text">{stats.samples.toLocaleString()}</span> samples
      <span className="block text-[12px] text-text-3">
        {stats.avg_sentence_len != null && `~${Math.round(stats.avg_sentence_len)} words per sentence · `}
        {stats.words.toLocaleString()} words
      </span>
    </p>
  );
}

function Legend() {
  return (
    <div className="mt-1 flex flex-wrap justify-center gap-x-3.5 gap-y-1 text-[11px] text-text-3">
      {(Object.keys(LABEL_NAME) as NodeLabel[]).map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <GlyphMark label={k} />
          {LABEL_NAME[k]}
        </span>
      ))}
    </div>
  );
}

/* ---------------- Sources (where Polytone learns from) ---------------- */

function Sources({
  sources,
  error,
  loading,
  onChanged,
  personas,
  activePersona,
  online,
}: {
  sources: IngestSource[] | null;
  error: unknown;
  loading: boolean;
  onChanged: () => void;
  personas: Persona[];
  activePersona: string | null;
  online: boolean;
}) {
  const manual = sources?.find((s) => s.name === "manual");
  const others = (sources ?? []).filter((s) => s.name !== "manual");
  return (
    <section>
      <h2 className="group-title">Learns from</h2>
      <div className="inset">
        <PasteRow
          count={manual?.items_ingested ?? null}
          personas={personas}
          activePersona={activePersona}
          online={online}
          onDone={onChanged}
        />
        {others.map((s) => (
          <SourceRow key={s.name} source={s} onDone={onChanged} />
        ))}
        {!sources && loading && <div className="row text-[12px] text-text-3">Loading…</div>}
      </div>
      {error != null && !sources && !isOffline(error) && (
        <div className="mt-2">
          <ErrorLine error={error} />
        </div>
      )}
      <p className="footnote">Only text you wrote yourself is learned. AI-written text, Polytone's own drafts included, is rejected.</p>
    </section>
  );
}

function PasteRow({
  count,
  personas,
  activePersona,
  online,
  onDone,
}: {
  count: number | null;
  personas: Persona[];
  activePersona: string | null;
  online: boolean;
  onDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [persona, setPersona] = useState<string>(activePersona ?? "");
  const [result, setResult] = useState<IngestResult | null>(null);
  const learn = useAction(api.ingestManual);

  useEffect(() => {
    if (activePersona && !persona) setPersona(activePersona);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePersona]);

  const ingest = async () => {
    if (!text.trim()) return;
    setResult(null);
    const r = await learn.run(text, persona || null);
    if (!r) return;
    setResult(r);
    if (r.accepted > 0) setText("");
    onDone();
  };

  return (
    <div className="row flex-col items-stretch !gap-0">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-[13px]">Pasted writing</div>
          <div className="text-[11.5px] text-text-3">
            {count != null ? `${count.toLocaleString()} samples` : "Emails, messages, notes you wrote"}
          </div>
        </div>
        <button type="button" className="btn" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? "Done" : "Add…"}
        </button>
      </div>

      {open && (
        <div className="fade-in mt-2.5 space-y-2 pb-1">
          <label htmlFor="paste-text" className="sr-only">
            Text you wrote yourself
          </label>
          <textarea
            id="paste-text"
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void ingest();
              }
            }}
            placeholder="Paste something you wrote yourself…"
            className="field block h-[120px] resize-none leading-[1.5]"
          />
          <div className="flex items-center justify-between gap-2">
            <label className="flex items-center gap-2 text-[12px] text-text-2">
              Voice
              <select value={persona} onChange={(e) => setPersona(e.target.value)} className="select">
                <option value="">Any</option>
                {personas.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="btn btn-primary"
              disabled={learn.busy || !text.trim() || !online}
              onClick={ingest}
              title={online ? "⌘↵" : "Engine offline"}
            >
              {learn.busy ? "Learning…" : "Learn"}
            </button>
          </div>
          <div aria-live="polite">
            <ErrorLine error={learn.error} />
            {result && <Receipt result={result} />}
          </div>
        </div>
      )}
    </div>
  );
}

function Receipt({ result }: { result: IngestResult }) {
  const ok = result.accepted > 0;
  return (
    <div className="fade-in text-[12px]">
      <p className={ok ? "text-text-2" : "text-bad"}>
        {ok
          ? `Learned ${result.sentences} ${result.sentences === 1 ? "sentence" : "sentences"}, ${result.words_merged} words.`
          : "Not learned."}
        {result.rejected > 0 && ok ? ` ${result.rejected} rejected.` : ""}
      </p>
      {result.reasons.length > 0 && (
        <ul className="selectable mt-0.5 text-text-3">
          {result.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SourceRow({ source, onDone }: { source: IngestSource; onDone: () => void }) {
  const sync = useAction(api.runSource);
  const [done, setDone] = useState<string | null>(null);
  const meta = SOURCE_META[source.name] ?? { name: source.name, blurb: "" };
  const status = source.status ?? "";
  const failing = /error|fail/i.test(status);
  const runnable = source.enabled && source.name !== "typed";

  const run = async () => {
    setDone(null);
    const r = await sync.run(source.name);
    if (!r) return;
    setDone(r.accepted > 0 ? `${r.accepted} new` : `Nothing new${r.reasons[0] ? `: ${r.reasons[0]}` : ""}`);
    onDone();
  };
  const msg = sync.error != null ? { ok: false, text: errorMessage(sync.error) } : done ? { ok: true, text: done } : null;

  // Disabled sources carry their fix in `status` (e.g. Gmail's auth command): show it whole.
  const subtitle = !source.enabled
    ? status && status !== "disabled"
      ? status
      : meta.blurb
    : failing
      ? status
      : source.items_ingested > 0 || source.last_run
        ? `${source.items_ingested.toLocaleString()} samples · ${relativeTime(source.last_run)}`
        : meta.blurb;

  return (
    <div className="row">
      <div className="min-w-0 flex-1">
        <div className={`text-[13px] ${source.enabled ? "" : "text-text-2"}`}>{meta.name}</div>
        <div
          className={`selectable break-words text-[11.5px] leading-snug ${failing ? "text-bad" : "text-text-3"} ${source.enabled ? "truncate" : "line-clamp-2"}`}
          title={status}
        >
          {subtitle}
        </div>
        {msg && <div className={`fade-in text-[11.5px] ${msg.ok ? "text-text-2" : "text-bad"}`}>{msg.text}</div>}
      </div>
      {runnable && (
        <button
          type="button"
          className="icon-btn"
          disabled={sync.busy}
          onClick={run}
          aria-label={`Sync ${meta.name} now`}
          title="Sync now"
        >
          <RefreshIcon className={sync.busy ? "animate-spin" : ""} width={14} height={14} />
        </button>
      )}
    </div>
  );
}
