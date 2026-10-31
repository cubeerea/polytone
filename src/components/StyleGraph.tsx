import { useEffect, useMemo, useRef, useState } from "react";
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { select } from "d3-selection";
import { zoom, zoomIdentity, type ZoomBehavior } from "d3-zoom";
import type { EdgeType, GraphData, GraphNode, NodeLabel } from "../api";

type SimNode = GraphNode & SimulationNodeDatum & { r: number };
type SimLink = SimulationLinkDatum<SimNode> & { type: EdgeType; weight: number; w: number };

export const LABEL_NAME: Record<NodeLabel, string> = {
  Word: "Words",
  SentenceStructure: "Sentence shapes",
  PunctuationMark: "Punctuation",
  IntentContext: "Voices",
};

const NODE_STYLE: Record<NodeLabel, { fill: string; stroke: string; sw: number }> = {
  Word: { fill: "var(--color-text-3)", stroke: "none", sw: 0 },
  SentenceStructure: { fill: "var(--color-surface)", stroke: "var(--color-text-2)", sw: 1.2 },
  PunctuationMark: { fill: "var(--color-accent)", stroke: "none", sw: 0 },
  IntentContext: { fill: "var(--color-text)", stroke: "none", sw: 0 },
};

const EDGE_STYLE: Record<EdgeType, { color: string; opacity: number }> = {
  FOLLOWED_BY: { color: "var(--color-text-3)", opacity: 0.35 },
  PART_OF_STRUCTURE: { color: "var(--color-text-3)", opacity: 0.2 },
  USED_IN: { color: "var(--color-accent)", opacity: 0.35 },
};

const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

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

export function GraphFrame({ children, height }: { children: React.ReactNode; height: number }) {
  return (
    <div className="relative overflow-hidden" style={{ height }}>
      {children}
    </div>
  );
}

