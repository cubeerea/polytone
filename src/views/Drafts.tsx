import { useState } from "react";
import {
  api,
  isOffline,
  type Draft,
  type Evaluation,
  type EvaluationResult,
  type LearningSummary,
  type ScoreComponents,
  type ScoresSummary,
} from "../api";
import { relativeTime, useAction } from "../hooks";
import { invalidateQueries } from "../query";
import { AFTER_EVALUATION, useConfig, useDrafts, useEvaluations, useLearning, useScoresSummary } from "../queries";
import { Bar, ErrorLine, Loading, OfflineNotice, prettyHotkey } from "../components/bits";
import { Sparkline } from "../components/Sparkline";

const COMPONENTS: { key: keyof ScoreComponents; label: string; hint: string }[] = [
  { key: "sentence_length", label: "Sentence length", hint: "How similar your sentence lengths are" },
  { key: "punctuation", label: "Punctuation", hint: "Commas, dashes, exclamation marks and other punctuation habits" },
  { key: "lexical", label: "Vocabulary", hint: "Word choice and variety" },
  { key: "function_words", label: "Function words", hint: "Small words like 'the', 'just', 'actually' that form your rhythm" },
  { key: "embedding", label: "Meaning", hint: "Whether the draft keeps the meaning of what you typed" },
];

export interface TrendComparison {
  /** Rounded difference in points: mean(last n) − mean(first n). */
  delta: number;
  /** How many drafts are averaged on each side. */
  n: number;
}

/**
 * Compare the average of the most recent drafts with the average of the first ones.
 * Uses n = min(5, ⌊count/2⌋) drafts per side so the two windows never overlap.
 * `scores` is oldest first. Returns null with fewer than 4 scores (too noisy).
 */
export function trendComparison(scores: number[]): TrendComparison | null {
  const valid = scores.filter((s) => Number.isFinite(s));
  if (valid.length < 4) return null;
  const n = Math.min(5, Math.floor(valid.length / 2));
  const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
  const delta = Math.round(mean(valid.slice(-n)) - mean(valid.slice(0, n)));
  return { delta: Object.is(delta, -0) ? 0 : delta, n };
}

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
      <LearningPanel />

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
  const cmp = trendComparison(trend.map((p) => p.score));
  return (
    <section>
      <div className="flex items-end gap-5">
        <div className="flex-none">
          <div className="tnum text-[44px] font-light leading-none tracking-[-0.03em]">
            {summary.mean == null ? "–" : Math.round(summary.mean)}
            <span className="ml-0.5 text-[15px] font-normal tracking-normal text-text-3">/100</span>
          </div>
          <p className="mt-1.5 text-[12px] text-text-2">
            Average style match
            <span className="text-text-3">
              {" "}
              · {summary.count} {summary.count === 1 ? "draft" : "drafts"}
            </span>
          </p>
        </div>
        <div className="min-w-0 flex-1 pb-[3px]">
          {cmp && <TrendLine cmp={cmp} />}
          <Sparkline points={trend} />
        </div>
      </div>
      <p className="mt-1 text-[11.5px] text-text-3">
        How closely your drafts sound like your own writing (100 = indistinguishable).
      </p>
    </section>
  );
}

function TrendLine({ cmp }: { cmp: TrendComparison }) {
  const { delta, n } = cmp;
  const color = delta > 0 ? "var(--color-ok)" : delta < 0 ? "var(--color-bad)" : undefined;
  // n >= 2 always (trendComparison needs at least 4 scores).
  const tip = `Average of your last ${n} drafts vs. your first ${n}`;
  return (
    <p className="tnum mb-1 text-right text-[11.5px]" title={tip}>
      <span className={color ? undefined : "text-text-2"} style={color ? { color } : undefined}>
        {delta === 0 ? "No change" : `${delta > 0 ? "▲" : "▼"} ${Math.abs(delta)} ${Math.abs(delta) === 1 ? "pt" : "pts"}`}
      </span>
      <span className="text-text-3"> recent vs. your first drafts</span>
    </p>
  );
}

