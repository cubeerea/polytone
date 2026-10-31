import { useState, type ReactNode } from "react";
import { errorMessage, isOffline } from "../api";
import { ChevronIcon } from "./Icons";

/** Backend-offline notice: one sentence and the command that fixes it. */
export function OfflineNotice() {
  return (
    <div role="status" className="fade-in py-6 text-center">
      <p className="font-medium text-text">Polytone's engine isn't running</p>
      <p className="mt-1 text-[12px] text-text-2">
        Start it from the repo with{" "}
        <code className="selectable rounded bg-fill px-1.5 py-0.5 font-mono text-[11.5px] text-text">
          uv run polytone-api
        </code>
      </p>
    </div>
  );
}

/** Renders either the offline notice or a one-line error. */
export function ErrorLine({ error }: { error: unknown }) {
  if (!error) return null;
  if (isOffline(error)) return <OfflineNotice />;
  return (
    <p className="selectable text-[12px] text-bad" role="alert">
      {errorMessage(error)}
    </p>
  );
}

export function StatusDot({ state }: { state: "ok" | "warn" | "down" | "unknown" }) {
  const color =
    state === "ok"
      ? "var(--color-ok)"
      : state === "warn"
        ? "var(--color-warn)"
        : state === "down"
          ? "var(--color-bad)"
          : "var(--color-text-3)";
  return <span aria-hidden className="inline-block h-[7px] w-[7px] flex-none rounded-full" style={{ background: color }} />;
}

/** A thin match bar. value 0..1 */
export function Bar({ value }: { value: number | null }) {
  const v = value == null || Number.isNaN(value) ? null : Math.max(0, Math.min(1, value));
  return (
    <div className="relative h-[4px] w-full overflow-hidden rounded-full bg-fill" aria-hidden>
      {v != null && (
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-text-2 transition-[width] duration-500"
          style={{ width: `${v * 100}%` }}
        />
      )}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="breathe py-6 text-center text-[12px] text-text-3" aria-live="polite">
      {label}
    </p>
  );
}

/** Progressive disclosure: a quiet chevron row that reveals its children. */
export function Disclosure({
  title,
  children,
  defaultOpen = false,
  right,
}: {
  title: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  right?: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section>
      <div className="flex items-center justify-between">
        <button
          type="button"
          className="-ml-1 flex items-center gap-1 rounded px-1 py-0.5 text-[12px] font-semibold text-text-2 hover:text-text"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          <ChevronIcon open={open} />
          {title}
        </button>
        {right}
      </div>
      {open && <div className="fade-in mt-2">{children}</div>}
    </section>
  );
}

export function Switch({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="switch"
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  );
}

export function prettyHotkey(accel: string): string {
  return accel
    .replace(/CmdOrCtrl|CommandOrControl/gi, "⌘")
    .replace(/Cmd|Command|Super|Meta/gi, "⌘")
    .replace(/Shift/gi, "⇧")
    .replace(/Alt|Option/gi, "⌥")
    .replace(/Ctrl|Control/gi, "⌃")
    .replace(/\+/g, "");
}
