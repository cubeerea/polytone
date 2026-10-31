import type { ReactNode } from "react";
import { OFFLINE_HINT, errorMessage, isOffline } from "../api";

/** Backend-offline notice — a typed slip of paper with a ribbon-red margin. */
export function OfflineNotice({ compact = false }: { compact?: boolean }) {
  const [lead, cmd] = OFFLINE_HINT.split(" — run ");
  return (
    <div
      role="status"
      className={`card border-l-2 strike-in ${compact ? "px-3 py-2" : "px-4 py-3"}`}
      style={{ borderLeftColor: "var(--color-ribbon)" }}
    >
      <div className="flex items-center gap-2">
        <span className="inline-block h-1.5 w-1.5 rounded-full bg-ribbon" aria-hidden />
        <span className="font-medium text-ink">{lead}</span>
      </div>
      <p className="mt-1 text-ink-soft">
        Start it from the repo root:{" "}
        <code className="selectable whitespace-nowrap rounded-sm bg-paper-deep px-1.5 py-0.5 text-ink">
          {cmd?.replace(/`/g, "") ?? "uv run polytone-api"}
        </code>
      </p>
    </div>
  );
}

/** Renders either the offline notice or a generic error line. */
export function ErrorLine({ error }: { error: unknown }) {
  if (!error) return null;
  if (isOffline(error)) return <OfflineNotice compact />;
  return (
    <p className="selectable border-l-2 border-ribbon pl-2 text-ribbon" role="alert">
      {errorMessage(error)}
    </p>
  );
}

export function SectionHead({ title, kicker, right }: { title: string; kicker?: string; right?: ReactNode }) {
  return (
    <div className="mb-2 flex items-end justify-between gap-3 border-b hairline pb-1.5">
      <div className="min-w-0">
        {kicker && <div className="label">{kicker}</div>}
        <h2 className="display text-[19px] leading-tight text-ink">{title}</h2>
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  );
}

export function StatusDot({ state }: { state: "ok" | "warn" | "down" | "unknown" }) {
  const color =
    state === "ok"
      ? "var(--color-ok)"
      : state === "warn"
        ? "var(--color-warn)"
        : state === "down"
          ? "var(--color-ribbon)"
          : "var(--color-ink-faint)";
  return (
    <span
      aria-hidden
      className={`inline-block h-2 w-2 rounded-full ${state === "ok" ? "dot-live" : ""}`}
      style={{
        background: color,
        boxShadow: "inset 0 -1px 1px rgb(0 0 0 / 0.3), inset 0 1px 1px rgb(255 255 255 / 0.4)",
      }}
    />
  );
}

/** A thin bar, like a ribbon gauge. value 0..1 */
export function Gauge({ value, muted = false }: { value: number | null; muted?: boolean }) {
  const v = value == null || Number.isNaN(value) ? null : Math.max(0, Math.min(1, value));
  return (
    <div className="relative h-[5px] w-full overflow-hidden rounded-[1px] bg-paper-deep" aria-hidden>
      {/* tick marks */}
      <div
        className="absolute inset-0"
        style={{
          backgroundImage: "repeating-linear-gradient(90deg, transparent 0 9.5%, var(--color-rule) 9.5% 10%)",
        }}
      />
      {v != null && (
        <div
          className="absolute inset-y-0 left-0 transition-[width] duration-500"
          style={{ width: `${v * 100}%`, background: muted ? "var(--color-ink-soft)" : "var(--color-ink)" }}
        />
      )}
    </div>
  );
}

export function Spinner({ label = "working" }: { label?: string }) {
  return (
    <span className="label inline-flex items-center gap-1 normal-case tracking-normal" aria-live="polite">
      {label}
      <span className="caret text-ribbon">▌</span>
    </span>
  );
}
