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
import { api, isOffline, type EdgeType, type GraphData, type GraphNode, type NodeLabel, type Persona, type Stats } from "../api";
import { useResource } from "../hooks";
import { ErrorLine, OfflineNotice, SectionHead, Spinner } from "../components/bits";

type SimNode = GraphNode & SimulationNodeDatum & { r: number };
type SimLink = SimulationLinkDatum<SimNode> & { type: EdgeType; weight: number; w: number };

const LABEL_META: Record<NodeLabel, { name: string; glyph: string }> = {
  Word: { name: "Word", glyph: "●" },
  SentenceStructure: { name: "Structure", glyph: "■" },
  PunctuationMark: { name: "Punctuation", glyph: "◆" },
  IntentContext: { name: "Intent", glyph: "◎" },
};

const EDGE_META: Record<EdgeType, { name: string; dash: string; color: string; opacity: number }> = {
  FOLLOWED_BY: { name: "followed by", dash: "", color: "var(--color-ink)", opacity: 0.55 },
  PART_OF_STRUCTURE: { name: "part of", dash: "1 3", color: "var(--color-ink-soft)", opacity: 0.7 },
  USED_IN: { name: "used in", dash: "6 2 1 2", color: "var(--color-ribbon)", opacity: 0.55 },
};

const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Draws a node glyph path centred on 0,0 with radius r. */
function glyphPath(label: NodeLabel, r: number): string {
  switch (label) {
    case "SentenceStructure": {
      const s = r * 0.9;
      return `M${-s},${-s}H${s}V${s}H${-s}Z`;
    }
    case "PunctuationMark":
      return `M0,${-r * 1.2}L${r * 1.2},0L0,${r * 1.2}L${-r * 1.2},0Z`;
    case "IntentContext": {
      // hexagonal nut
      const pts = Array.from({ length: 6 }, (_, i) => {
        const a = (Math.PI / 3) * i + Math.PI / 6;
        return `${(Math.cos(a) * r * 1.15).toFixed(2)},${(Math.sin(a) * r * 1.15).toFixed(2)}`;
      });
      return `M${pts.join("L")}Z`;
    }
    default:
      return `M${r},0A${r},${r} 0 1,1 ${-r},0A${r},${r} 0 1,1 ${r},0Z`;
  }
}

export function GraphView({ personas, tick }: { personas: Persona[]; tick: number }) {
  const [persona, setPersona] = useState("");
  const graph = useResource<GraphData>(() => api.graph(60, persona), [persona, tick]);
  const stats = useResource<Stats>(api.stats, [tick]);

  const offline = isOffline(graph.error) && !graph.data;

  return (
    <div className="space-y-3">
      <SectionHead
        kicker="Style memory"
        title="The linkage"
        right={
          <button
            type="button"
            className="keycap keycap-sm"
            onClick={() => {
              graph.reload();
              stats.reload();
            }}
            aria-label="Reload graph"
          >
            ↻
          </button>
        }
      />

      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by persona">
        <span className="label mr-1">Filter</span>
        <button type="button" className="chip" aria-pressed={persona === ""} onClick={() => setPersona("")}>
          All
        </button>
        {personas.map((p) => (
          <button
            key={p.name}
            type="button"
            className="chip"
            aria-pressed={persona === p.name}
            onClick={() => setPersona(p.name)}
          >
            {p.name}
          </button>
        ))}
      </div>

      {offline ? (
        <>
          <GraphFrame>
            <EmptyLinkage caption="No connection to the style graph." />
          </GraphFrame>
          <OfflineNotice compact />
        </>
      ) : graph.error && !graph.data ? (
        <ErrorLine error={graph.error} />
      ) : graph.data && graph.data.nodes.length === 0 ? (
        <GraphFrame>
          <EmptyLinkage caption="The graph is empty. Feed it some of your writing first." />
        </GraphFrame>
      ) : graph.data ? (
        <ForceGraph data={graph.data} />
      ) : (
        <GraphFrame>
          <div className="flex h-full items-center justify-center">
            <Spinner label="loading graph" />
          </div>
        </GraphFrame>
      )}

      <Legend />
      <StatsLine stats={stats.data} error={stats.error} />
    </div>
  );
}

