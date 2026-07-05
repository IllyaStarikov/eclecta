/**
 * Pure geometry/binning helpers behind /coverage/ — the transparency
 * dashboard. No Astro imports, no DOM: everything here is unit-tested and
 * the .astro page stays thin. Charts are build-time inline SVG/CSS per the
 * design language (no chart libraries).
 */
import type { DayPoint } from './schema';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** '2026-07-04' -> 'Jul 4' (UTC, no locale surprises at build time). */
export function fmtDay(d: string): string {
  const m = Number(d.slice(5, 7));
  const day = Number(d.slice(8, 10));
  return `${MONTHS[m - 1]} ${day}`;
}

export interface Column {
  x: number;
  y: number;
  w: number;
  h: number;
  title: string;
}

/**
 * Column geometry for the 90-day wire chart. Origin top-left (SVG): a
 * column of height h sits at y = height - h. Zero days render h=0 (no
 * 1px lie for a genuinely empty day).
 */
export function columnChart(
  series: DayPoint[],
  width: number,
  height: number,
  gap = 1
): { columns: Column[]; max: number } {
  const max = Math.max(...series.map((p) => p.items), 1);
  const w = (width - gap * (series.length - 1)) / series.length;
  const columns = series.map((p, i) => {
    const h = p.items === 0 ? 0 : Math.max(1, (p.items / max) * height);
    return {
      x: i * (w + gap),
      y: height - h,
      w,
      h,
      title: `${fmtDay(p.d)}: ${p.items.toLocaleString('en-US')} items, ${p.curated} curated`,
    };
  });
  return { columns, max: Math.max(...series.map((p) => p.items), 0) };
}

/**
 * Curated-picks strip under the wire chart: one cell per day, opacity level
 * 0..steps-1 (0 = none). Own scale — curated volume is two orders below
 * ingest and would vanish on the shared axis.
 */
export function curatedStrip(
  series: DayPoint[],
  steps = 5
): { level: number; title: string }[] {
  const max = Math.max(...series.map((p) => p.curated), 0);
  return series.map((p) => ({
    level: p.curated === 0 || max === 0 ? 0 : Math.max(1, Math.ceil((p.curated / max) * (steps - 1))),
    title: `${fmtDay(p.d)}: ${p.curated} curated`,
  }));
}

export function seriesCaption(series: DayPoint[]): {
  busiest: DayPoint;
  quietest: DayPoint;
  mean: number;
} {
  let busiest = series[0];
  let quietest = series[0];
  let total = 0;
  for (const p of series) {
    if (p.items > busiest.items) busiest = p;
    if (p.items < quietest.items) quietest = p;
    total += p.items;
  }
  return { busiest, quietest, mean: Math.round(total / series.length) };
}
