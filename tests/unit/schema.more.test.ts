import { describe, expect, it } from 'vitest';
import { parseChannels, parsePicks, parseStats } from '../../src/lib/schema';

/* ── synthetic factories ───────────────────────────────────────────────────
 * These exercise the parse functions with hand-built inputs. The real data
 * files are already covered by schema.test.ts — we deliberately do NOT reload
 * them here. */

/** A minimal pick with every REQUIRED key present (nullable keys set to null
 * where the contract allows it). No optional v2 fields. */
function minimalPick(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    title: 'A pick',
    relevance: 0.5,
    score: 10,
    why: 'because',
    notes: [],
    summary: 'a summary',
    channels: [],
    novelty: null,
    audience: null,
    source_url: 'https://example.com/story',
    read_kind: null,
    free_link: null,
    paywalled: false,
    surfaces: [],
    sources_count: 0,
    first_seen: null,
    curated_at: '2026-07-04T00:00:00Z',
    model: null,
    ...overrides,
  };
}

function minimalChannel(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { slug: 'ai', name: 'AI', blurb: 'stuff', ...overrides };
}

function minimalStats(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    generated_at: '2026-07-04T00:00:00Z',
    sources: {
      total: 100,
      verified: 90,
      by_category: {},
      by_tier: {},
    },
    pipeline: {
      items_total: 1000,
      clusters_total: 200,
      curations_done: 50,
    },
    digests: {
      total: 5,
      by_kind: {},
      latest: null,
    },
    channels: [],
    top_surfaces_7d: [],
    models: {
      triage: 'triage-model',
      deep: 'deep-model',
      digest: 'digest-model',
    },
    ...overrides,
  };
}

describe('parsePicks — synthetic', () => {
  it('accepts a minimal valid pick', () => {
    const picks = parsePicks([minimalPick()]);
    expect(picks).toHaveLength(1);
    expect(picks[0].source_url).toBe('https://example.com/story');
  });

  it('throws when source_url is missing', () => {
    const { source_url: _omit, ...noUrl } = minimalPick();
    expect(() => parsePicks([noUrl])).toThrow();
  });

  it('throws when title is missing', () => {
    const { title: _omit, ...noTitle } = minimalPick();
    expect(() => parsePicks([noTitle])).toThrow();
  });

  it('throws when source_url is the empty string (min(1))', () => {
    expect(() => parsePicks([minimalPick({ source_url: '' })])).toThrow();
  });

  it('throws when title is the empty string (min(1))', () => {
    expect(() => parsePicks([minimalPick({ title: '' })])).toThrow();
  });

  it('accepts the v2 taxonomy fields when present', () => {
    const picks = parsePicks([
      minimalPick({
        category: 'science',
        subcategories: ['physics', 'space'],
        story_id: 'abc123',
        state: 'confident',
        published_at: '2026-07-01T00:00:00Z',
      }),
    ]);
    expect(picks[0].category).toBe('science');
    expect(picks[0].subcategories).toEqual(['physics', 'space']);
    expect(picks[0].story_id).toBe('abc123');
    expect(picks[0].state).toBe('confident');
    expect(picks[0].published_at).toBe('2026-07-01T00:00:00Z');
  });

  it('accepts the v2 taxonomy fields when absent (all optional)', () => {
    const picks = parsePicks([minimalPick()]);
    expect(picks[0].category).toBeUndefined();
    expect(picks[0].subcategories).toBeUndefined();
    expect(picks[0].story_id).toBeUndefined();
    expect(picks[0].state).toBeUndefined();
    expect(picks[0].published_at).toBeUndefined();
  });

  it('accepts null for every nullable field', () => {
    const picks = parsePicks([
      minimalPick({
        novelty: null,
        audience: null,
        read_kind: null,
        free_link: null,
        first_seen: null,
        model: null,
        published_at: null,
      }),
    ]);
    const p = picks[0];
    expect(p.novelty).toBeNull();
    expect(p.audience).toBeNull();
    expect(p.read_kind).toBeNull();
    expect(p.free_link).toBeNull();
    expect(p.first_seen).toBeNull();
    expect(p.model).toBeNull();
    expect(p.published_at).toBeNull();
  });

  it('rejects a bad state enum value', () => {
    expect(() => parsePicks([minimalPick({ state: 'maybe' })])).toThrow();
  });

  it('accepts both valid state enum values', () => {
    expect(parsePicks([minimalPick({ state: 'confident' })])[0].state).toBe('confident');
    expect(parsePicks([minimalPick({ state: 'developing' })])[0].state).toBe('developing');
  });

  it('validates nested surfaces (points/comments nullable, url/name min(1))', () => {
    const ok = parsePicks([
      minimalPick({
        surfaces: [
          { url: 'https://a', name: 'A', points: null, comments: null },
          { url: 'https://b', name: 'B', points: 5, comments: 3 },
        ],
      }),
    ]);
    expect(ok[0].surfaces).toHaveLength(2);

    // empty surface url is rejected
    expect(() =>
      parsePicks([
        minimalPick({ surfaces: [{ url: '', name: 'A', points: null, comments: null }] }),
      ])
    ).toThrow();
  });

  it('accepts an empty array of picks', () => {
    expect(parsePicks([])).toEqual([]);
  });

  it('throws when given a non-array', () => {
    expect(() => parsePicks(minimalPick())).toThrow();
  });
});

