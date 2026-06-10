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
