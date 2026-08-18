import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getLearningJourney } from "./api";
import type { JourneyEdge, JourneyGraph, JourneyNode } from "./types";

// ─── Layout Engine ───────────────────────────────────────────────────────────

interface LayoutNode {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  kind: string;
  label: string;
  data: JourneyNode;
}

interface LayoutEdge {
  source: string;
  target: string;
  relation: string;
}

/**
 * Simple force-directed layout: repulsion between all nodes, attraction on
 * edges, gravity toward center. Iterates synchronously for a fixed number of
 * ticks to produce a stable layout without animation overhead.
 */
function computeLayout(
  graphNodes: JourneyNode[],
  graphEdges: JourneyEdge[],
  width: number,
  height: number,
): { nodes: LayoutNode[]; edges: LayoutEdge[] } {
  const nodes: LayoutNode[] = graphNodes.map((n, i) => {
    // Seed positions in a circle to avoid initial overlap
    const angle = (2 * Math.PI * i) / graphNodes.length;
    const radius = Math.min(width, height) * 0.3;
    return {
      id: n.id,
      x: width / 2 + radius * Math.cos(angle),
      y: height / 2 + radius * Math.sin(angle),
      vx: 0,
      vy: 0,
      kind: n.kind,
      label: n.label,
      data: n,
    };
  });

  const edges: LayoutEdge[] = graphEdges.map((e) => ({
    source: e.source,
    target: e.target,
    relation: e.relation,
  }));

  const nodeMap = new Map<string, LayoutNode>();
  for (const n of nodes) nodeMap.set(n.id, n);

  // Simulation parameters
  const iterations = 120;
  const repulsion = 8000;
  const attraction = 0.005;
  const gravity = 0.02;
  const damping = 0.85;
  const minDist = 60;

  for (let tick = 0; tick < iterations; tick++) {
    // Repulsion (all pairs)
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < minDist) dist = minDist;
        const force = repulsion / (dist * dist);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx -= fx;
        a.vy -= fy;
        b.vx += fx;
        b.vy += fy;
      }
    }

    // Attraction (edges)
    for (const edge of edges) {
      const source = nodeMap.get(edge.source);
      const target = nodeMap.get(edge.target);
      if (!source || !target) continue;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist === 0) continue;
      const force = dist * attraction;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      source.vx += fx;
      source.vy += fy;
      target.vx -= fx;
      target.vy -= fy;
    }

    // Gravity toward center
    for (const node of nodes) {
      node.vx += (width / 2 - node.x) * gravity;
      node.vy += (height / 2 - node.y) * gravity;
    }

    // Apply velocities with damping
    for (const node of nodes) {
      node.vx *= damping;
      node.vy *= damping;
      node.x += node.vx;
      node.y += node.vy;
      // Clamp within bounds
      node.x = Math.max(40, Math.min(width - 40, node.x));
      node.y = Math.max(40, Math.min(height - 40, node.y));
    }
  }

  return { nodes, edges };
}

// ─── Node styling by kind ────────────────────────────────────────────────────

const NODE_STYLES: Record<string, { fill: string; stroke: string; radius: number; icon: string }> = {
  skill: { fill: "var(--journey-skill-fill, #7c3aed)", stroke: "var(--journey-skill-stroke, #a78bfa)", radius: 28, icon: "⊙" },
  revision: { fill: "var(--journey-revision-fill, #2563eb)", stroke: "var(--journey-revision-stroke, #60a5fa)", radius: 20, icon: "↻" },
  proposal: { fill: "var(--journey-proposal-fill, #d97706)", stroke: "var(--journey-proposal-stroke, #fbbf24)", radius: 22, icon: "◆" },
  run: { fill: "var(--journey-run-fill, #059669)", stroke: "var(--journey-run-stroke, #34d399)", radius: 18, icon: "▸" },
};

const EDGE_COLORS: Record<string, string> = {
  produces: "var(--journey-edge-produces, #a78bfa)",
  evolves_to: "var(--journey-edge-evolves, #60a5fa)",
  triggers_creation: "var(--journey-edge-trigger, #34d399)",
  triggers_improvement: "var(--journey-edge-improve, #fbbf24)",
  approved_as: "var(--journey-edge-approve, #a78bfa)",
  generates_proposal: "var(--journey-edge-proposal, #fbbf24)",
  targets: "var(--journey-edge-target, #d97706)",
  used_in: "var(--journey-edge-use, #34d399)",
  validates: "var(--journey-edge-validate, #10b981)",
  fails_with: "var(--journey-edge-fail, #ef4444)",
  corrects: "var(--journey-edge-correct, #f59e0b)",
  proposes_change: "var(--journey-edge-change, #f59e0b)",
};