function GraphFrame({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="relative h-[300px] overflow-hidden rounded-[4px] border border-[var(--color-rule-strong)] bg-paper-hi"
      style={{
        backgroundImage:
          "linear-gradient(color-mix(in oklab, var(--color-rule) 45%, transparent) 1px, transparent 1px), linear-gradient(90deg, color-mix(in oklab, var(--color-rule) 45%, transparent) 1px, transparent 1px)",
        backgroundSize: "24px 24px",
        backgroundPosition: "-1px -1px",
        boxShadow: "inset 0 1px 4px rgb(0 0 0 / 0.08)",
      }}
    >
      {children}
    </div>
  );
}

function EmptyLinkage({ caption }: { caption: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-8 text-center">
      <svg width="120" height="60" viewBox="0 0 120 60" aria-hidden>
        <g fill="none" stroke="var(--color-ink-faint)" strokeWidth="1.4" strokeLinecap="round">
          <path d="M14 44 L52 18 L92 36" />
          <path d="M52 18 L60 50" strokeDasharray="1 3" />
        </g>
        <g fill="var(--color-paper-hi)" stroke="var(--color-ink-faint)" strokeWidth="1.4">
          <circle cx="14" cy="44" r="5" />
          <rect x="47" y="13" width="10" height="10" />
          <path d="M92 29 L99 36 L92 43 L85 36Z" />
          <circle cx="60" cy="50" r="3" />
        </g>
      </svg>
      <p className="font-display text-[14px] italic text-ink-soft">{caption}</p>
    </div>
  );
}

