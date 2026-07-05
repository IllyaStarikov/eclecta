import { describe, expect, it } from 'vitest';
import {
  SOURCE_CATEGORIES,
  canonicalUrl,
  normalizeName,
  sortSources,
  parseSources,
  type Source,
} from '../../src/lib/sources';

/**
 * Focused, synthetic-input coverage for the src/lib/sources.ts helpers.
 * The existing sources.test.ts exercises these against the real data file;
 * here we pin the exact contract of each function with sharp cases, including
 * the canonicalUrl catch-branch fallback for non-URL input.
 */

// -- helper: build a Source without repeating the boilerplate; `category` is
// typed loosely so we can also feed an unknown category to sortSources.
type SrcOverrides = {
  name: string;
  category: string;
  tier?: 1 | 2 | 3;
  homepage?: string;
  paywalled?: boolean;
  feed?: string | null;
};
const src = (o: SrcOverrides): Source =>
  ({
    homepage: 'https://example.com',
    tier: 2,
    paywalled: false,
    feed: null,
    ...o,
  } as unknown as Source);

describe('canonicalUrl() — try branch (valid URLs)', () => {
  it('strips scheme, leading www, and trailing slash', () => {
    expect(canonicalUrl('https://www.host.com/blog/')).toBe('host.com/blog');
  });

  it('collapses http+www and https+bare variants to one key', () => {
    expect(canonicalUrl('http://www.host.com/blog/')).toBe('host.com/blog');
    expect(canonicalUrl('https://host.com/blog')).toBe('host.com/blog');
    expect(canonicalUrl('http://www.host.com/blog/')).toBe(canonicalUrl('https://host.com/blog'));
  });

  it('keeps the path, so bare host and host/blog stay distinct', () => {
    expect(canonicalUrl('https://host.com')).toBe('host.com');
    expect(canonicalUrl('https://host.com/blog')).toBe('host.com/blog');
    expect(canonicalUrl('https://host.com')).not.toBe(canonicalUrl('https://host.com/blog'));
  });

  it('lowercases the host but preserves path case', () => {
    expect(canonicalUrl('https://WWW.HOST.com/Blog')).toBe('host.com/Blog');
  });

  it('drops the fragment', () => {
    expect(canonicalUrl('https://host.com/x#section')).toBe('host.com/x');
  });

  it('collapses a root path and repeated trailing slashes', () => {
    expect(canonicalUrl('https://host.com/')).toBe('host.com');
    expect(canonicalUrl('https://host.com/x///')).toBe('host.com/x');
  });

  it('strips tracking params but keeps the rest, sorted', () => {
    const input =
      'https://host.com/x?keep1=a&ref=x&referrer=y&source=z&sourced=w' +
      '&utm_source=nl&utm_medium=email&fbclid=1&gclid=2&mc_cid=3&mc_eid=4&igshid=5&b=2&a=1';
    // stripped: utm_*, fbclid, gclid, mc_*, ref, source, igshid
    // kept (and re-sorted): a, b, keep1, referrer, sourced
    expect(canonicalUrl(input)).toBe('host.com/x?a=1&b=2&keep1=a&referrer=y&sourced=w');
  });

  it('re-sorts kept params regardless of original order', () => {
    expect(canonicalUrl('https://host.com/x?z=1&a=2&m=3')).toBe('host.com/x?a=2&m=3&z=1');
  });

  it('emits no query string when the only params were tracking', () => {
    expect(canonicalUrl('https://host.com/x?utm_source=nl&fbclid=9')).toBe('host.com/x');
  });

  it('strips ref/source/igshid exactly but keeps look-alike keys', () => {
    // ref/source are anchored ($), so referrer/sourced survive
    expect(canonicalUrl('https://host.com/?ref=a&referrer=b&source=c&sourced=d')).toBe(
      'host.com?referrer=b&sourced=d'
    );
  });
});