// ─── Component ───────────────────────────────────────────────────────────────

export function JourneyGraphView() {
  const [graph, setGraph] = useState<JourneyGraph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedNode, setSelectedNode] = useState<LayoutNode | null>(null);
  const [filterKind, setFilterKind] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    getLearningJourney()
      .then((data) => { if (!cancelled) { setGraph(data); setLoading(false); } })
      .catch((err) => { if (!cancelled) { setError(err instanceof Error ? err.message : "Failed to load journey"); setLoading(false); } });
    return () => { cancelled = true; };
  }, []);

  if (loading) return <div className="workspace-loading">Loading journey graph...</div>;
  if (error) return <div className="inline-error" role="alert">{error}</div>;
  if (!graph || graph.nodes.length === 0) {
    return (
      <div className="empty-state">
        <span className="empty-icon">⟡</span>
        <strong>No journey data yet</strong>
        <span>As skills are learned, used, and improved, the provenance graph will grow here.</span>
      </div>
    );
  }

  return (
    <div className="journey-container">
      <div className="journey-toolbar">
        <div className="journey-filters">
          {["", "skill", "revision", "proposal", "run"].map((kind) => (
            <button
              key={kind || "all"}
              className={`filter ${filterKind === kind ? "active" : ""}`}
              onClick={() => { setFilterKind(kind); setSelectedNode(null); }}
            >
              {kind || "All"}
            </button>
          ))}
        </div>
        <div className="journey-stats">
          <span>{graph.stats.total_nodes} nodes</span>
          <span>{graph.stats.total_edges} edges</span>
        </div>
      </div>
      <div className="journey-graph-area">
        <GraphCanvas
          graph={graph}
          filterKind={filterKind}
          selectedNode={selectedNode}
          onSelectNode={setSelectedNode}
        />
        {selectedNode && (
          <NodeDetail node={selectedNode} onClose={() => setSelectedNode(null)} />
        )}
      </div>
      <JourneyLegend />
    </div>
  );
}

// ─── SVG Canvas with zoom/pan ────────────────────────────────────────────────

interface GraphCanvasProps {
  graph: JourneyGraph;
  filterKind: string;
  selectedNode: LayoutNode | null;
  onSelectNode: (node: LayoutNode | null) => void;
}

