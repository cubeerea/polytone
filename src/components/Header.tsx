import { useEffect } from "react";
import type { Health, Persona } from "../api";
import { isOffline } from "../api";
import { StatusDot } from "./bits";
import { BackIcon, GearIcon } from "./Icons";

export type ViewId = "drafts" | "style" | "settings";

interface Props {
  view: ViewId;
  onView: (v: ViewId) => void;
  health: Health | null;
  healthError: unknown;
  healthLoading: boolean;
  personas: Persona[];
  active: string | null;
  onPersona: (name: string) => void;
  personaDisabled: boolean;
  generating: boolean;
}

const MAIN: { id: Exclude<ViewId, "settings">; label: string; key: string }[] = [
  { id: "drafts", label: "Drafts", key: "1" },
  { id: "style", label: "Style", key: "2" },
];

export function Header({
  view,
  onView,
  health,
  healthError,
  healthLoading,
  personas,
  active,
  onPersona,
  personaDisabled,
  generating,
}: Props) {
  // ⌘1 / ⌘2 switch views, ⌘, opens Settings (the macOS convention).
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const t = MAIN.find((x) => x.key === e.key);
      if (t) {
        e.preventDefault();
        onView(t.id);
      } else if (e.key === ",") {
        e.preventDefault();
        onView("settings");
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onView]);

  // Status is silent when healthy; it only speaks up when something needs attention.
  const problem = healthLoading
    ? null
    : healthError
      ? isOffline(healthError)
        ? "Offline"
        : "Engine error"
      : health && health.llm_ready === false
        ? "No API key"
        : health && !health.neo4j
          ? "Graph offline"
          : null;
  const state = healthLoading ? "unknown" : healthError ? "down" : problem ? "warn" : "ok";
  const title = health
    ? `Model: ${health.llm === "mock" ? "mock" : "Claude"}${health.llm_ready === false ? " (no API key)" : ""}\nEmbeddings: ${health.embeddings}\nGraph: ${
        health.neo4j ? "Neo4j" : health.graph_store === "memory" ? "in-memory" : "unavailable"
      }`
    : "Engine offline: run `uv run polytone-api`";

  const options = personas.some((p) => p.name === active) || !active ? personas : [...personas, { name: active, description: "" }];

  return (
    <header className="px-4 pb-3 pt-3" data-tauri-drag-region>
      <div className="flex h-[26px] items-center gap-2" data-tauri-drag-region>
        {view === "settings" ? (
          <button type="button" className="btn btn-plain -ml-1 gap-0.5 px-1 text-[13px]" onClick={() => onView("drafts")}>
            <BackIcon width={14} height={14} />
            Back
          </button>
        ) : (
          <h1 className="text-[13px] font-semibold tracking-[-0.01em]" data-tauri-drag-region>
            Polytone
          </h1>
        )}
        {generating && (
          <span className="breathe text-[12px] text-accent" aria-live="polite">
            Writing…
          </span>
        )}

        <div className="ml-auto flex items-center gap-1.5">
          <span className="mr-1 flex items-center gap-1.5 text-[11.5px] text-text-2" title={title}>
            <StatusDot state={state} />
            {problem && <span>{problem}</span>}
            {!problem && <span className="sr-only">Engine online</span>}
          </span>
          <label className="sr-only" htmlFor="persona">
            Voice
          </label>
          <select
            id="persona"
            className="select"
            value={active ?? ""}
            disabled={personaDisabled}
            title="Voice used for the next rewrite"
            onChange={(e) => onPersona(e.target.value)}
          >
            {!active && <option value="">Voice</option>}
            {options.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="icon-btn"
            aria-label="Settings"
            aria-pressed={view === "settings"}
            title="Settings (⌘,)"
            onClick={() => onView(view === "settings" ? "drafts" : "settings")}
          >
            <GearIcon />
          </button>
        </div>
      </div>

      {view !== "settings" && (
        <nav className="mt-3 flex justify-center">
          <div role="tablist" aria-label="Views" className="seg">
            {MAIN.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={view === t.id}
                title={`${t.label} (⌘${t.key})`}
                onClick={() => onView(t.id)}
                onKeyDown={(e) => {
                  if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
                    e.preventDefault();
                    onView(t.id === "drafts" ? "style" : "drafts");
                  }
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
        </nav>
      )}
      {view === "settings" && <h2 className="mt-3 text-[20px] font-semibold tracking-[-0.02em]">Settings</h2>}
    </header>
  );
}