describe('canonicalUrl() — catch branch (non-URL input)', () => {
  it('falls back for bare domains, stripping www/query/fragment/trailing slash', () => {
    // no scheme -> new URL throws -> regex fallback
    expect(canonicalUrl('www.example.com/blog/?utm=1#top')).toBe('example.com/blog');
  });

  it('fallback strips the WHOLE query, not just tracking params', () => {
    expect(canonicalUrl('example.com/x?keep=yes')).toBe('example.com/x');
  });

  it('fallback strips the scheme when host is malformed', () => {
    // space in host makes new URL throw even with a scheme present
    expect(canonicalUrl('http://exa mple.com/path')).toBe('exa mple.com/path');
  });

  it('lowercases everything in the fallback path', () => {
    expect(canonicalUrl('WWW.Example.COM/Blog/')).toBe('example.com/blog');
  });

  it('returns empty string for empty / whitespace-only input', () => {
    expect(canonicalUrl('')).toBe('');
    expect(canonicalUrl('   ')).toBe('');
  });

  it('trims surrounding whitespace before parsing', () => {
    expect(canonicalUrl('  https://host.com/x/  ')).toBe('host.com/x');
  });
});

describe('normalizeName()', () => {
  it('drops parentheticals', () => {
    expect(normalizeName('Machine Learning Street Talk (MLST)')).toBe('machine learning street talk');
  });

  it('drops multiple parentheticals', () => {
    expect(normalizeName('Foo (bar) Baz (qux)')).toBe('foo baz');
  });

  it('collapses non-alphanumeric runs to single spaces and trims', () => {
    expect(normalizeName('  --Hello--World!!  ')).toBe('hello world');
    expect(normalizeName('A.I. Weekly')).toBe('a i weekly');
  });

  it('lowercases and keeps digits', () => {
    expect(normalizeName('ArXiv Web3 News')).toBe('arxiv web3 news');
  });

  it('returns empty string when nothing alphanumeric survives', () => {
    expect(normalizeName('   ')).toBe('');
    expect(normalizeName('()')).toBe('');
    expect(normalizeName('---')).toBe('');
  });
});

describe('sortSources()', () => {
  it('orders by category (per SOURCE_CATEGORIES), then tier, then name', () => {
    const s1 = src({ name: 'B', category: 'tech_news', tier: 1 }); // idx 11
    const s2 = src({ name: 'Z', category: 'aggregators', tier: 3 }); // idx 0
    const s3 = src({ name: 'apple', category: 'aggregators', tier: 1 });
    const s4 = src({ name: 'Mango', category: 'aggregators', tier: 1 });
    const s5 = src({ name: 'X', category: 'devtools', tier: 2 }); // idx 2
    const sorted = sortSources([s1, s2, s3, s4, s5]);
    // aggregators tier1 (apple<Mango), aggregators tier3 (Z), devtools, tech_news
    expect(sorted.map((s) => s.name)).toEqual(['apple', 'Mango', 'Z', 'X', 'B']);
  });

  it('name comparison is case-insensitive within a category+tier', () => {
    const sorted = sortSources([
      src({ name: 'zebra', category: 'news', tier: 1 }),
      src({ name: 'Apple', category: 'news', tier: 1 }),
      src({ name: 'mango', category: 'news', tier: 1 }),
    ]);
    expect(sorted.map((s) => s.name)).toEqual(['Apple', 'mango', 'zebra']);
  });

  it('sorts unknown categories last', () => {
    const known = src({ name: 'Known', category: 'security', tier: 2 }); // idx 10
    const unknown = src({ name: 'Unknown', category: 'not_a_category', tier: 1 });
    expect(sortSources([unknown, known]).map((s) => s.name)).toEqual(['Known', 'Unknown']);
  });

  it('is stable for equal keys (same category/tier/name keeps input order)', () => {
    const a1 = src({ name: 'Same', category: 'news', tier: 2, homepage: 'https://a1' });
    const a2 = src({ name: 'Same', category: 'news', tier: 2, homepage: 'https://a2' });
    expect(sortSources([a2, a1]).map((s) => s.homepage)).toEqual(['https://a2', 'https://a1']);
  });

  it('returns a new array and does not mutate the input', () => {
    const a = src({ name: 'A', category: 'devtools', tier: 1 });
    const b = src({ name: 'B', category: 'aggregators', tier: 1 });
    const input = [a, b];
    const out = sortSources(input);
    expect(out).not.toBe(input);
    expect(input).toEqual([a, b]); // untouched
    expect(out.map((s) => s.name)).toEqual(['B', 'A']);
  });

  it('every declared category has a defined position (sanity on the index map)', () => {
    // Build one source per category and confirm the sort respects declared order.
    const built = SOURCE_CATEGORIES.map((c, i) =>
      src({ name: `n${String(i).padStart(2, '0')}`, category: c, tier: 1 })
    );
    const shuffled = [...built].reverse();
    expect(sortSources(shuffled).map((s) => s.category)).toEqual([...SOURCE_CATEGORIES]);
  });
});

