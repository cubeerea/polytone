import { useState } from "react";
import { isOffline, type Draft, type ScoreComponents, type ScoresSummary } from "../api";
import { relativeTime } from "../hooks";
import { useDrafts, useScoresSummary } from "../queries";
import { Bar, ErrorLine, Loading, OfflineNotice, prettyHotkey } from "../components/bits";
import { Sparkline } from "../components/Sparkline";

const COMPONENTS: { key: keyof ScoreComponents; label: string }[] = [
  { key: "sentence_length", label: "Sentence length" },
  { key: "punctuation", label: "Punctuation" },
  { key: "lexical", label: "Vocabulary" },
  { key: "function_words", label: "Function words" },
  { key: "embedding", label: "Meaning" },
];

/** Components arrive as 0..1 DISTANCES (0 = identical); show them as match = 1 - distance. */
const match = (v: number | null | undefined): number | null =>
  v == null || !Number.isFinite(v) ? null : 1 - Math.max(0, Math.min(1, v));

const CONFIDENCE_HINT: Record<string, string> = {
  low: "Low confidence: few samples for this voice, or a very short draft.",
  medium: "Medium confidence.",
  high: "High confidence.",
};

export function Drafts({ hotkey }: { hotkey: string }) {
  const summary = useScoresSummary();
  const drafts = useDrafts();

  if (summary.error && !summary.data && isOffline(summary.error)) return <OfflineNotice />;
  if (!summary.data && !drafts.data && (summary.loading || drafts.loading)) return <Loading />;

  const empty = summary.data?.count === 0 || (drafts.data != null && drafts.data.length === 0);
  if (empty) return <EmptyState hotkey={hotkey} />;

  return (
    <div className="space-y-6 pt-1">
      {summary.data && <Score summary={summary.data} />}
      <ErrorLine error={summary.error && !isOffline(summary.error) ? summary.error : null} />

      <section aria-label="Recent drafts">
        {drafts.error != null && !drafts.data && <ErrorLine error={drafts.error} />}
        <ol className="-mx-2">
          {(drafts.data ?? []).map((d) => (
            <DraftRow key={d.draft_id} draft={d} />
          ))}
        </ol>
      </section>
    </div>
  );
}

function Score({ summary }: { summary: ScoresSummary }) {
  const trend = summary.trend ?? [];
  const delta = trend.length >= 2 ? trend[trend.length - 1].score - trend[0].score : null;
  return (
    <section className="flex items-end gap-5">
      <div className="flex-none">
        <div className="tnum text-[44px] font-light leading-none tracking-[-0.03em]" title="0–100, where 100 reads exactly like your own writing">
          {summary.mean == null ? "–" : Math.round(summary.mean)}
        </div>
        <p className="mt-1.5 text-[12px] text-text-2">
          Style match
          <span className="text-text-3">
            {" "}
            · {summary.count} {summary.count === 1 ? "draft" : "drafts"}
          </span>
        </p>
      </div>
      <div className="min-w-0 flex-1 pb-[3px]">
        {delta != null && (
          <p className="tnum mb-1 text-right text-[11.5px] text-text-3">
            {delta >= 0 ? "+" : "−"}
            {Math.abs(delta).toFixed(0)} since first
          </p>
        )}
        <Sparkline points={trend} />
      </div>
    </section>
  );
}

function EmptyState({ hotkey }: { hotkey: string }) {
  return (
    <section className="flex flex-col items-center px-6 pt-16 text-center">
      <div className="kbd rounded-md bg-fill px-2.5 py-1 text-[15px]">{prettyHotkey(hotkey)}</div>
      <h2 className="mt-5 text-[15px] font-semibold">No drafts yet</h2>
      <p className="mt-1.5 max-w-[30ch] text-[13px] text-text-2">
        Type something rough in any app, then press {prettyHotkey(hotkey)}. Polytone rewrites it in your voice and
        scores it here.
      </p>
    </section>
  );
}

function DraftRow({ draft }: { draft: Draft }) {
  const [open, setOpen] = useState(false);
  const s = draft.score;
  const low = s?.confidence === "low";
  return (
    <li className="rounded-lg transition-colors hover:bg-fill/60 [&+&]:mt-0.5">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 rounded-lg px-2 py-2.5 text-left"
      >
        <span
          className={`tnum w-[26px] flex-none pt-px text-right text-[15px] font-medium ${s ? (low ? "text-text-3" : "text-text") : "text-text-3"}`}
          title={s?.confidence ? CONFIDENCE_HINT[s.confidence] : s ? undefined : "Not scored"}
        >
          {s ? Math.round(s.score) : "–"}
        </span>
        <span className="min-w-0 flex-1">
          <span className={`selectable text-[13px] leading-[1.45] ${open ? "block" : "line-clamp-2"}`}>{draft.text}</span>
          <span className="mt-1 block text-[11.5px] text-text-3">
            {draft.persona} · {relativeTime(draft.created_at)}
          </span>
        </span>
      </button>

      {open && (
        <div className="fade-in space-y-3 pb-3 pl-[46px] pr-2">
          <p className="selectable text-[12px] text-text-2">
            <span className="text-text-3">You typed </span>
            {draft.intent}
          </p>
          {s && (
            <div className="space-y-1.5">
              {COMPONENTS.map((c) => {
                const v = match(s.components[c.key]);
                return (
                  <div key={c.key} className="grid grid-cols-[96px_1fr_24px] items-center gap-2.5">
                    <span className="text-[11.5px] text-text-2">{c.label}</span>
                    <Bar value={v} />
                    <span className="tnum text-right text-[11.5px] text-text-3">{v == null ? "–" : Math.round(v * 100)}</span>
                  </div>
                );
              })}
              <p className="pt-1 text-[11.5px] text-text-3">
                {s.confidence ? `${CONFIDENCE_HINT[s.confidence] ?? s.confidence} ` : ""}
                {draft.latency_ms} ms.
              </p>
            </div>
          )}
        </div>
      )}
    </li>
  );
}
