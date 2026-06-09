import { Maximize2, Minus, Plus } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  WORK_PROJECT_ASSET_TYPE,
  WORK_PROJECT_ASSET_TYPES,
  WORK_PROJECT_GRAPH_EDGE_CATEGORIES,
  workProjectEdgeCategory,
  type WorkProjectGraphEdgeCategory,
} from "../../shared/api/contract";
import type { WorkProjectAsset, WorkProjectAssetType, WorkProjectGraphEdge } from "../../shared/api/types";
import {
  WORK_PROJECT_ASSET_ORIGIN_LABEL,
  WORK_PROJECT_ASSET_TYPE_LABEL,
  WORK_PROJECT_GRAPH_EDGE_CATEGORY_LABEL,
  WORK_PROJECT_GRAPH_EDGE_TYPE_LABEL,
} from "../../shared/lib/labels";
import { formatWorkProjectAsset } from "./workProjectView";

// Visual encoding. Keyed by the contract enums so a new asset type / edge category
// fails the build here until it is given a color, instead of silently falling back.
const ASSET_TYPE_COLOR: Record<WorkProjectAssetType, string> = {
  [WORK_PROJECT_ASSET_TYPE.SERVICE]: "#2f6fed",
  [WORK_PROJECT_ASSET_TYPE.DOMAIN]: "#0d9aa8",
  [WORK_PROJECT_ASSET_TYPE.NETWORK]: "#7c5cff",
  [WORK_PROJECT_ASSET_TYPE.BINARY]: "#e08a13",
};

const EDGE_CATEGORY_COLOR: Record<WorkProjectGraphEdgeCategory, string> = {
  structural: "#5b7ba6",
  offensive: "#d92d3a",
};

const VIEW_WIDTH = 960;
const VIEW_HEIGHT = 600;
const NODE_RADIUS = 8;
const MIN_SCALE = 0.3;
const MAX_SCALE = 4;
// Minimum center-to-center distance enforced after layout so related nodes never overlap.
const MIN_SEPARATION = 78;

type Point = { x: number; y: number };
type ViewTransform = { x: number; y: number; k: number };
type HoverTarget =
  | { kind: "node"; asset: WorkProjectAsset }
  | { kind: "edge"; edge: WorkProjectGraphEdge };
type HoverState = { target: HoverTarget; left: number; top: number };

