import { useState } from "react";
import { api, isOffline, type Draft, type ScoreComponents, type ScoresSummary } from "../api";
import { relativeTime, useResource } from "../hooks";
import { ErrorLine, Gauge, OfflineNotice, SectionHead, Spinner } from "../components/bits";
import { Odometer } from "../components/Odometer";
import { Sparkline } from "../components/Sparkline";

const COMPONENTS: { key: keyof ScoreComponents; label: string }[] = [
  { key: "sentence_length", label: "Sentence len" },
  { key: "punctuation", label: "Punctuation" },
  { key: "lexical", label: "Lexical" },
  { key: "function_words", label: "Function wds" },
  { key: "embedding", label: "Embedding" },
];

/**
 * Per-component values. The backend (evaluation/style_distance.py) reports every
 * component as a 0..1 DISTANCE (0 = identical), like the overall `distance` — so
 * bars show *match* = 1 - distance, aligned with the 0-100 score where higher is closer.
 */
const COMPONENTS_ARE_DISTANCES = true;
const norm = (v: number | null | undefined): number | null => {
  if (v == null || !Number.isFinite(v)) return null;
  const x = Math.max(0, Math.min(1, v));
  return COMPONENTS_ARE_DISTANCES ? 1 - x : x;
};

const CONFIDENCE_HINT: Record<string, string> = {
  low: "Low confidence: few samples for this voice or a very short draft",
  medium: "Medium confidence",
  high: "High confidence: plenty of samples and a long enough draft",
};

function prettyHotkey(accel: string): string {
  return accel
    .replace(/CmdOrCtrl|CommandOrControl/gi, "⌘")
    .replace(/Cmd|Command|Super|Meta/gi, "⌘")
    .replace(/Shift/gi, "⇧")
    .replace(/Alt|Option/gi, "⌥")
    .replace(/Ctrl|Control/gi, "⌃")
    .replace(/\+/g, "");
}

