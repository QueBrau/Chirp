/**
 * Pure SVG geometry for the chart primitives (DESIGN §11).
 *
 * NOTHING in here imports React or react-native. That is deliberate: arc maths is
 * the part of a chart that silently renders a wrong picture rather than throwing,
 * so it is kept callable from a plain node script and verified on its own, away
 * from a device and away from the app's data.
 *
 * Angles are degrees, clockwise, 0 = 12 o'clock — the direction a reader expects a
 * donut to start and travel.
 */

/** A point in SVG user space. */
export interface Point {
  x: number;
  y: number;
}

export interface DonutSegment {
  /** SVG path `d` for this ring segment. */
  d: string;
  /** Share of the total, 0..1 — the caller labels with this, never with the angle. */
  fraction: number;
  /** Mid-angle, for anchoring a leader line or label if one is ever added. */
  midDeg: number;
}

export interface DonutOptions {
  /** Overall square box the ring is drawn in. */
  size: number;
  /** Ring thickness; inner radius is derived, so the hole scales with the box. */
  thickness: number;
  /**
   * Gap between touching segments, in PIXELS rather than degrees. The spec calls
   * for a constant 2px of surface showing between marks; a constant angular gap
   * would render thinner on a small ring and fatter on a big one.
   */
  gapPx?: number;
}

/**
 * Smallest sweep a segment is allowed to draw at, in degrees.
 *
 * A category worth 0.1% would otherwise subtract to a negative sweep once the gap
 * is taken out and vanish — which reads as "this category does not exist" rather
 * than "this category is tiny". A sliver is the honest rendering. The value is
 * still exact in the legend, so nothing depends on reading this angle.
 */
const MIN_SWEEP_DEG = 0.75;

/** Below this, a single segment is drawn as a closed ring instead of an arc. */
const FULL_RING_DEG = 359.9;