const WEEK_MS = 7 * 24 * 3600 * 1000;

export interface WeekComparison {
  /** Style match of the newest evaluation of the last 7 days. */
  thisWeek: number | null;
  /** Style match of the newest evaluation 7-14 days ago. */
  lastWeek: number | null;
}

/** `evals` newest first (GET /learning/evaluations). */
export function weekComparison(evals: Evaluation[], now = Date.now()): WeekComparison {
  const pick = (from: number, to: number) => {
    const e = evals.find((x) => {
      const age = now - Date.parse(x.created_at);
      return age >= from && age < to && x.score != null && Number.isFinite(x.score);
    });
    return e?.score ?? null;
  };
  return { thisWeek: pick(0, WEEK_MS), lastWeek: pick(WEEK_MS, 2 * WEEK_MS) };
}

const PUNCT_NAMES: Record<string, string> = {
  "—": "em-dashes",
  "!": "exclamation marks",
  "!!": "double exclamation marks",
  "…": "ellipses",
  ";": "semicolons",
  "?": "question marks",
  ",": "commas",
  "(": "parentheses",
  ":": "colons",
  emoji: "emoji",
};

/** Plain-language list of what edit learning is correcting (only rules with enough edits behind them). */
export function corrections(l: LearningSummary): string[] {
  const ready = (d: { edits: number } | null | undefined) => d != null && d.edits >= l.min_edits;
  const out: string[] = [];
  if (l.avoid_words.length) out.push(`avoids ${l.avoid_words.slice(0, 4).map((w) => `“${w}”`).join(", ")}`);
  const fewer = Object.entries(l.punctuation)
    .filter(([, d]) => ready(d) && d.value < 0)
    .sort((a, b) => a[1].value - b[1].value)
    .map(([m]) => PUNCT_NAMES[m] ?? m);
  if (fewer.length) out.push(`fewer ${fewer.slice(0, 3).join(", ")}`);
  const len = l.sentence_len_delta;
  if (ready(len) && len && Math.abs(len.value) >= 0.5) out.push(len.value < 0 ? "shorter sentences" : "longer sentences");
  return out;
}

const DECISION: Record<string, { label: string; hint: string }> = {
  accepted: { label: "Using what it learned", hint: "The latest rules sounded most like you." },
  rolled_back: { label: "Rolled back", hint: "Newer learning sounded less like you, so drafts use the earlier rules." },
  corrections_paused: { label: "Edit corrections paused", hint: "Drafts sounded more like you without them." },
};