export function StyleGraph({ data, height = 280 }: { data: GraphData; height?: number }) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const [hover, setHover] = useState<{ node: SimNode; x: number; y: number; degree: number } | null>(null);

  const { nodes, links, degree } = useMemo(() => {
    const maxW = Math.max(1, ...data.nodes.map((n) => n.weight));
    const ids = new Set(data.nodes.map((n) => n.id));
    const nodes: SimNode[] = data.nodes.map((n) => ({
      ...n,
      r: (n.label === "Word" ? 2 : 2.5) + Math.sqrt(Math.max(0, n.weight) / maxW) * (n.label === "Word" ? 5 : 6) + (n.label === "IntentContext" ? 2.5 : 0),
    }));
    const valid = data.edges.filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target);
    const maxE = Math.max(1, ...valid.map((e) => e.weight));
    const links: SimLink[] = valid.map((e) => ({
      source: e.source,
      target: e.target,
      type: e.type,
      weight: e.weight,
      w: 0.5 + Math.sqrt(Math.max(0, e.weight) / maxE) * 2,
    }));
    const degree = new Map<string, number>();
    for (const e of valid) {
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
    }
    return { nodes, links, degree };
  }, [data]);

  useEffect(() => {
    const svgEl = svgRef.current;
    const wrap = wrapRef.current;
    if (!svgEl || !wrap) return;
    const width = wrap.clientWidth || 388;
    const h = wrap.clientHeight || height;

    const svg = select(svgEl).attr("viewBox", `${-width / 2} ${-h / 2} ${width} ${h}`);
    svg.selectAll("*").remove();
    const root = svg.append("g");

    const labelThreshold = [...nodes].sort((a, b) => b.weight - a.weight)[Math.min(5, nodes.length - 1)]?.weight ?? 0;

    const linkSel = root
      .append("g")
      .attr("stroke-linecap", "round")
      .selectAll<SVGLineElement, SimLink>("line")
      .data(links)
      .join("line")
      .style("stroke", (d) => EDGE_STYLE[d.type].color)
      .style("stroke-opacity", (d) => EDGE_STYLE[d.type].opacity)
      .attr("stroke-width", (d) => d.w);

    const nodeSel = root
      .append("g")
      .selectAll<SVGPathElement, SimNode>("path")
      .data(nodes, (d) => d.id)
      .join("path")
      .attr("d", (d) => glyphPath(d.label, d.r))
      .style("fill", (d) => NODE_STYLE[d.label].fill)
      .style("stroke", (d) => NODE_STYLE[d.label].stroke)
      .attr("stroke-width", (d) => NODE_STYLE[d.label].sw)
      .style("fill-opacity", (d) => (d.label === "Word" ? 0.75 : 1))
      .style("cursor", "default");

    const labelSel = root
      .append("g")
      .style("pointer-events", "none")
      .selectAll<SVGTextElement, SimNode>("text")
      .data(nodes.filter((d) => d.weight >= labelThreshold || d.label === "IntentContext"))
      .join("text")
      .text((d) => (d.name.length > 16 ? `${d.name.slice(0, 15)}…` : d.name))
      .attr("dx", (d) => d.r + 4)
      .attr("dy", 3.5)
      .style("font-family", "var(--font-sans)")
      .style("font-size", "10px")
      .style("font-weight", (d) => (d.label === "IntentContext" ? "600" : "400"))
      .style("fill", (d) => (d.label === "IntentContext" ? "var(--color-text)" : "var(--color-text-2)"))
      .style("paint-order", "stroke")
      .style("stroke", "var(--color-bg)")
      .style("stroke-width", "3px");

    const toScreen = (x: number, y: number) => {
      const pt = svgEl.createSVGPoint();
      pt.x = x;
      pt.y = y;
      const m = (root.node() as SVGGElement).getScreenCTM();
      const r = wrap.getBoundingClientRect();
      if (!m) return { x: 0, y: 0 };
      const p = pt.matrixTransform(m);
      return { x: p.x - r.left, y: p.y - r.top };
    };

    nodeSel
      .on("mouseenter", function (_e, d) {
        const p = toScreen(d.x ?? 0, d.y ?? 0);
        setHover({ node: d, x: p.x, y: p.y, degree: degree.get(d.id) ?? 0 });
        linkSel.style("stroke-opacity", (l) =>
          (l.source as SimNode).id === d.id || (l.target as SimNode).id === d.id ? 0.9 : 0.06,
        );
      })
      .on("mouseleave", () => {
        setHover(null);
        linkSel.style("stroke-opacity", (l) => EDGE_STYLE[l.type].opacity);
      });

    const sim = forceSimulation<SimNode>(nodes)
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance((l) => (l.type === "FOLLOWED_BY" ? 38 : 60))
          .strength((l) => (l.type === "FOLLOWED_BY" ? 0.15 : 0.18)),
      )
      .force("charge", forceManyBody<SimNode>().strength((d) => -80 - d.r * 8))
      .force("collide", forceCollide<SimNode>().radius((d) => d.r + 4))
      .force("x", forceX(0).strength(0.05))
      .force("y", forceY(0).strength(0.08));

    const render = () => {
      linkSel
        .attr("x1", (l) => (l.source as SimNode).x ?? 0)
        .attr("y1", (l) => (l.source as SimNode).y ?? 0)
        .attr("x2", (l) => (l.target as SimNode).x ?? 0)
        .attr("y2", (l) => (l.target as SimNode).y ?? 0);
      nodeSel.attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
      labelSel.attr("x", (d) => d.x ?? 0).attr("y", (d) => d.y ?? 0);
    };

    const fit = () => {
      if (!nodes.length) return;
      const xs = nodes.map((n) => n.x ?? 0);
      const ys = nodes.map((n) => n.y ?? 0);
      const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
      const k = Math.min(1.6, 0.9 / Math.max((x1 - x0 + 60) / width, (y1 - y0 + 30) / h));
      svg.call(zoomRef.current!.transform, zoomIdentity.scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2));
    };

    const z = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.3, 5])
      .on("zoom", (e) => {
        root.attr("transform", e.transform.toString());
        setHover(null);
      });
    zoomRef.current = z;
    svg.call(z).on("dblclick.zoom", fit);

    if (prefersReducedMotion()) {
      sim.stop();
      sim.tick(300);
      render();
      fit();
    } else {
      sim.on("tick", render);
      sim.on("end", fit);
      setTimeout(fit, 450);
      setTimeout(fit, 1600);
    }

    return () => {
      sim.stop();
      svg.on(".zoom", null);
    };
  }, [nodes, links, degree, height]);

  return (
    <GraphFrame height={height}>
      <div ref={wrapRef} className="absolute inset-0">
        <svg
          ref={svgRef}
          className="h-full w-full touch-none"
          role="img"
          aria-label={`Style graph with ${nodes.length} nodes. Scroll to zoom, drag to pan, double-click to fit.`}
        />
        {hover && (
          <div
            className="fade-in pointer-events-none absolute z-10 max-w-[200px] rounded-lg bg-surface px-2.5 py-1.5 shadow-[0_4px_16px_-4px_rgb(0_0_0/0.25),0_0_0_0.5px_var(--color-sep-strong)]"
            style={{
              left: Math.min(hover.x + 12, (wrapRef.current?.clientWidth ?? 388) - 205),
              top: Math.max(4, hover.y - 50),
            }}
          >
            <div className="truncate text-[13px] font-medium">{hover.node.name}</div>
            <div className="tnum text-[11px] text-text-3">
              {LABEL_NAME[hover.node.label] ?? hover.node.label} · used {fmt(hover.node.weight)}× · {hover.degree} links
            </div>
          </div>
        )}
      </div>
    </GraphFrame>
  );
}

function fmt(n: number): string {
  return Number.isInteger(n) ? n.toLocaleString() : n.toFixed(1);
}