function polar(cx: number, cy: number, r: number, deg: number): Point {
  const rad = ((deg - 90) * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function round(n: number): number {
  // Two decimals is well under a device pixel and keeps the path strings short
  // enough to read in a diff when one of these is wrong.
  return Math.round(n * 100) / 100;
}

/**
 * A closed ring (two concentric circles). Used when one category is 100% of the
 * total: an arc whose start and end angles are equal is degenerate and renders as
 * nothing at all, which is the classic "my pie chart is blank" bug.
 *
 * Consumers must set fillRule="evenodd" so the inner circle punches the hole.
 */
function fullRingPath(cx: number, cy: number, rOuter: number, rInner: number): string {
  const o = (r: number, sweep: 0 | 1): string =>
    `M ${round(cx)} ${round(cy - r)} ` +
    `A ${round(r)} ${round(r)} 0 1 ${sweep} ${round(cx)} ${round(cy + r)} ` +
    `A ${round(r)} ${round(r)} 0 1 ${sweep} ${round(cx)} ${round(cy - r)} Z`;
  return `${o(rOuter, 1)} ${o(rInner, 0)}`;
}

/**
 * Ring segments for `values`, in the order given.
 *
 * Order is the caller's responsibility and must be STABLE per category — colour
 * follows the entity, never its rank, so sorting by size here would repaint every
 * slice the moment a number changed.
 *
 * Non-positive values are dropped rather than drawn: a ledger category that nets
 * to zero has no share of the whole, and a zero-width wedge with a gap either side
 * renders as a stray notch.
 */
export function donutSegments(values: number[], options: DonutOptions): DonutSegment[] {
  const { size, thickness, gapPx = 2 } = options;
  const cx = size / 2;
  const cy = size / 2;
  const rOuter = size / 2;
  const rInner = Math.max(0, rOuter - thickness);

  const usable = values.filter((v) => v > 0);
  const total = usable.reduce((sum, v) => sum + v, 0);
  if (total <= 0) return [];

  // The gap is specified in pixels at the ring's MID radius, so the visible band
  // of surface is even through the thickness of the ring.
  const rMid = (rOuter + rInner) / 2;
  const gapDeg = rMid > 0 ? (gapPx / (2 * Math.PI * rMid)) * 360 : 0;
  const single = usable.length === 1;

  const segments: DonutSegment[] = [];
  let cursor = 0;
  for (const value of usable) {
    const fraction = value / total;
    const sweep = fraction * 360;

    if (single && sweep >= FULL_RING_DEG) {
      segments.push({ d: fullRingPath(cx, cy, rOuter, rInner), fraction, midDeg: 180 });
      break;
    }

    const drawn = Math.max(sweep - gapDeg, MIN_SWEEP_DEG);
    const start = cursor + (sweep - drawn) / 2;
    const end = start + drawn;

    const p0 = polar(cx, cy, rOuter, start);
    const p1 = polar(cx, cy, rOuter, end);
    const p2 = polar(cx, cy, rInner, end);
    const p3 = polar(cx, cy, rInner, start);
    const largeArc = drawn > 180 ? 1 : 0;

    segments.push({
      d:
        `M ${round(p0.x)} ${round(p0.y)} ` +
        `A ${round(rOuter)} ${round(rOuter)} 0 ${largeArc} 1 ${round(p1.x)} ${round(p1.y)} ` +
        `L ${round(p2.x)} ${round(p2.y)} ` +
        `A ${round(rInner)} ${round(rInner)} 0 ${largeArc} 0 ${round(p3.x)} ${round(p3.y)} Z`,
      fraction,
      midDeg: cursor + sweep / 2,
    });
    cursor += sweep;
  }
  return segments;
}

export interface TrendOptions {
  width: number;
  height: number;
  /** Room for the end-dot and its surface ring so neither is clipped at the edge. */
  inset?: number;
}

export interface TrendGeometry {
  /** `d` for the 2px stroked line. */
  line: string;
  /** `d` for the filled wash beneath it, closed onto the baseline. */
  area: string;
  /** Where to park the end-dot; null when there was nothing to plot. */
  last: Point | null;
  /** Y in user space of the value the area closes onto. */
  baselineY: number;
  /** Y in user space of zero, or null when zero is outside the plotted range. */
  zeroY: number | null;
  min: number;
  max: number;
}

/**
 * Line + area geometry for a single series.
 *
 * THE AREA CLOSES ONTO ZERO WHEN ZERO IS IN RANGE, not onto the bottom of the box.
 * A chapter that overspends has a negative balance, and an area filled to the floor
 * would shade the deficit exactly like a surplus — the one thing this chart exists
 * to distinguish. When the whole series sits on one side of zero the fill closes on
 * the nearer edge, which is the ordinary case and reads as a normal area chart.
 */
export function trendGeometry(points: Point[], options: TrendOptions): TrendGeometry {
  const { width, height, inset = 0 } = options;
  const empty: TrendGeometry = {
    line: "",
    area: "",
    last: null,
    baselineY: height,
    zeroY: null,
    min: 0,
    max: 0,
  };
  if (points.length === 0) return empty;

  const top = inset;
  const bottom = height - inset;
  const left = inset;
  const right = width - inset;

  const ys = points.map((p) => p.y);
  let min = Math.min(...ys);
  let max = Math.max(...ys);
  if (min === max) {
    // A flat series still deserves a line rather than a divide-by-zero: give it a
    // symmetric band so it draws through the middle.
    const pad = Math.abs(min) > 0 ? Math.abs(min) * 0.1 : 1;
    min -= pad;
    max += pad;
  }

  const xs = points.map((p) => p.x);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const spanX = xMax - xMin;

  const sx = (x: number): number =>
    spanX === 0 ? (left + right) / 2 : left + ((x - xMin) / spanX) * (right - left);
  const sy = (y: number): number => bottom - ((y - min) / (max - min)) * (bottom - top);

  const plotted = points.map((p) => ({ x: round(sx(p.x)), y: round(sy(p.y)) }));
  const line = plotted
    .map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`)
    .join(" ");

  const baselineValue = min <= 0 && max >= 0 ? 0 : min;
  const baselineY = round(sy(baselineValue));
  const first = plotted[0];
  const last = plotted[plotted.length - 1];
  const area = `${line} L ${last.x} ${baselineY} L ${first.x} ${baselineY} Z`;

  return {
    line,
    area,
    last,
    baselineY,
    zeroY: min <= 0 && max >= 0 ? baselineY : null,
    min,
    max,
  };
}
