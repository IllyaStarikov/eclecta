/**
 * Spotlight selection + formatting. Render-lenient (a malformed file hides
 * the section), CI-strict (when src/data/spotlight.json exists it MUST
 * parse — a bad pipeline export fails the build here, not on readers).
 */
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { spotlightFileSchema, type Pick } from '../../src/lib/schema';
import {
  SPOTLIGHT_LIMIT,
  joinCurated,
  normalizeSpotlight,
  relativeAge,
  tractionParts,
} from '../../src/lib/spotlight';
import fixture from '../fixtures/spotlight.json';

const NOW = new Date('2026-07-04T18:00:00+00:00');

describe('normalizeSpotlight', () => {
  it('parses the object shape, sorts by score, keeps generated_at', () => {
    const s = normalizeSpotlight(fixture);
    expect(s.generatedAt).toBe('2026-07-04T18:00:00+00:00');
    expect(s.items.map((i) => i.story_id)).toEqual(['st-aaa', 'st-bbb', 'st-ccc']);
  });

  it('parses the bare-array shape', () => {
    const s = normalizeSpotlight((fixture as { items: unknown[] }).items);
    expect(s.generatedAt).toBeNull();
    expect(s.items).toHaveLength(3);
  });

  it('resolves link from url or canonical_url', () => {
    const s = normalizeSpotlight(fixture);
    expect(s.items[0].link).toBe('https://example.com/openweight');
    expect(s.items[1].link).toBe('https://example.com/zeroday');
  });

  it(`caps at ${SPOTLIGHT_LIMIT}`, () => {
    const many = Array.from({ length: 20 }, (_, i) => ({
      ...(fixture.items[1] as object),
      story_id: `st-${i}`,
      score: i,
    }));
    const s = normalizeSpotlight(many);
    expect(s.items).toHaveLength(SPOTLIGHT_LIMIT);
    expect(s.items[0].score).toBe(19); // highest score first
  });

  it('degrades to empty on malformed input and on null', () => {
    expect(normalizeSpotlight({ items: [{ nope: true }] }).items).toEqual([]);
    expect(normalizeSpotlight(null).items).toEqual([]);
  });
});

describe('tractionParts', () => {
  it('formats the full line with k-abbreviation', () => {
    expect(tractionParts(normalizeSpotlight(fixture).items[0], NOW)).toEqual([
      '14 surfaces',
      '2.2k pts',
      '381 comments',
      'first seen 12h ago',
    ]);
  });

  it('omits zero parts and uses the singular', () => {
    const item = { ...normalizeSpotlight(fixture).items[2], surface_count: 1 };
    const parts = tractionParts(item, NOW);
    expect(parts[0]).toBe('1 surface');
    expect(parts.some((p) => p.endsWith('pts'))).toBe(false);
    expect(parts).toContain('12 comments');
  });

  it('sums points/comments from surfaces when totals are absent', () => {
    const item = normalizeSpotlight(fixture).items[0];
    const bare = { ...item, points: null, comments: null };
    const parts = tractionParts(bare, NOW);
    expect(parts).toContain('2.2k pts');
  });
});

describe('relativeAge', () => {
  it('buckets hours and days', () => {
    expect(relativeAge('2026-07-04T17:30:00+00:00', NOW)).toBe('under 1h ago');
    expect(relativeAge('2026-07-04T06:00:00+00:00', NOW)).toBe('12h ago');
    expect(relativeAge('2026-07-01T18:00:00+00:00', NOW)).toBe('3d ago');
  });
});

describe('joinCurated', () => {
  const pick = { id: 7, why: 'It matters.' } as unknown as Pick;

  it('attaches the pick on a live pick_id and degrades on a dangling one', () => {
    const items = normalizeSpotlight(fixture).items;
    const joined = joinCurated(items, [pick]);
    expect(joined[0].pick?.why).toBe('It matters.');
    expect(joined[2].pick).toBeUndefined(); // pick_id 999999 rotated out
    expect(joined[1].pick).toBeUndefined(); // uncurated
  });
});

describe('the real spotlight.json, when present', () => {
  it('must parse strictly', () => {
    const path = join(__dirname, '../../src/data/spotlight.json');
    if (!existsSync(path)) return; // dormant until the pipeline emits it
    const parsed = spotlightFileSchema.safeParse(JSON.parse(readFileSync(path, 'utf8')));
    expect(parsed.success, JSON.stringify(parsed.success ? '' : parsed.error.issues[0])).toBe(true);
  });
});
