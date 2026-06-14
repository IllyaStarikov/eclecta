import { describe, expect, it } from 'vitest';
import {
  SOURCE_CATEGORIES,
  canonicalUrl,
  normalizeName,
  parseSources,
} from '../../src/lib/sources';
import sourcesRaw from '../../src/data/sources.json';
import statsRaw from '../../src/data/stats.json';

const sources = parseSources(sourcesRaw);

describe('sources.json validates against the schema', () => {
  it('parses and is non-empty', () => {
    expect(sources.length).toBeGreaterThan(0);
  });

  it('every category is one of the known 12', () => {
    const known = new Set<string>(SOURCE_CATEGORIES);
    for (const s of sources) {
      expect(known, `"${s.name}" has unknown category "${s.category}"`).toContain(s.category);
    }
  });

  it('every tier is 1, 2, or 3', () => {
    for (const s of sources) expect([1, 2, 3]).toContain(s.tier);
  });

  it('every homepage is an http(s) URL', () => {
    for (const s of sources) {
      expect(/^https?:\/\//i.test(s.homepage), `"${s.name}" homepage is not http(s): ${s.homepage}`).toBe(true);
    }
  });

  it('every feed (when present) is an http(s) URL', () => {
    for (const s of sources) {
      if (s.feed) {
        expect(/^https?:\/\//i.test(s.feed), `"${s.name}" feed is not http(s): ${s.feed}`).toBe(true);
      }
    }
  });
});

describe('sources.json has no duplicates', () => {
  it('no two sources share a canonical URL', () => {
    const groups = new Map<string, string[]>();
    for (const s of sources) {
      const k = canonicalUrl(s.homepage);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k)!.push(s.name);
    }
    const dups = [...groups.entries()].filter(([, g]) => g.length > 1);
    expect(dups, `duplicate canonical URLs:\n${dups.map(([k, g]) => `  ${k} <- ${g.join(', ')}`).join('\n')}`).toHaveLength(0);
  });

  it('no two sources share a normalized name', () => {
    const groups = new Map<string, string[]>();
    for (const s of sources) {
      const k = normalizeName(s.name);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k)!.push(`${s.name} <${s.homepage}>`);
    }
    const dups = [...groups.entries()].filter(([, g]) => g.length > 1);
    expect(dups, `duplicate normalized names:\n${dups.map(([k, g]) => `  "${k}" <- ${g.join(' | ')}`).join('\n')}`).toHaveLength(0);
  });
});

describe('tier discipline', () => {
  it('tier-1 (flagship) stays small — at most 12% of the set', () => {
    const t1 = sources.filter((s) => s.tier === 1).length;
    const ratio = t1 / sources.length;
    expect(ratio, `tier-1 is ${(ratio * 100).toFixed(1)}% (${t1}/${sources.length}); flagship should stay <=12%`).toBeLessThanOrEqual(0.12);
  });
});

describe('soft consistency with stats.json (pipeline-owned, drift tolerated)', () => {
  const stats = statsRaw as {
    sources: { total: number; by_category: Record<string, number>; by_tier: Record<string, number> };
  };

  it('stats by_tier and by_category count the same set (internally consistent)', () => {
    // Note: stats.total counts all sources; by_tier/by_category count only the
    // enabled set, so they sum to `enabled` (<= total), not to `total`.
    const tierSum = Object.values(stats.sources.by_tier).reduce((a, b) => a + b, 0);
    const catSum = Object.values(stats.sources.by_category).reduce((a, b) => a + b, 0);
    expect(tierSum).toBe(catSum);
  });

  it('every source category appears in stats.by_category keys', () => {
    const statKeys = new Set(Object.keys(stats.sources.by_category));
    const used = new Set(sources.map((s) => s.category));
    for (const c of used) {
      expect(statKeys, `category "${c}" missing from stats.by_category`).toContain(c);
    }
  });
});