function GraphCanvas({ graph, filterKind, selectedNode, onSelectNode }: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 900, height: 560 });
  const [viewBox, setViewBox] = useState({ x: 0, y: 0, w: 900, h: 560 });
  const [isPanning, setIsPanning] = useState(false);
  const panStart = useRef({ x: 0, y: 0, vx: 0, vy: 0 });

  // Resize observer
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) {
        const { width, height } = entry.contentRect;
        setDimensions({ width: Math.max(400, width), height: Math.max(300, height) });
      }
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // Compute layout
  const layout = useMemo(() => {
    const filteredNodes = filterKind
      ? graph.nodes.filter((n) => n.kind === filterKind)
      : graph.nodes;
    const nodeIds = new Set(filteredNodes.map((n) => n.id));
    const filteredEdges = graph.edges.filter(
      (e) => nodeIds.has(e.source) && nodeIds.has(e.target)
    );
    return computeLayout(filteredNodes, filteredEdges, dimensions.width, dimensions.height);
  }, [graph, filterKind, dimensions]);

  // Reset viewBox when dimensions/filter change
  useEffect(() => {
    setViewBox({ x: 0, y: 0, w: dimensions.width, h: dimensions.height });
  }, [dimensions, filterKind]);

  // Zoom
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const scale = e.deltaY > 0 ? 1.1 : 0.9;
    const svg = e.currentTarget as SVGSVGElement;
    const rect = svg.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * viewBox.w + viewBox.x;
    const my = ((e.clientY - rect.top) / rect.height) * viewBox.h + viewBox.y;

    const newW = viewBox.w * scale;
    const newH = viewBox.h * scale;
    const newX = mx - (mx - viewBox.x) * scale;
    const newY = my - (my - viewBox.y) * scale;
    setViewBox({ x: newX, y: newY, w: newW, h: newH });
  }, [viewBox]);

  // Pan
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    setIsPanning(true);
    panStart.current = { x: e.clientX, y: e.clientY, vx: viewBox.x, vy: viewBox.y };
  }, [viewBox]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning) return;
    const svg = e.currentTarget as SVGSVGElement;
    const rect = svg.getBoundingClientRect();
    const dx = ((e.clientX - panStart.current.x) / rect.width) * viewBox.w;
    const dy = ((e.clientY - panStart.current.y) / rect.height) * viewBox.h;
    setViewBox({ ...viewBox, x: panStart.current.vx - dx, y: panStart.current.vy - dy });
  }, [isPanning, viewBox]);

  const handleMouseUp = useCallback(() => {
    setIsPanning(false);
  }, []);

  // Touch handlers for mobile
  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    if (e.touches.length === 1) {
      const touch = e.touches[0];
      setIsPanning(true);
      panStart.current = { x: touch.clientX, y: touch.clientY, vx: viewBox.x, vy: viewBox.y };
    }
  }, [viewBox]);

  const handleTouchMove = useCallback((e: React.TouchEvent) => {
    if (!isPanning || e.touches.length !== 1) return;
    const touch = e.touches[0];
    const svg = e.currentTarget as SVGSVGElement;
    const rect = svg.getBoundingClientRect();
    const dx = ((touch.clientX - panStart.current.x) / rect.width) * viewBox.w;
    const dy = ((touch.clientY - panStart.current.y) / rect.height) * viewBox.h;
    setViewBox({ ...viewBox, x: panStart.current.vx - dx, y: panStart.current.vy - dy });
  }, [isPanning, viewBox]);

  const handleTouchEnd = useCallback(() => setIsPanning(false), []);

  // Build a map for quick lookups
  const nodeMap = useMemo(() => {
    const map = new Map<string, LayoutNode>();
    for (const n of layout.nodes) map.set(n.id, n);
    return map;
  }, [layout]);

  // Zoom controls
  const zoomIn = () => {
    const cx = viewBox.x + viewBox.w / 2;
    const cy = viewBox.y + viewBox.h / 2;
    const newW = viewBox.w * 0.8;
    const newH = viewBox.h * 0.8;
    setViewBox({ x: cx - newW / 2, y: cy - newH / 2, w: newW, h: newH });
  };
  const zoomOut = () => {
    const cx = viewBox.x + viewBox.w / 2;
    const cy = viewBox.y + viewBox.h / 2;
    const newW = viewBox.w * 1.25;
    const newH = viewBox.h * 1.25;
    setViewBox({ x: cx - newW / 2, y: cy - newH / 2, w: newW, h: newH });
  };
  const resetView = () => {
    setViewBox({ x: 0, y: 0, w: dimensions.width, h: dimensions.height });
  };

  return (
    <div className="journey-canvas-wrapper" ref={containerRef}>
      <div className="journey-zoom-controls">
        <button onClick={zoomIn} aria-label="Zoom in" title="Zoom in">+</button>
        <button onClick={zoomOut} aria-label="Zoom out" title="Zoom out">−</button>
        <button onClick={resetView} aria-label="Reset view" title="Reset view">⟲</button>
      </div>
      <svg
        className="journey-svg"
        viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
        width="100%"
        height="100%"
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
        style={{ cursor: isPanning ? "grabbing" : "grab" }}
      >
        <defs>
          <marker
            id="arrow"
            viewBox="0 0 10 10"
            refX="10"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 Z" fill="var(--journey-arrow, #6b7280)" />
          </marker>
        </defs>

        {/* Edges */}
        {layout.edges.map((edge, i) => {
          const source = nodeMap.get(edge.source);
          const target = nodeMap.get(edge.target);
          if (!source || !target) return null;
          const color = EDGE_COLORS[edge.relation] ?? "var(--journey-edge-default, #6b7280)";
          const isHighlighted = selectedNode && (selectedNode.id === edge.source || selectedNode.id === edge.target);
          const opacity = selectedNode ? (isHighlighted ? 1 : 0.15) : 0.6;

          // Offset endpoint by target radius to avoid overlap
          const targetStyle = NODE_STYLES[target.kind] ?? NODE_STYLES.run;
          const dx = target.x - source.x;
          const dy = target.y - source.y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          const endX = dist > 0 ? target.x - (dx / dist) * (targetStyle.radius + 4) : target.x;
          const endY = dist > 0 ? target.y - (dy / dist) * (targetStyle.radius + 4) : target.y;

          return (
            <line
              key={`edge-${i}`}
              x1={source.x}
              y1={source.y}
              x2={endX}
              y2={endY}
              stroke={color}
              strokeWidth={isHighlighted ? 2.5 : 1.5}
              opacity={opacity}
              markerEnd="url(#arrow)"
            />
          );
        })}

        {/* Nodes */}
        {layout.nodes.map((node) => {
          const style = NODE_STYLES[node.kind] ?? NODE_STYLES.run;
          const isSelected = selectedNode?.id === node.id;
          const isConnected = selectedNode && layout.edges.some(
            (e) => (e.source === selectedNode.id && e.target === node.id) ||
                   (e.target === selectedNode.id && e.source === node.id)
          );
          const opacity = selectedNode ? (isSelected || isConnected ? 1 : 0.25) : 1;

          return (
            <g
              key={node.id}
              className="journey-node"
              onClick={(e) => { e.stopPropagation(); onSelectNode(isSelected ? null : node); }}
              style={{ cursor: "pointer", opacity }}
              role="button"
              aria-label={`${node.kind}: ${node.label}`}
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelectNode(isSelected ? null : node); } }}
            >
              <circle
                cx={node.x}
                cy={node.y}
                r={style.radius}
                fill={style.fill}
                stroke={isSelected ? "var(--journey-selected, #fff)" : style.stroke}
                strokeWidth={isSelected ? 3 : 1.5}
              />
              <text
                x={node.x}
                y={node.y + 1}
                textAnchor="middle"
                dominantBaseline="central"
                className="journey-node-icon"
                fill="white"
                fontSize={style.radius * 0.7}
                pointerEvents="none"
              >
                {style.icon}
              </text>
              <text
                x={node.x}
                y={node.y + style.radius + 14}
                textAnchor="middle"
                className="journey-node-label"
                fill="var(--journey-label, #e5e7eb)"
                fontSize="11"
                pointerEvents="none"
              >
                {node.label.length > 18 ? node.label.slice(0, 16) + "…" : node.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ─── Node Detail Panel ───────────────────────────────────────────────────────

function NodeDetail({ node, onClose }: { node: LayoutNode; onClose: () => void }) {
  const style = NODE_STYLES[node.kind] ?? NODE_STYLES.run;
  const data = node.data;

  return (
    <aside className="journey-detail-panel" aria-label="Node details">
      <div className="journey-detail-header">
        <span className="journey-detail-icon" style={{ background: style.fill }}>{style.icon}</span>
        <div>
          <strong>{data.label}</strong>
          <span className="journey-detail-kind">{data.kind}</span>
        </div>
        <button className="journey-detail-close" onClick={onClose} aria-label="Close details">✕</button>
      </div>
      <div className="journey-detail-body">
        {data.description && <DetailRow label="Description" value={data.description} />}
        {data.state && <DetailRow label="State" value={data.state} />}
        {data.operation && <DetailRow label="Operation" value={data.operation} />}
        {data.status && <DetailRow label="Status" value={data.status} />}
        {data.reason && <DetailRow label="Reason" value={data.reason} />}
        {data.confidence !== undefined && <DetailRow label="Confidence" value={`${(data.confidence * 100).toFixed(0)}%`} />}
        {data.revision !== undefined && <DetailRow label="Revision" value={String(data.revision)} />}
        {data.owner && <DetailRow label="Owner" value={data.owner} />}
        {data.trust && <DetailRow label="Trust" value={data.trust} />}
        {data.run_id && <DetailRow label="Run ID" value={data.run_id.slice(0, 12)} />}
        {data.skill_name && <DetailRow label="Skill" value={data.skill_name} />}
        {data.created_at && <DetailRow label="Created" value={formatTimestamp(data.created_at)} />}
        {data.resolved_at && <DetailRow label="Resolved" value={formatTimestamp(data.resolved_at)} />}
      </div>
    </aside>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="journey-detail-row">
      <span className="journey-detail-label">{label}</span>
      <span className="journey-detail-value">{value}</span>
    </div>
  );
}

// ─── Legend ───────────────────────────────────────────────────────────────────

function JourneyLegend() {
  return (
    <div className="journey-legend" aria-label="Graph legend">
      <span className="journey-legend-title">Legend</span>
      <div className="journey-legend-items">
        {Object.entries(NODE_STYLES).map(([kind, style]) => (
          <span key={kind} className="journey-legend-item">
            <span className="journey-legend-dot" style={{ background: style.fill }} />
            {kind}
          </span>
        ))}
      </div>
      <div className="journey-legend-edges">
        <span className="journey-legend-edge"><span style={{ color: "var(--journey-edge-trigger, #34d399)" }}>→</span> creates/triggers</span>
        <span className="journey-legend-edge"><span style={{ color: "var(--journey-edge-improve, #fbbf24)" }}>→</span> improves</span>
        <span className="journey-legend-edge"><span style={{ color: "var(--journey-edge-use, #34d399)" }}>→</span> uses</span>
        <span className="journey-legend-edge"><span style={{ color: "var(--journey-edge-fail, #ef4444)" }}>→</span> fails/corrects</span>
      </div>
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}
