import { describe, expect, it } from 'vitest';
import { parseChannels, parsePicks, parseStats } from '../../src/lib/schema';
import picksRaw from '../../src/data/picks.json';
import channelsRaw from '../../src/data/channels.json';
import statsRaw from '../../src/data/stats.json';

describe('data files validate against the schemas', () => {
  it('picks.json parses', () => {
    const picks = parsePicks(picksRaw);
    expect(picks.length).toBeGreaterThan(0);
  });

  it('channels.json parses', () => {
    const channels = parseChannels(channelsRaw);
    expect(channels.length).toBeGreaterThan(0);
  });

  it('stats.json parses', () => {
    const stats = parseStats(statsRaw);
    expect(stats.sources.verified).toBeGreaterThan(0);
  });
});

describe('cross-file invariants', () => {
  const picks = parsePicks(picksRaw);
  const channelSlugs = new Set(parseChannels(channelsRaw).map((c) => c.slug));

  it('every pick channel exists in channels.json', () => {
    for (const p of picks) {
      for (const ch of p.channels) {
        expect(channelSlugs, `pick #${p.id} references unknown channel "${ch}"`).toContain(ch);
      }
    }
  });

  it('no archive.* URLs anywhere in picks.json', () => {
    expect(JSON.stringify(picksRaw)).not.toContain('archive.');
  });

  it('every pick has a primary source_url', () => {
    for (const p of picks) {
      expect(p.source_url.length > 0, `pick #${p.id} has no primary link`).toBe(true);
    }
  });
});

describe('stats v2 coverage blocks', () => {
  // Thin shape (current live pipeline export): must still parse.
  it('parses without any v2 block', () => {
    const thin = { ...statsRaw } as Record<string, unknown>;
    delete thin.series_daily; delete thin.funnel; delete thin.relevance_hist_30d;
    delete thin.models_used_30d; delete thin.fetch_30d; delete thin.top_sources_30d;
    delete thin.echo_dist; delete thin.rhythm_7x24;
    expect(() => parseStats(thin)).not.toThrow();
  });

  // Rich shape: a minimal fixture with every v2 block present.
  it('parses with all v2 blocks', () => {
    const rich = {
      ...(statsRaw as Record<string, unknown>),
      series_daily: [{ d: '2026-07-01', items: 3204, clusters: 2900, curated: 25 }],
      funnel: {
        all_time: { items: 123935, clusters: 111215, fetched: 4200, curated: 897, published: 850 },
        last_30d: { items: 90000, clusters: 82000, fetched: 900, curated: 600, published: 580 },
      },
      relevance_hist_30d: { kept: { '7': 40, '8': 12 }, skipped: { '3': 55 } },
      models_used_30d: [
        { scope: 'curation', model: 'qwen2.5:14b', backend: 'local', count: 412, avg_relevance: 7.1 },
        { scope: 'digest', model: 'claude-opus-4-8', backend: null, count: 9, avg_relevance: null },
      ],
      fetch_30d: { ok: 610, paywalled: 55, failed: 20, skipped: 210 },
      top_sources_30d: [{ name: 'Hacker News', items: 3100 }],
      echo_dist: { '1': 90000, '2': 12000, '3_5': 7000, '6_plus': 2200 },
      rhythm_7x24: Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => 3)),
    };
    const parsed = parseStats(rich);
    expect(parsed.series_daily![0].items).toBe(3204);
    expect(parsed.models_used_30d![0].scope).toBe('curation');
  });

  it('rejects a malformed rhythm grid (wrong row length)', () => {
    const bad = {
      ...(statsRaw as Record<string, unknown>),
      rhythm_7x24: Array.from({ length: 7 }, () => [1, 2, 3]),
    };
    expect(() => parseStats(bad)).toThrow();
  });
});
