import { useEffect, useRef, type KeyboardEvent } from "react";

export type TabId = "feed" | "scoreboard" | "graph" | "settings";

const TABS: { id: TabId; label: string; key: string }[] = [
  { id: "feed", label: "Feed", key: "1" },
  { id: "scoreboard", label: "Scoreboard", key: "2" },
  { id: "graph", label: "Graph", key: "3" },
  { id: "settings", label: "Settings", key: "4" },
];

/** A row of keycaps. The active tab stays "pressed" into the deck. */
export function Tabs({ value, onChange }: { value: TabId; onChange: (t: TabId) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const n = (i + (e.key === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length;
    onChange(TABS[n].id);
    refs.current[n]?.focus();
  };

  // ⌘1–⌘4 switch tabs
  useEffect(() => {
    const h = (e: globalThis.KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const t = TABS.find((x) => x.key === e.key);
      if (t) {
        e.preventDefault();
        onChange(t.id);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onChange]);

  return (
    <nav className="mt-3 px-4">
      <div
        role="tablist"
        aria-label="Sections"
        className="grid grid-cols-4 gap-2 rounded-[8px] border hairline bg-paper-deep/70 p-1.5 pb-2"
        style={{ boxShadow: "inset 0 1px 3px rgb(0 0 0 / 0.1)" }}
      >
        {TABS.map((t, i) => {
          const active = t.id === value;
          return (
            <button
              key={t.id}
              ref={(el) => {
                refs.current[i] = el;
              }}
              role="tab"
              aria-selected={active}
              tabIndex={active ? 0 : -1}
              data-pressed={active}
              onClick={() => onChange(t.id)}
              onKeyDown={(e) => onKey(e, i)}
              title={`${t.label} (⌘${t.key})`}
              className={`keycap w-full px-1 text-[10px] tracking-[0.04em] ${active ? "keycap-ink" : ""}`}
            >
              {t.label}
            </button>
          );
        })}
      </div>
    </nav>
  );
}
