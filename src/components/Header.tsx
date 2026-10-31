import type { Health, Persona } from "../api";
import { isOffline } from "../api";
import { StatusDot } from "./bits";

interface Props {
  health: Health | null;
  healthError: unknown;
  healthLoading: boolean;
  personas: Persona[];
  active: string | null;
  onPersona: (name: string) => void;
  personaDisabled: boolean;
  generating: boolean;
}

export function Header({
  health,
  healthError,
  healthLoading,
  personas,
  active,
  onPersona,
  personaDisabled,
  generating,
}: Props) {
  const state = healthLoading
    ? "unknown"
    : healthError
      ? "down"
      : health
        ? health.neo4j && health.llm_ready !== false
          ? "ok"
          : "warn"
        : "unknown";

  const statusText = healthLoading
    ? "connecting…"
    : healthError
      ? isOffline(healthError)
        ? "offline"
        : "error"
      : health
        ? `${health.llm_ready === false ? "no API key" : health.llm} · ${
            health.neo4j ? "graph ok" : health.graph_store === "memory" ? "memory graph" : "no graph"
          }`
        : "—";

  const title = health
    ? `Backend online\nLLM: ${health.llm}${health.llm_ready === false ? " (not configured — add ANTHROPIC_API_KEY)" : ""}\nEmbeddings: ${health.embeddings}\nGraph: ${
        health.neo4j ? "Neo4j connected" : health.graph_store === "memory" ? "in-memory (Neo4j unreachable, not persisted)" : "Neo4j unavailable"
      }`
    : "Backend offline — run `uv run polytone-api`";

  return (
    <header className="px-4 pt-3" data-tauri-drag-region>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <h1 className="display text-[26px] leading-none tracking-[-0.02em] text-ink">
            Polytone
            <span className="text-ribbon">.</span>
          </h1>
          {generating && (
            <span className="label strike-in normal-case tracking-normal text-ribbon">
              typing<span className="caret">▌</span>
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5" title={title}>
          <StatusDot state={state} />
          <span className="label tracking-[0.1em]">{statusText}</span>
        </div>
      </div>

      <div className="mt-2.5 flex items-center gap-2">
        <span className="label shrink-0">Voice</span>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Active persona">
          {personas.map((p) => (
            <button
              key={p.name}
              type="button"
              className="chip disabled:cursor-not-allowed disabled:opacity-50"
              aria-pressed={active === p.name}
              disabled={personaDisabled}
              title={p.description || p.name}
              onClick={() => onPersona(p.name)}
            >
              {p.name}
            </button>
          ))}
        </div>
      </div>
    </header>
  );
}
