import { describe, expect, it } from 'vitest';
import {
  columnChart,
  curatedStrip,
  fmtDay,
  seriesCaption,
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