describe('parseChannels — synthetic', () => {
  it('accepts a valid channel array', () => {
    const channels = parseChannels([minimalChannel(), minimalChannel({ slug: 'crypto', name: 'Crypto' })]);
    expect(channels).toHaveLength(2);
    expect(channels[1].slug).toBe('crypto');
  });

  it('throws when slug is missing', () => {
    const { slug: _omit, ...noSlug } = minimalChannel();
    expect(() => parseChannels([noSlug])).toThrow();
  });

  it('throws on empty slug/name (min(1))', () => {
    expect(() => parseChannels([minimalChannel({ slug: '' })])).toThrow();
    expect(() => parseChannels([minimalChannel({ name: '' })])).toThrow();
  });

  it('throws when blurb is the wrong type', () => {
    expect(() => parseChannels([minimalChannel({ blurb: 42 })])).toThrow();
  });
});

describe('parseStats — synthetic', () => {
  it('accepts a minimal valid stats object', () => {
    const stats = parseStats(minimalStats());
    expect(stats.sources.verified).toBe(90);
    expect(stats.models.triage).toBe('triage-model');
  });

  it('models.catchall accepts an extra stage', () => {
    const stats = parseStats(
      minimalStats({
        models: {
          triage: 'triage-model',
          deep: 'deep-model',
          digest: 'digest-model',
          embed: 'embed-model',
        },
      })
    );
    // pinned stages plus the extra one all survive
    expect(stats.models.triage).toBe('triage-model');
    expect((stats.models as Record<string, string>).embed).toBe('embed-model');
  });

  it('throws when a pinned model stage is missing', () => {
    for (const stage of ['triage', 'deep', 'digest']) {
      const models = {
        triage: 'triage-model',
        deep: 'deep-model',
        digest: 'digest-model',
      } as Record<string, string>;
      delete models[stage];
      expect(() => parseStats(minimalStats({ models })), `missing ${stage} should throw`).toThrow();
    }
  });

  it('models.catchall rejects a non-string extra stage', () => {
    expect(() =>
      parseStats(
        minimalStats({
          models: { triage: 't', deep: 'd', digest: 'g', extra: 123 },
        })
      )
    ).toThrow();
  });

  it('by_category / by_tier are string→number records', () => {
    const stats = parseStats(
      minimalStats({
        sources: {
          total: 3,
          verified: 3,
          by_category: { ai: 2, science: 1 },
          by_tier: { primary: 1, secondary: 2 },
        },
      })
    );
    expect(stats.sources.by_category.ai).toBe(2);
    expect(stats.sources.by_tier.secondary).toBe(2);
  });

  it('rejects a non-number value in by_category', () => {
    expect(() =>
      parseStats(
        minimalStats({
          sources: { total: 1, verified: 1, by_category: { ai: 'lots' }, by_tier: {} },
        })
      )
    ).toThrow();
  });

  it('accepts a null latest digest', () => {
    const stats = parseStats(minimalStats({ digests: { total: 0, by_kind: {}, latest: null } }));
    expect(stats.digests.latest).toBeNull();
  });

  it('accepts an omitted latest digest (optional)', () => {
    const stats = parseStats(minimalStats({ digests: { total: 0, by_kind: {} } }));
    expect(stats.digests.latest).toBeUndefined();
  });

  it('accepts a fully populated latest digest', () => {
    const stats = parseStats(
      minimalStats({
        digests: {
          total: 1,
          by_kind: { weekly: 1 },
          latest: { kind: 'weekly', period: '2026-W27', title: 'This Week', date: '2026-07-04' },
        },
      })
    );
    expect(stats.digests.latest).toEqual({
      kind: 'weekly',
      period: '2026-W27',
      title: 'This Week',
      date: '2026-07-04',
    });
  });

  it('rejects a latest digest missing a required field', () => {
    expect(() =>
      parseStats(
        minimalStats({
          digests: { total: 1, by_kind: {}, latest: { kind: 'weekly', period: 'p', title: 't' } },
        })
      )
    ).toThrow();
  });

  it('accepts the optional pipeline 7d fields and site_name/enabled', () => {
    const stats = parseStats(
      minimalStats({
        site_name: 'Eclecta',
        sources: { total: 5, enabled: 4, verified: 5, by_category: {}, by_tier: {} },
        pipeline: {
          items_total: 1,
          clusters_total: 1,
          curations_done: 1,
          items_7d: 10,
          curated_7d: 3,
          avg_relevance_7d: 0.42,
        },
      })
    );
    expect(stats.site_name).toBe('Eclecta');
    expect(stats.sources.enabled).toBe(4);
    expect(stats.pipeline.avg_relevance_7d).toBeCloseTo(0.42);
  });

  it('throws when the models object is absent entirely', () => {
    const { models: _omit, ...noModels } = minimalStats();
    expect(() => parseStats(noModels)).toThrow();
  });
});
