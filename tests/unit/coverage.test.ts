import { describe, expect, it } from 'vitest';
import {
  columnChart,
  curatedStrip,
  editionsCalendar,
  fmtDay,
  funnelRows,
  heatLevels,
  histogramRows,
  seriesCaption,
  stripSegments,
} from '../../src/lib/coverage';

const day = (d: string, items: number, curated = 0) => ({
  d,
  items,
  clusters: 0,
  curated,
});

describe('fmtDay', () => {
  it('formats an ISO date as short month + day', () => {
    expect(fmtDay('2026-07-04')).toBe('Jul 4');
    expect(fmtDay('2026-12-31')).toBe('Dec 31');
  });
});

describe('columnChart', () => {
  const series = [day('2026-07-01', 100, 5), day('2026-07-02', 50), day('2026-07-03', 0)];
  const { columns, max } = columnChart(series, 300, 100, 2);

  it('scales the tallest column to full height', () => {
    expect(max).toBe(100);
    expect(columns[0].h).toBe(100);
    expect(columns[0].y).toBe(0);
  });

  it('gives zero days zero height at the baseline', () => {
    expect(columns[2].h).toBe(0);
    expect(columns[2].y).toBe(100);
  });

  it('fills the width: last column ends at width', () => {
    const last = columns[columns.length - 1];
    expect(last.x + last.w).toBeCloseTo(300, 6);
  });

  it('titles carry the day, items, and curated count', () => {
    expect(columns[0].title).toBe('Jul 1: 100 items, 5 curated');
  });

  it('never divides by zero on an all-zero series', () => {
    const flat = columnChart([day('2026-07-01', 0)], 300, 100);
    expect(flat.columns[0].h).toBe(0);
  });
});

describe('curatedStrip', () => {
  it('maps zero to level 0 and the max to the top level', () => {
    const strip = curatedStrip(
      [day('2026-07-01', 1, 0), day('2026-07-02', 1, 2), day('2026-07-03', 1, 8)],
      5
    );
    expect(strip[0].level).toBe(0);
    expect(strip[2].level).toBe(4);
    expect(strip[1].level).toBeGreaterThanOrEqual(1);
    expect(strip[1].level).toBeLessThan(4);
    expect(strip[2].title).toBe('Jul 3: 8 curated');
  });

  it('handles an all-zero curated series', () => {
    const strip = curatedStrip([day('2026-07-01', 5, 0)]);
    expect(strip[0].level).toBe(0);
  });
});

describe('seriesCaption', () => {
  it('finds busiest, quietest, and mean', () => {
    const cap = seriesCaption([
      day('2026-07-01', 100),
      day('2026-07-02', 40),
      day('2026-07-03', 10),
    ]);
    expect(cap.busiest.d).toBe('2026-07-01');
    expect(cap.quietest.d).toBe('2026-07-03');
    expect(cap.mean).toBe(50);
  });
});

describe('funnelRows', () => {
  const rows = funnelRows({
    items: 100000,
    clusters: 90000,
    fetched: 4000,
    curated: 900,
    published: 850,
  });

  it('orders the five stages and keeps counts', () => {
    expect(rows.map((r) => r.key)).toEqual([
      'items',
      'clusters',
      'fetched',
      'curated',
      'published',
    ]);
    expect(rows[0].count).toBe(100000);
  });

  it('computes conversion vs the previous stage (first is null)', () => {
    expect(rows[0].pct).toBeNull();
    expect(rows[1].pct).toBeCloseTo(90, 1);
    expect(rows[4].pct).toBeCloseTo((850 / 900) * 100, 1);
  });

  it('log-scales widths: first is 100, all positive counts >= 2', () => {
    expect(rows[0].widthPct).toBe(100);
    for (const r of rows) expect(r.widthPct).toBeGreaterThanOrEqual(2);
    expect(rows[3].widthPct).toBeLessThan(rows[1].widthPct);
  });

  it('survives zero counts', () => {
    const z = funnelRows({ items: 0, clusters: 0, fetched: 0, curated: 0, published: 0 });
    expect(z[0].widthPct).toBe(2);
    expect(z[1].pct).toBeNull();
  });
});

describe('stripSegments', () => {
  it('drops zero segments and yields percentages summing to ~100', () => {
    const segs = stripSegments([
      { label: 'ok', value: 75 },
      { label: 'paywalled', value: 25 },
      { label: 'failed', value: 0 },
    ]);
    expect(segs.map((s) => s.label)).toEqual(['ok', 'paywalled']);
    expect(segs[0].pct).toBeCloseTo(75, 5);
    expect(segs.reduce((a, s) => a + s.pct, 0)).toBeCloseTo(100, 5);
  });

  it('returns [] when everything is zero', () => {
    expect(stripSegments([{ label: 'x', value: 0 }])).toEqual([]);
  });
});

describe('histogramRows', () => {
  it('fills 0..10, coercing missing buckets to zero', () => {
    const { rows, max } = histogramRows({ kept: { '7': 40 }, skipped: { '3': 55 } });
    expect(rows).toHaveLength(11);
    expect(rows[7]).toEqual({ score: 7, kept: 40, skipped: 0 });
    expect(rows[3]).toEqual({ score: 3, kept: 0, skipped: 55 });
    expect(max).toBe(55);
  });
});

describe('heatLevels', () => {
  it('buckets 0 to level 0 and max to the top level', () => {
    const levels = heatLevels([
      [0, 5, 10],
      [1, 0, 2],
    ]);
    expect(levels[0][0]).toBe(0);
    expect(levels[0][2]).toBe(4);
    expect(levels[1][1]).toBe(0);
    expect(levels[1][0]).toBeGreaterThanOrEqual(1);
  });

  it('handles an all-zero grid', () => {
    expect(heatLevels([[0, 0]])).toEqual([[0, 0]]);
  });
});

const entry = (kind: string, iso: string, id: string) => ({
  kind,
  date: new Date(`${iso}T00:00:00Z`),
  id,
  title: id,
  period: iso,
});

describe('editionsCalendar', () => {
  const entries = [
    entry('daily', '2026-05-14', 'daily/2026-05-14'), // a Thursday
    entry('daily', '2026-05-15', 'daily/2026-05-15'),
    entry('weekly', '2026-05-11', 'weekly/2026-w20'), // Monday of that week
    entry('monthly', '2026-05-01', 'monthly/2026-05'),
  ];
  const cal = editionsCalendar(entries, new Date('2026-05-21T12:00:00Z'));

  it('spans from the earliest entry week to the today week', () => {
    expect(cal.weeks).toHaveLength(2); // week of May 11, week of May 18
    expect(cal.weeks[0].days[0].date).toBe('2026-05-11');
    expect(cal.weeks[0].days[6].date).toBe('2026-05-17');
  });

  it('places dailies on their day and weeklies on their week', () => {
    expect(cal.weeks[0].days[3].entry?.id).toBe('daily/2026-05-14');
    expect(cal.weeks[0].days[0].entry).toBeNull(); // weekly is not a day fill
    expect(cal.weeks[0].weekly?.id).toBe('weekly/2026-w20');
    expect(cal.weeks[1].weekly).toBeNull();
  });

  it('routes monthly/quarterly/yearly to specials, ascending', () => {
    expect(cal.specials.map((s) => s.id)).toEqual(['monthly/2026-05']);
  });

  it('returns empty structures for no entries', () => {
    const empty = editionsCalendar([], new Date('2026-05-21T00:00:00Z'));
    expect(empty.weeks).toEqual([]);
    expect(empty.specials).toEqual([]);
  });
});