describe('parseSources() — valid input', () => {
  const valid = [
    { name: 'A', homepage: 'https://a.com', category: 'news', tier: 1, paywalled: false },
    { name: 'B', homepage: 'http://b.com', category: 'devtools', tier: 3, paywalled: true, feed: null },
    {
      name: 'C',
      homepage: 'https://c.com',
      category: 'research',
      tier: 2,
      paywalled: false,
      feed: 'https://c.com/rss',
    },
  ];

  it('parses a well-formed array of sources', () => {
    const parsed = parseSources(valid);
    expect(parsed).toHaveLength(3);
  });

  it('accepts feed as an http url, null, or absent', () => {
    const parsed = parseSources(valid);
    expect(parsed[0].feed).toBeUndefined(); // absent (optional)
    expect(parsed[1].feed).toBeNull(); // explicit null (nullable)
    expect(parsed[2].feed).toBe('https://c.com/rss'); // http url
  });

  it('accepts both http and https homepages', () => {
    const parsed = parseSources(valid);
    expect(parsed[0].homepage).toBe('https://a.com');
    expect(parsed[1].homepage).toBe('http://b.com');
  });

  it('accepts each tier 1, 2, 3', () => {
    for (const tier of [1, 2, 3] as const) {
      expect(() =>
        parseSources([{ name: 'T', homepage: 'https://t.com', category: 'news', tier, paywalled: false }])
      ).not.toThrow();
    }
  });

  it('accepts an empty array', () => {
    expect(parseSources([])).toEqual([]);
  });
});

describe('parseSources() — invalid input', () => {
  const base = { name: 'X', homepage: 'https://x.com', category: 'news', tier: 1, paywalled: false };

  it('rejects tier 4 (and other non-1/2/3 tiers)', () => {
    expect(() => parseSources([{ ...base, tier: 4 }])).toThrow();
    expect(() => parseSources([{ ...base, tier: 0 }])).toThrow();
    expect(() => parseSources([{ ...base, tier: '1' }])).toThrow();
  });

  it('rejects an unknown category', () => {
    expect(() => parseSources([{ ...base, category: 'bogus' }])).toThrow();
  });

  it('rejects a non-http(s) homepage', () => {
    expect(() => parseSources([{ ...base, homepage: 'ftp://x.com' }])).toThrow();
    expect(() => parseSources([{ ...base, homepage: 'x.com' }])).toThrow();
  });

  it('rejects a non-http(s) feed', () => {
    expect(() => parseSources([{ ...base, feed: 'ftp://x.com/feed' }])).toThrow();
    expect(() => parseSources([{ ...base, feed: 'not-a-url' }])).toThrow();
  });

  it('rejects a missing required field', () => {
    const { paywalled, ...noPaywall } = base;
    expect(() => parseSources([noPaywall])).toThrow();
    const { name, ...noName } = base;
    expect(() => parseSources([noName])).toThrow();
  });

  it('rejects an empty name', () => {
    expect(() => parseSources([{ ...base, name: '' }])).toThrow();
  });

  it('rejects a non-array payload', () => {
    expect(() => parseSources(base)).toThrow();
    expect(() => parseSources(null)).toThrow();
  });
});
