// Node glyphs shared by the Style legend and the (lazy-loaded) graph. No d3 in here, so the
// legend doesn't pull the graph chunk into the main bundle.
import type { NodeLabel } from "../api";

export const LABEL_NAME: Record<NodeLabel, string> = {
  Word: "Words",
  SentenceStructure: "Sentence shapes",
  PunctuationMark: "Punctuation",
  IntentContext: "Voices",
};

export const NODE_STYLE: Record<NodeLabel, { fill: string; stroke: string; sw: number }> = {
  Word: { fill: "var(--color-text-3)", stroke: "none", sw: 0 },
  SentenceStructure: { fill: "var(--color-surface)", stroke: "var(--color-text-2)", sw: 1.2 },
  PunctuationMark: { fill: "var(--color-accent)", stroke: "none", sw: 0 },
  IntentContext: { fill: "var(--color-text)", stroke: "none", sw: 0 },
};

/** Node glyph centred on 0,0. Shape carries the type; colour stays quiet. */
export function glyphPath(label: NodeLabel, r: number): string {
  switch (label) {
    case "SentenceStructure": {
      const s = r * 0.85;
      return `M${-s},${-s}H${s}V${s}H${-s}Z`;
    }
    case "PunctuationMark":
      return `M0,${-r * 1.15}L${r * 1.15},0L0,${r * 1.15}L${-r * 1.15},0Z`;
    default:
      return `M${r},0A${r},${r} 0 1,1 ${-r},0A${r},${r} 0 1,1 ${r},0Z`;
  }
}

export function GlyphMark({ label }: { label: NodeLabel }) {
  const st = NODE_STYLE[label];
  return (
    <svg width="10" height="10" viewBox="-6 -6 12 12" aria-hidden>
      <path d={glyphPath(label, 4)} style={{ fill: st.fill, stroke: st.stroke }} strokeWidth={st.sw} />
    </svg>
  );
}