export function ProjectGraphCanvas({ assets, edges }: { assets: WorkProjectAsset[]; edges: WorkProjectGraphEdge[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const assetById = useMemo(() => new Map(assets.map((asset) => [asset.id, asset])), [assets]);
  const visibleEdges = useMemo(
    () => edges.filter((edge) => assetById.has(edge.source_asset_id) && assetById.has(edge.target_asset_id)),
    [edges, assetById],
  );
  const layout = useMemo(() => computeLayout(assets, visibleEdges), [assets, visibleEdges]);

  const [positions, setPositions] = useState<Record<number, Point>>(layout);
  const [view, setView] = useState<ViewTransform>({ x: 0, y: 0, k: 1 });
  const [hover, setHover] = useState<HoverState | null>(null);

  useEffect(() => setPositions(layout), [layout]);

  // Translate a pointer event into the unscaled viewBox coordinate space.
  const toViewBox = useCallback((clientX: number, clientY: number): Point => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: ((clientX - rect.left) / rect.width) * VIEW_WIDTH,
      y: ((clientY - rect.top) / rect.height) * VIEW_HEIGHT,
    };
  }, []);

  const toContent = useCallback(
    (clientX: number, clientY: number): Point => {
      const local = toViewBox(clientX, clientY);
      return { x: (local.x - view.x) / view.k, y: (local.y - view.y) / view.k };
    },
    [toViewBox, view],
  );

  const containerPoint = useCallback((clientX: number, clientY: number): Point => {
    const rect = containerRef.current?.getBoundingClientRect();
    return { x: clientX - (rect?.left ?? 0), y: clientY - (rect?.top ?? 0) };
  }, []);

  const dragRef = useRef<{ id: number } | null>(null);
  const panRef = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null);

  const zoomAt = useCallback((clientX: number, clientY: number, factor: number) => {
    setView((current) => {
      const next = clamp(current.k * factor, MIN_SCALE, MAX_SCALE);
      const local = toViewBox(clientX, clientY);
      const cx = (local.x - current.x) / current.k;
      const cy = (local.y - current.y) / current.k;
      return { k: next, x: local.x - cx * next, y: local.y - cy * next };
    });
  }, [toViewBox]);

  // Native non-passive wheel listener so zoom can suppress page scrolling.
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      zoomAt(event.clientX, event.clientY, event.deltaY < 0 ? 1.12 : 1 / 1.12);
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const onNodePointerDown = (event: ReactPointerEvent, id: number) => {
    event.stopPropagation();
    dragRef.current = { id };
    setHover(null);
    svgRef.current?.setPointerCapture(event.pointerId);
  };

  const onBackgroundPointerDown = (event: ReactPointerEvent) => {
    panRef.current = { startX: event.clientX, startY: event.clientY, originX: view.x, originY: view.y };
    svgRef.current?.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: ReactPointerEvent) => {
    if (dragRef.current) {
      const point = toContent(event.clientX, event.clientY);
      const id = dragRef.current.id;
      setPositions((current) => ({ ...current, [id]: point }));
      return;
    }
    if (panRef.current) {
      const rect = svgRef.current?.getBoundingClientRect();
      if (!rect) return;
      const dx = ((event.clientX - panRef.current.startX) / rect.width) * VIEW_WIDTH;
      const dy = ((event.clientY - panRef.current.startY) / rect.height) * VIEW_HEIGHT;
      setView((current) => ({ ...current, x: panRef.current!.originX + dx, y: panRef.current!.originY + dy }));
    }
  };

  const endInteraction = (event: ReactPointerEvent) => {
    dragRef.current = null;
    panRef.current = null;
    if (svgRef.current?.hasPointerCapture(event.pointerId)) {
      svgRef.current.releasePointerCapture(event.pointerId);
    }
  };

  const moveHover = (event: ReactPointerEvent, target: HoverTarget) => {
    if (dragRef.current || panRef.current) return;
    const point = containerPoint(event.clientX, event.clientY);
    setHover({ target, left: point.x, top: point.y });
  };

  const zoomFromCenter = (factor: number) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
  };

  const resetView = () => setView({ x: 0, y: 0, k: 1 });

  return (
    <div className="project-graph" ref={containerRef}>
      <svg
        ref={svgRef}
        className="project-graph-canvas"
        viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
        role="img"
        aria-label="Work project relationship graph"
        onPointerDown={onBackgroundPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endInteraction}
        onPointerLeave={endInteraction}
      >
        <defs>
          {WORK_PROJECT_GRAPH_EDGE_CATEGORIES.map((category) => (
            <marker
              key={category}
              id={`graph-arrow-${category}`}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="10"
              markerHeight="10"
              markerUnits="userSpaceOnUse"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill={EDGE_CATEGORY_COLOR[category]} />
            </marker>
          ))}
        </defs>

        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          {visibleEdges.map((edge) => {
            const source = positions[edge.source_asset_id];
            const target = positions[edge.target_asset_id];
            if (!source || !target) return null;
            const category = workProjectEdgeCategory(edge.type);
            const end = retract(source, target, NODE_RADIUS + 1);
            const active = hover?.target.kind === "edge" && hover.target.edge.id === edge.id;
            return (
              <g key={edge.id}>
                <line
                  className="project-graph-edge"
                  stroke={EDGE_CATEGORY_COLOR[category]}
                  strokeWidth={active ? 3 : category === "offensive" ? 2 : 1.5}
                  x1={source.x}
                  y1={source.y}
                  x2={end.x}
                  y2={end.y}
                  markerEnd={`url(#graph-arrow-${category})`}
                />
                <line
                  className="project-graph-edge-hit"
                  x1={source.x}
                  y1={source.y}
                  x2={target.x}
                  y2={target.y}
                  onPointerEnter={(event) => moveHover(event, { kind: "edge", edge })}
                  onPointerMove={(event) => moveHover(event, { kind: "edge", edge })}
                  onPointerLeave={() => setHover(null)}
                />
              </g>
            );
          })}

          {assets.map((asset) => {
            const point = positions[asset.id];
            if (!point) return null;
            const active = hover?.target.kind === "node" && hover.target.asset.id === asset.id;
            return (
              <g key={asset.id} transform={`translate(${point.x} ${point.y})`} className="project-graph-node">
                <circle
                  r={NODE_RADIUS}
                  fill={ASSET_TYPE_COLOR[asset.type]}
                  strokeWidth={active ? 3 : 2}
                  onPointerDown={(event) => onNodePointerDown(event, asset.id)}
                  onPointerEnter={(event) => moveHover(event, { kind: "node", asset })}
                  onPointerMove={(event) => moveHover(event, { kind: "node", asset })}
                  onPointerLeave={() => setHover(null)}
                />
                <text y={NODE_RADIUS + 13}>{truncate(formatWorkProjectAsset(asset), 22)}</text>
              </g>
            );
          })}
        </g>
      </svg>

      <GraphLegend />

      <div className="project-graph-controls">
        <button type="button" aria-label="Zoom in" onClick={() => zoomFromCenter(1.2)}>
          <Plus size={15} />
        </button>
        <button type="button" aria-label="Zoom out" onClick={() => zoomFromCenter(1 / 1.2)}>
          <Minus size={15} />
        </button>
        <button type="button" aria-label="Reset view" onClick={resetView}>
          <Maximize2 size={14} />
        </button>
      </div>

      {hover ? <GraphTooltip hover={hover} assetById={assetById} /> : null}
    </div>
  );
}