export function Scoreboard({ tick, hotkey }: { tick: number; hotkey: string }) {
  const summary = useResource<ScoresSummary>(api.scoresSummary, [tick], 15_000);
  const drafts = useResource<Draft[]>(() => api.drafts(50), [tick], 15_000);

  if (summary.error && !summary.data && isOffline(summary.error)) {
    return (
      <div className="space-y-4">
        <ScoreHero summary={null} />
        <OfflineNotice />
      </div>
    );
  }

  const empty = summary.data?.count === 0 || (drafts.data != null && drafts.data.length === 0);

  return (
    <div className="space-y-5">
      <ScoreHero summary={summary.data} loading={summary.loading && !summary.data} />
      <ErrorLine error={summary.error && !isOffline(summary.error) ? summary.error : null} />

      {empty ? (
        <EmptyState hotkey={hotkey} />
      ) : (
        <section>
          <SectionHead
            kicker="Ledger"
            title="Recent drafts"
            right={<span className="label tabular-nums">{drafts.data?.length ?? 0} shown</span>}
          />
          {drafts.error != null && !drafts.data && <ErrorLine error={drafts.error} />}
          {!drafts.data && drafts.loading && <Spinner label="loading" />}
          <ol className="divide-y divide-[var(--color-rule)]">
            {(drafts.data ?? []).map((d) => (
              <DraftRow key={d.draft_id} draft={d} />
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function ScoreHero({ summary, loading = false }: { summary: ScoresSummary | null; loading?: boolean }) {
  const trend = summary?.trend ?? [];
  const delta = trend.length >= 2 ? trend[trend.length - 1].score - trend[0].score : null;
  return (
    <section className="card px-4 pb-3 pt-3.5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="label">Style score</div>
          <p className="mt-0.5 max-w-[150px] font-display text-[13px] italic leading-snug text-ink-soft">
            How close drafts sound to you. 100 is indistinguishable.
          </p>
        </div>
        <Odometer value={summary?.mean ?? null} />
      </div>

      <div className="mt-3 border-t hairline pt-2">
        <div className="mb-1 flex items-baseline justify-between">
          <span className="label">Trend</span>
          <span className="label tabular-nums normal-case tracking-normal">
            {loading
              ? "…"
              : summary
                ? `${summary.count} scored${
                    delta != null ? ` · ${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta).toFixed(1)}` : ""
                  }`
                : "—"}
          </span>
        </div>
        <Sparkline points={trend} />
      </div>
    </section>
  );
}

function EmptyState({ hotkey }: { hotkey: string }) {
  const keys = prettyHotkey(hotkey).split("");
  return (
    <section className="card border-l-2 px-5 py-5" style={{ borderLeftColor: "var(--color-ribbon)" }}>
      <div className="label">Nothing scored yet</div>
      <h3 className="display mt-1 text-[20px] leading-tight text-ink">Write something rough, anywhere.</h3>
      <ol className="mt-3 space-y-[6px] text-[12px] leading-[16px] text-ink-soft">
        <li>1. Type or select a rough intent in any app.</li>
        <li className="flex flex-wrap items-center gap-1.5">
          2. Press
          {keys.map((k, i) => (
            <kbd key={i} className="keycap keycap-sm min-w-[24px] px-1.5 normal-case">
              {k}
            </kbd>
          ))}
        </li>
        <li>3. Polytone rewrites it in your voice and scores it here.</li>
      </ol>
      <p className="mt-3 text-[11px] text-ink-faint">Feed it more of your own writing to sharpen the score.</p>
    </section>
  );
}

function DraftRow({ draft }: { draft: Draft }) {
  const [open, setOpen] = useState(false);
  const s = draft.score;
  return (
    <li className="py-2.5">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 text-left"
      >
        <div className="w-[46px] shrink-0 text-right">
          <div className={`display text-[22px] leading-none tabular-nums ${s ? (s.confidence === "low" ? "text-ink-soft" : "text-ink") : "text-ink-faint"}`}>
            {s ? Math.round(s.score) : "—"}
          </div>
          <div className="label mt-0.5 text-[8px]" title={s?.confidence ? CONFIDENCE_HINT[s.confidence] : undefined}>
            {s ? (s.confidence && s.confidence !== "high" ? `${s.confidence} conf` : "score") : "unscored"}
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2">
            <span className="label normal-case tracking-[0.06em] text-ink-soft">{draft.persona}</span>
            <span className="label normal-case tracking-normal">
              {relativeTime(draft.created_at)} · {draft.latency_ms}ms
            </span>
          </div>
          <p className={`selectable mt-0.5 text-[12px] leading-snug text-ink ${open ? "" : "line-clamp-2"}`}>
            {draft.text}
          </p>
        </div>
      </button>

      {open && (
        <div className="strike-in mt-2 ml-[58px] space-y-2">
          <p className="selectable border-l-2 border-[var(--color-rule-strong)] pl-2 text-[11px] italic text-ink-faint">
            intent: {draft.intent}
          </p>
          {s && (
            <div className="space-y-1">
              {s.confidence && (
                <p className="text-[10.5px] text-ink-faint">{CONFIDENCE_HINT[s.confidence] ?? s.confidence}.</p>
              )}
              {COMPONENTS.map((c) => {
                const v = norm(s.components[c.key]);
                return (
                  <div key={c.key} className="grid grid-cols-[84px_1fr_30px] items-center gap-2">
                    <span className="label text-[8.5px] tracking-[0.08em]">{c.label}</span>
                    <Gauge value={v} muted={c.key === "embedding"} />
                    <span className="text-right text-[10px] tabular-nums text-ink-soft">
                      {v == null ? "n/a" : Math.round(v * 100)}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {!open && s && (
        <div className="mt-1.5 ml-[58px] grid grid-cols-5 gap-1" aria-hidden>
          {COMPONENTS.map((c) => (
            <Gauge key={c.key} value={norm(s.components[c.key])} muted={c.key === "embedding"} />
          ))}
        </div>
      )}
    </li>
  );
}
