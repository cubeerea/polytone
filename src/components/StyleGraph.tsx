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
import { zoom, zoomIdentity, type D3ZoomEvent, type ZoomBehavior } from "d3-zoom";
import type { EdgeType, GraphData, GraphEdge, GraphNode } from "../api";
import { LABEL_NAME, NODE_STYLE, glyphPath } from "./graphGlyphs";

type SimNode = GraphNode & SimulationNodeDatum & { r: number };
type SimLink = SimulationLinkDatum<SimNode> & { type: EdgeType; weight: number; w: number };

const EDGE_STYLE: Record<EdgeType, { color: string; opacity: number }> = {
  FOLLOWED_BY: { color: "var(--color-text-3)", opacity: 0.35 },
  PART_OF_STRUCTURE: { color: "var(--color-text-3)", opacity: 0.2 },
  USED_IN: { color: "var(--color-accent)", opacity: 0.35 },
};

const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** The graph stays legible at ~420px: only the heaviest words, and the strongest edges of each kind. */
const MAX_WORDS = 24;
const MAX_EDGES: Record<EdgeType, number> = { FOLLOWED_BY: 60, PART_OF_STRUCTURE: 40, USED_IN: Infinity };
const SETTLED_ALPHA = 0.02;

function prepare(data: GraphData) {
  const words = data.nodes.filter((n) => n.label === "Word").sort((a, b) => b.weight - a.weight);
  const keepWords = new Set(words.slice(0, MAX_WORDS).map((n) => n.id));
  const kept = data.nodes.filter((n) => n.label !== "Word" || keepWords.has(n.id));

  const maxW = Math.max(1, ...kept.map((n) => n.weight));
  const nodes: SimNode[] = kept.map((n) => ({
    ...n,
    r:
      (n.label === "Word" ? 2 : 2.5) +
      Math.sqrt(Math.max(0, n.weight) / maxW) * (n.label === "Word" ? 5 : 6) +
      (n.label === "IntentContext" ? 2.5 : 0),
  }));

  // Degree counts every edge the backend sent (it's what the tooltip reports), not just drawn ones.
  const all = new Set(data.nodes.map((n) => n.id));
  const degree = new Map<string, number>();
  for (const e of data.edges) {
    if (!all.has(e.source) || !all.has(e.target) || e.source === e.target) continue;
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
  }

  const ids = new Set(kept.map((n) => n.id));
  const perType: Record<EdgeType, number> = { FOLLOWED_BY: 0, PART_OF_STRUCTURE: 0, USED_IN: 0 };
  const candidates = data.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target)
    .sort((a, b) => b.weight - a.weight);
  // Every node keeps its strongest edge (so trimming never strands one), then the heaviest
  // remaining edges fill each type's budget.
  const linked = new Set<string>();
  const picked = new Set<GraphEdge>();
  for (const e of candidates) {
    if (linked.has(e.source) && linked.has(e.target)) continue;
    picked.add(e);
    perType[e.type]++;
    linked.add(e.source);
    linked.add(e.target);
  }
  for (const e of candidates) {
    if (!picked.has(e) && perType[e.type] < MAX_EDGES[e.type]) {
      picked.add(e);
      perType[e.type]++;
    }
  }
  const valid = [...picked];
  const maxE = Math.max(1, ...valid.map((e) => e.weight));
  const links: SimLink[] = valid.map((e) => ({
    source: e.source,
    target: e.target,
    type: e.type,
    weight: e.weight,
    w: 0.5 + Math.sqrt(Math.max(0, e.weight) / maxE) * 2,
  }));
  return { nodes, links, degree };
}

export default function StyleGraph({ data, height = 280 }: { data: GraphData; height?: number }) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const [hover, setHover] = useState<{ node: SimNode; x: number; y: number; degree: number } | null>(null);

  const { nodes, links, degree } = useMemo(() => prepare(data), [data]);

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
      .on("zoom", (e: D3ZoomEvent<SVGSVGElement, unknown>) => {
        root.attr("transform", e.transform.toString());
        setHover((h) => (h ? null : h));
      });
    zoomRef.current = z;
    svg.call(z).on("dblclick.zoom", fit);

    // Stop well before d3's default alphaMin (0.001, ~300 ticks): past this point nodes move
    // sub-pixel, so the remaining ticks only burn CPU.
    sim.alphaMin(SETTLED_ALPHA);
    const timers: ReturnType<typeof setTimeout>[] = [];
    if (prefersReducedMotion()) {
      sim.stop();
      sim.tick(Math.ceil(Math.log(SETTLED_ALPHA) / Math.log(1 - sim.alphaDecay())));
      render();
      fit();
    } else {
      sim.on("tick", render);
      sim.on("end", fit);
      timers.push(setTimeout(fit, 450), setTimeout(fit, 1600));
    }

    return () => {
      sim.stop();
      timers.forEach(clearTimeout);
      svg.on(".zoom", null);
    };
  }, [nodes, links, degree, height]);

  return (
    <div className="relative overflow-hidden" style={{ height }}>
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
    </div>
  );
}

function fmt(n: number): string {
  return Number.isInteger(n) ? n.toLocaleString() : n.toFixed(1);
}