function ForceGraph({ data }: { data: GraphData }) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const [hover, setHover] = useState<{ node: SimNode; x: number; y: number; degree: number } | null>(null);

  const { nodes, links, degree } = useMemo(() => {
    const maxW = Math.max(1, ...data.nodes.map((n) => n.weight));
    const ids = new Set(data.nodes.map((n) => n.id));
    const nodes: SimNode[] = data.nodes.map((n) => ({
      ...n,
      r: 3.5 + Math.sqrt(Math.max(0, n.weight) / maxW) * 9 + (n.label === "IntentContext" ? 2 : 0),
    }));
    const valid = data.edges.filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target);
    const maxE = Math.max(1, ...valid.map((e) => e.weight));
    const links: SimLink[] = valid.map((e) => ({
      source: e.source,
      target: e.target,
      type: e.type,
      weight: e.weight,
      w: 0.6 + Math.sqrt(Math.max(0, e.weight) / maxE) * 3.2,
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
    const height = wrap.clientHeight || 300;

    const svg = select(svgEl).attr("viewBox", `${-width / 2} ${-height / 2} ${width} ${height}`);
    svg.selectAll("*").remove();
    const root = svg.append("g");

    const labelThreshold = [...nodes].sort((a, b) => b.weight - a.weight)[Math.min(11, nodes.length - 1)]?.weight ?? 0;

    const linkSel = root
      .append("g")
      .attr("fill", "none")
      .attr("stroke-linecap", "round")
      .selectAll<SVGPathElement, SimLink>("path")
      .data(links)
      .join("path")
      .style("stroke", (d) => EDGE_META[d.type].color)
      .style("stroke-opacity", (d) => EDGE_META[d.type].opacity)
      .attr("stroke-width", (d) => d.w)
      .attr("stroke-dasharray", (d) => EDGE_META[d.type].dash || null);

    // linkage pins at each end of a stroke
    const pinSel = root
      .append("g")
      .selectAll<SVGCircleElement, SimLink>("circle")
      .data(links.filter((l) => l.type === "FOLLOWED_BY"))
      .join("circle")
      .attr("r", (d) => Math.max(1.1, d.w * 0.45))
      .style("fill", "var(--color-paper-hi)")
      .style("stroke", "var(--color-ink)")
      .style("stroke-opacity", 0.5)
      .attr("stroke-width", 0.8);

    const nodeSel = root
      .append("g")
      .selectAll<SVGGElement, SimNode>("g")
      .data(nodes, (d) => d.id)
      .join("g")
      .style("cursor", "pointer");

    nodeSel
      .append("path")
      .attr("d", (d) => glyphPath(d.label, d.r))
      .style("fill", (d) =>
        d.label === "Word"
          ? "var(--color-ink)"
          : d.label === "PunctuationMark"
            ? "var(--color-ribbon)"
            : "var(--color-paper-hi)",
      )
      .style("stroke", (d) => (d.label === "PunctuationMark" ? "var(--color-ribbon)" : "var(--color-ink)"))
      .attr("stroke-width", (d) => (d.label === "Word" ? 0 : 1.4));

    // inner bore on intent nuts
    nodeSel
      .filter((d) => d.label === "IntentContext")
      .append("circle")
      .attr("r", (d) => d.r * 0.4)
      .style("fill", "none")
      .style("stroke", "var(--color-ink)")
      .attr("stroke-width", 1);

    // labels live in their own top layer so nodes never cover them
    const labelSel = root
      .append("g")
      .style("pointer-events", "none")
      .selectAll<SVGTextElement, SimNode>("text")
      .data(nodes.filter((d) => d.weight >= labelThreshold || d.label === "IntentContext"))
      .join("text")
      .text((d) => (d.name.length > 18 ? `${d.name.slice(0, 17)}…` : d.name))
      .attr("dx", (d) => d.r + 4)
      .attr("dy", 3.5)
      .style("font-family", "var(--font-mono)")
      .style("font-size", "9.5px")
      .style("fill", "var(--color-ink-soft)")
      .style("paint-order", "stroke")
      .style("stroke", "var(--color-paper-hi)")
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
          (l.source as SimNode).id === d.id || (l.target as SimNode).id === d.id ? 0.95 : 0.12,
        );
      })
      .on("mouseleave", () => {
        setHover(null);
        linkSel.style("stroke-opacity", (l) => EDGE_META[l.type].opacity);
      });

    const sim = forceSimulation<SimNode>(nodes)
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance((l) => (l.type === "FOLLOWED_BY" ? 34 : 52))
          .strength((l) => (l.type === "FOLLOWED_BY" ? 0.5 : 0.25)),
      )
      .force("charge", forceManyBody<SimNode>().strength((d) => -55 - d.r * 7))
      .force("collide", forceCollide<SimNode>().radius((d) => d.r + 3))
      .force("x", forceX(0).strength(0.06))
      .force("y", forceY(0).strength(0.09));

    // Ink-stroke edges: a gentle curve, like a pen arm sweeping between pivots.
    const render = () => {
      linkSel.attr("d", (l) => {
        const s = l.source as SimNode;
        const t = l.target as SimNode;
        const sx = s.x ?? 0,
          sy = s.y ?? 0,
          tx = t.x ?? 0,
          ty = t.y ?? 0;
        const mx = (sx + tx) / 2 + (ty - sy) * 0.12;
        const my = (sy + ty) / 2 - (tx - sx) * 0.12;
        return `M${sx},${sy}Q${mx},${my} ${tx},${ty}`;
      });
      pinSel.attr("cx", (l) => (l.target as SimNode).x ?? 0).attr("cy", (l) => (l.target as SimNode).y ?? 0);
      nodeSel.attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
      labelSel.attr("x", (d) => d.x ?? 0).attr("y", (d) => d.y ?? 0);
    };

    const fit = () => {
      if (!nodes.length) return;
      const xs = nodes.map((n) => n.x ?? 0);
      const ys = nodes.map((n) => n.y ?? 0);
      const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
      const k = Math.min(1.6, 0.85 / Math.max((x1 - x0 + 40) / width, (y1 - y0 + 40) / height));
      const cx = (x0 + x1) / 2;
      const cy = (y0 + y1) / 2;
      svg.call(zoomRef.current!.transform, zoomIdentity.scale(k).translate(-cx, -cy));
    };

    const z = zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.3, 5])
      .on("zoom", (e) => {
        root.attr("transform", e.transform.toString());
        setHover(null);
      });
    zoomRef.current = z;
    svg.call(z).on("dblclick.zoom", null);

    if (prefersReducedMotion()) {
      sim.stop();
      sim.tick(300);
      render();
      fit();
    } else {
      sim.on("tick", render);
      sim.on("end", fit);
      // fit once early so the first frames aren't off-screen
      setTimeout(fit, 450);
    }

    return () => {
      sim.stop();
      svg.on(".zoom", null);
    };
  }, [nodes, links, degree]);

  return (
    <GraphFrame>
      <div ref={wrapRef} className="absolute inset-0">
        <svg ref={svgRef} className="h-full w-full touch-none" role="img" aria-label={`Style graph with ${nodes.length} nodes`} />
        {hover && (
          <div
            className="pointer-events-none absolute z-10 min-w-[120px] max-w-[200px] rounded-[3px] border border-[var(--color-rule-strong)] bg-paper px-2 py-1.5 shadow-[0_4px_10px_-4px_rgb(0_0_0/0.35)]"
            style={{
              left: Math.min(hover.x + 12, (wrapRef.current?.clientWidth ?? 388) - 205),
              top: Math.max(4, hover.y - 52),
            }}
          >
            <div className="label text-[8.5px]">
              {LABEL_META[hover.node.label]?.glyph} {LABEL_META[hover.node.label]?.name ?? hover.node.label}
            </div>
            <div className="truncate font-display text-[15px] leading-tight text-ink">{hover.node.name}</div>
            <div className="mt-0.5 text-[10px] tabular-nums text-ink-soft">
              weight {fmt(hover.node.weight)} · {hover.degree} links
            </div>
          </div>
        )}
        <div className="pointer-events-auto absolute bottom-2 right-2 flex gap-1">
          <button
            type="button"
            className="keycap keycap-sm px-2"
            aria-label="Zoom in"
            onClick={() => svgRef.current && zoomRef.current && select(svgRef.current).call(zoomRef.current.scaleBy, 1.35)}
          >
            +
          </button>
          <button
            type="button"
            className="keycap keycap-sm px-2"
            aria-label="Zoom out"
            onClick={() => svgRef.current && zoomRef.current && select(svgRef.current).call(zoomRef.current.scaleBy, 1 / 1.35)}
          >
            −
          </button>
        </div>
      </div>
    </GraphFrame>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-ink-soft">
      {(Object.keys(LABEL_META) as NodeLabel[]).map((k) => (
        <span key={k} className="inline-flex items-center gap-1">
          <svg width="12" height="12" viewBox="-7 -7 14 14" aria-hidden>
            <path
              d={glyphPath(k, 4.5)}
              style={{
                fill:
                  k === "Word" ? "var(--color-ink)" : k === "PunctuationMark" ? "var(--color-ribbon)" : "var(--color-paper-hi)",
                stroke: k === "PunctuationMark" ? "var(--color-ribbon)" : "var(--color-ink)",
              }}
              strokeWidth={k === "Word" ? 0 : 1.2}
            />
          </svg>
          {LABEL_META[k].name}
        </span>
      ))}
      <span className="basis-full" aria-hidden />
      {(Object.keys(EDGE_META) as EdgeType[]).map((k) => (
        <span key={k} className="inline-flex items-center gap-1">
          <svg width="16" height="6" aria-hidden>
            <line
              x1="1"
              x2="15"
              y1="3"
              y2="3"
              style={{ stroke: EDGE_META[k].color }}
              strokeWidth="1.6"
              strokeDasharray={EDGE_META[k].dash || undefined}
              strokeLinecap="round"
            />
          </svg>
          {EDGE_META[k].name}
        </span>
      ))}
    </div>
  );
}

function StatsLine({ stats, error }: { stats: Stats | null; error: unknown }) {
  if (!stats) {
    return (
      <p className="label border-t hairline pt-2 normal-case tracking-normal">
        {error ? (isOffline(error) ? "stats unavailable — backend offline" : "stats unavailable") : "…"}
      </p>
    );
  }
  const items: [string, string][] = [
    ["words", stats.words.toLocaleString()],
    ["structures", stats.structures.toLocaleString()],
    ["samples", stats.samples.toLocaleString()],
    ["avg sentence", stats.avg_sentence_len == null ? "—" : `${stats.avg_sentence_len.toFixed(1)}w`],
  ];
  return (
    <dl className="grid grid-cols-4 gap-2 border-t hairline pt-2">
      {items.map(([k, v]) => (
        <div key={k}>
          <dd className="display text-[17px] leading-none tabular-nums text-ink">{v}</dd>
          <dt className="label mt-1 text-[8.5px] tracking-[0.1em]">{k}</dt>
        </div>
      ))}
    </dl>
  );
}

function fmt(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}