function GraphLegend() {
  return (
    <div className="project-graph-legend">
      <div className="project-graph-legend-group">
        <span className="project-graph-legend-title">Nodes</span>
        {WORK_PROJECT_ASSET_TYPES.map((type) => (
          <span key={type} className="project-graph-legend-item">
            <i className="project-graph-legend-dot" style={{ background: ASSET_TYPE_COLOR[type] }} />
            {WORK_PROJECT_ASSET_TYPE_LABEL[type]}
          </span>
        ))}
      </div>
      <div className="project-graph-legend-group">
        <span className="project-graph-legend-title">Edges</span>
        {WORK_PROJECT_GRAPH_EDGE_CATEGORIES.map((category) => (
          <span key={category} className="project-graph-legend-item">
            <i className="project-graph-legend-line" style={{ background: EDGE_CATEGORY_COLOR[category] }} />
            {WORK_PROJECT_GRAPH_EDGE_CATEGORY_LABEL[category]}
          </span>
        ))}
      </div>
    </div>
  );
}

function GraphTooltip({ hover, assetById }: { hover: HoverState; assetById: Map<number, WorkProjectAsset> }) {
  const rows = hover.target.kind === "node"
    ? nodeRows(hover.target.asset)
    : edgeRows(hover.target.edge, assetById);
  return (
    <div className="project-graph-tooltip" style={{ left: hover.left, top: hover.top }}>
      <strong>{rows.title}</strong>
      <dl>
        {rows.items.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function nodeRows(asset: WorkProjectAsset): { title: string; items: Array<[string, string]> } {
  const items: Array<[string, string | undefined]> = [
    ["Type", WORK_PROJECT_ASSET_TYPE_LABEL[asset.type]],
    ["Origin", WORK_PROJECT_ASSET_ORIGIN_LABEL[asset.origin]],
    asset.type === WORK_PROJECT_ASSET_TYPE.BINARY ? ["Path", asset.path] : ["Host", asset.host],
    ["Port", asset.port ? String(asset.port) : undefined],
    ["Banner", asset.extra?.banner],
  ];
  return { title: formatWorkProjectAsset(asset), items: keepFilled(items) };
}

function edgeRows(edge: WorkProjectGraphEdge, assetById: Map<number, WorkProjectAsset>): { title: string; items: Array<[string, string]> } {
  const source = assetById.get(edge.source_asset_id);
  const target = assetById.get(edge.target_asset_id);
  const items: Array<[string, string | undefined]> = [
    ["Category", WORK_PROJECT_GRAPH_EDGE_CATEGORY_LABEL[workProjectEdgeCategory(edge.type)]],
    ["From", source ? formatWorkProjectAsset(source) : `#${edge.source_asset_id}`],
    ["To", target ? formatWorkProjectAsset(target) : `#${edge.target_asset_id}`],
    ["Label", edge.label],
  ];
  return { title: WORK_PROJECT_GRAPH_EDGE_TYPE_LABEL[edge.type], items: keepFilled(items) };
}

function keepFilled(items: Array<[string, string | undefined]>): Array<[string, string]> {
  return items.filter((item): item is [string, string] => Boolean(item[1]));
}

// Deterministic Fruchterman-Reingold layout so the same graph always renders the same shape.
function computeLayout(assets: WorkProjectAsset[], edges: WorkProjectGraphEdge[]): Record<number, Point> {
  const count = assets.length;
  const positions: Record<number, Point> = {};
  assets.forEach((asset, index) => {
    const angle = (2 * Math.PI * index) / Math.max(count, 1);
    positions[asset.id] = {
      x: VIEW_WIDTH / 2 + Math.cos(angle) * VIEW_WIDTH * 0.3,
      y: VIEW_HEIGHT / 2 + Math.sin(angle) * VIEW_HEIGHT * 0.3,
    };
  });
  if (count <= 1) return positions;

  const ideal = Math.sqrt((VIEW_WIDTH * VIEW_HEIGHT) / count);
  const iterations = count > 120 ? 120 : 300;
  let temperature = VIEW_WIDTH / 10;

  for (let step = 0; step < iterations; step += 1) {
    const displacement: Record<number, Point> = {};
    for (const asset of assets) displacement[asset.id] = { x: 0, y: 0 };

    for (let i = 0; i < count; i += 1) {
      for (let j = i + 1; j < count; j += 1) {
        const a = positions[assets[i].id];
        const b = positions[assets[j].id];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distance = Math.hypot(dx, dy) || 0.01;
        const force = (ideal * ideal) / distance;
        const ux = (dx / distance) * force;
        const uy = (dy / distance) * force;
        displacement[assets[i].id].x += ux;
        displacement[assets[i].id].y += uy;
        displacement[assets[j].id].x -= ux;
        displacement[assets[j].id].y -= uy;
      }
    }

    for (const edge of edges) {
      const a = positions[edge.source_asset_id];
      const b = positions[edge.target_asset_id];
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const distance = Math.hypot(dx, dy) || 0.01;
      const force = (distance * distance) / ideal;
      const ux = (dx / distance) * force;
      const uy = (dy / distance) * force;
      displacement[edge.source_asset_id].x -= ux;
      displacement[edge.source_asset_id].y -= uy;
      displacement[edge.target_asset_id].x += ux;
      displacement[edge.target_asset_id].y += uy;
    }

    for (const asset of assets) {
      const move = displacement[asset.id];
      const length = Math.hypot(move.x, move.y) || 0.01;
      const limited = Math.min(length, temperature);
      positions[asset.id].x += (move.x / length) * limited;
      positions[asset.id].y += (move.y / length) * limited;
    }
    temperature *= 0.95;
  }

  const placed = normalize(positions, assets);
  separate(placed, assets);
  return placed;
}

// Push apart any node pair closer than MIN_SEPARATION so densely linked clusters stay readable.
function separate(positions: Record<number, Point>, assets: WorkProjectAsset[]): void {
  for (let pass = 0; pass < 200; pass += 1) {
    let moved = false;
    for (let i = 0; i < assets.length; i += 1) {
      for (let j = i + 1; j < assets.length; j += 1) {
        const a = positions[assets[i].id];
        const b = positions[assets[j].id];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let distance = Math.hypot(dx, dy);
        if (distance === 0) {
          // Deterministically nudge coincident nodes apart (no randomness, stable layout).
          dx = i - j - 1;
          dy = 1;
          distance = Math.hypot(dx, dy);
        }
        if (distance >= MIN_SEPARATION) continue;
        const shift = (MIN_SEPARATION - distance) / 2;
        const ux = (dx / distance) * shift;
        const uy = (dy / distance) * shift;
        a.x -= ux;
        a.y -= uy;
        b.x += ux;
        b.y += uy;
        moved = true;
      }
    }
    if (!moved) break;
  }
}

function normalize(positions: Record<number, Point>, assets: WorkProjectAsset[]): Record<number, Point> {
  const padding = 70;
  const xs = assets.map((asset) => positions[asset.id].x);
  const ys = assets.map((asset) => positions[asset.id].y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = maxX - minX || 1;
  const spanY = maxY - minY || 1;
  const scale = Math.min((VIEW_WIDTH - padding * 2) / spanX, (VIEW_HEIGHT - padding * 2) / spanY);
  const offsetX = (VIEW_WIDTH - spanX * scale) / 2;
  const offsetY = (VIEW_HEIGHT - spanY * scale) / 2;
  const result: Record<number, Point> = {};
  for (const asset of assets) {
    result[asset.id] = {
      x: offsetX + (positions[asset.id].x - minX) * scale,
      y: offsetY + (positions[asset.id].y - minY) * scale,
    };
  }
  return result;
}

function retract(source: Point, target: Point, distance: number): Point {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const length = Math.hypot(dx, dy) || 1;
  return { x: target.x - (dx / length) * distance, y: target.y - (dy / length) * distance };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function truncate(value: string, max: number): string {
  return value.length <= max ? value : `${value.slice(0, max - 1)}…`;
}