function LearningPanel() {
  const persona = useConfig().data?.active_persona ?? "Default";
  const learning = useLearning(persona);
  const evals = useEvaluations(persona);
  const evaluate = useAction(api.evaluateLearning);
  const undo = useAction(api.clearLearningState);
  const [result, setResult] = useState<EvaluationResult | null>(null);
  const l = learning.data;
  if (!l) return null;

  const mode = l.state?.mode ?? "normal";
  const last = l.last_evaluation ?? null;
  const week = weekComparison(evals.data?.evaluations ?? []);
  const fixes = corrections(l);
  const decision =
    mode === "pinned"
      ? DECISION.rolled_back
      : mode === "corrections_paused"
        ? DECISION.corrections_paused
        : last
          ? DECISION.accepted
          : null;

  const run = async () => {
    setResult(null);
    const r = await evaluate.run(persona);
    if (r) setResult(r);
    invalidateQueries(AFTER_EVALUATION);
  };
  const revert = async () => {
    await undo.run(persona);
    invalidateQueries(AFTER_EVALUATION);
  };

  return (
    <section aria-label="Learning" className="rounded-lg bg-fill/50 px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-[12px] font-medium">
          Learning <span className="font-normal text-text-3">· {persona}</span>
        </h2>
        <button type="button" className="btn btn-plain" disabled={evaluate.busy} onClick={run}>
          {evaluate.busy ? "Evaluating…" : "Evaluate now"}
        </button>
      </div>
      <dl className="mt-1.5 grid grid-cols-[96px_1fr] gap-x-2.5 gap-y-1 text-[11.5px]">
        <dt className="text-text-3">Style match</dt>
        <dd className="tnum text-text-2" title="Drafts for fixed test intents, scored against your newest samples">
          {week.thisWeek == null && week.lastWeek == null ? (
            last?.score != null ? (
              `${Math.round(last.score)} · ${relativeTime(last.created_at)}`
            ) : (
              <span className="text-text-3">not evaluated yet</span>
            )
          ) : (
            <>
              {week.thisWeek == null ? "–" : Math.round(week.thisWeek)} this week
              <span className="text-text-3"> · {week.lastWeek == null ? "–" : Math.round(week.lastWeek)} last week</span>
            </>
          )}
        </dd>
        {decision && (
          <>
            <dt className="text-text-3">Decision</dt>
            <dd className="text-text-2" title={decision.hint}>
              {decision.label}
              {mode !== "normal" && (
                <button type="button" className="ml-2 text-text-3 underline" disabled={undo.busy} onClick={revert}>
                  Undo
                </button>
              )}
            </dd>
          </>
        )}
        <dt className="text-text-3">From your edits</dt>
        <dd className="text-text-2">
          {fixes.length ? (
            fixes.join(" · ")
          ) : (
            <span className="text-text-3">
              {l.edits_learned ? `${l.edits_learned} edits so far, nothing to correct yet` : "no edits learned yet"}
            </span>
          )}
        </dd>
      </dl>
      <div aria-live="polite">
        {result && (result.status === "skipped" || result.status === "failed") && (
          <p className="mt-1.5 text-[11.5px] text-text-3">{result.reason}</p>
        )}
        <ErrorLine error={evaluate.error ?? undo.error} />
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
          className="flex w-[48px] flex-none flex-col items-end pt-px text-right"
          title={s?.confidence ? CONFIDENCE_HINT[s.confidence] : s ? undefined : "Not scored"}
        >
          <span className={`tnum text-[15px] font-medium leading-tight ${s && !low ? "text-text" : "text-text-3"}`}>
            {s ? Math.round(s.score) : "–"}
          </span>
          <span className="whitespace-nowrap text-[10px] leading-tight text-text-3">{s ? "match" : "not scored"}</span>
        </span>
        <span className="min-w-0 flex-1">
          <span className={`selectable text-[13px] leading-[1.45] ${open ? "block" : "line-clamp-2"}`}>{draft.text}</span>
          <span className="mt-1 block text-[11.5px] text-text-3">
            {draft.persona} · {relativeTime(draft.created_at)}
            {low && " · low confidence"}
          </span>
        </span>
      </button>

      {open && (
        <div className="fade-in space-y-3 pb-3 pl-[68px] pr-2">
          <p className="selectable text-[12px] text-text-2">
            <span className="text-text-3">You typed </span>
            {draft.intent}
          </p>
          {s && (
            <div className="space-y-1.5">
              <p className="text-[11.5px] text-text-3">How this draft matches your style (0–100 per signal)</p>
              {COMPONENTS.map((c) => {
                const v = match(s.components[c.key]);
                return (
                  <div key={c.key} className="grid grid-cols-[96px_1fr_24px] items-center gap-2.5" title={c.hint}>
                    <span className="text-[11.5px] text-text-2">{c.label}</span>
                    <Bar value={v} />
                    <span className="tnum text-right text-[11.5px] text-text-3">{v == null ? "–" : Math.round(v * 100)}</span>
                  </div>
                );
              })}
              <p className="pt-1 text-[11.5px] text-text-3">
                {s.confidence ? `${CONFIDENCE_HINT[s.confidence] ?? s.confidence} ` : ""}
                Drafted in {draft.latency_ms} ms.
              </p>
            </div>
          )}
        </div>
      )}
    </li>
  );
}
