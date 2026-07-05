import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';

// Both scripts resolve their data file as `<script dir>/../src/data/sources.json`.
// To drive them hermetically we copy the script into a throwaway tmp root that
// carries its OWN src/data/sources.json fixture. dedup reads it read-only; merge
// reads AND writes it in place — neither ever touches the real repo data file.

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const DEDUP_SRC = resolve(REPO_ROOT, 'scripts/dedup-sources.mjs');
const MERGE_SRC = resolve(REPO_ROOT, 'scripts/merge-sources.mjs');

const roots: string[] = [];

/** Build a tmp root: <root>/scripts/<name>.mjs + <root>/src/data/sources.json */
function stage(scriptSrc: string, sourcesContent: string) {
  const root = mkdtempSync(join(tmpdir(), 'scripts-test-'));
  roots.push(root);
  mkdirSync(join(root, 'scripts'), { recursive: true });
  mkdirSync(join(root, 'src', 'data'), { recursive: true });
  const scriptDst = join(root, 'scripts', scriptSrc.split('/').pop()!);
  copyFileSync(scriptSrc, scriptDst);
  const sourcesPath = join(root, 'src', 'data', 'sources.json');
  writeFileSync(sourcesPath, sourcesContent);
  return { root, scriptDst, sourcesPath };
}

function run(scriptDst: string, args: string[] = []) {
  return spawnSync('node', [scriptDst, ...args], { encoding: 'utf8' });
}

/** A minimal valid source record. */
function src(over: Record<string, unknown> = {}) {
  return {
    name: 'A Source',
    homepage: 'https://a.example',
    category: 'news',
    tier: 3,
    paywalled: false,
    feed: null,
    ...over,
  };
}

afterEach(() => {
  // Best-effort cleanup; tmpdir entries are disposable regardless.
  for (const r of roots.splice(0)) {
    spawnSync('rm', ['-rf', r]);
  }
});

describe('dedup-sources.mjs (read-only duplicate reporter)', () => {
  it('exits 0 on a clean set with no url/name/domain duplicates', () => {
    const sources = JSON.stringify([
      src({ name: 'Alpha', homepage: 'https://alpha.example' }),
      src({ name: 'Beta', homepage: 'https://beta.example' }),
    ]);
    const { scriptDst } = stage(DEDUP_SRC, sources);
    const r = run(scriptDst);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('sources.json: 2 entries');
    expect(r.stdout).toContain('canonical-URL duplicate groups: 0');
    expect(r.stdout).toContain('normalized-name duplicate groups: 0');
    expect(r.stdout).toContain('SUMMARY: 0 url-dup groups, 0 name-dup groups');
  });

  it('exits 1 and names the tier winner on a canonical-URL duplicate', () => {
    // www + trailing slash + http/https all canonicalize to `dup.example`.
    const sources = JSON.stringify([
      src({ name: 'DropMe', homepage: 'http://dup.example', tier: 3 }),
      src({ name: 'KeepMe', homepage: 'https://www.dup.example/', tier: 1 }),
    ]);
    const { scriptDst } = stage(DEDUP_SRC, sources);
    const r = run(scriptDst);
    expect(r.status).toBe(1);
    expect(r.stdout).toContain('canonical-URL duplicate groups: 1');
    // lower tier wins → KEEP marks the tier-1 entry, drop marks tier-3.
    expect(r.stdout).toMatch(/KEEP\s+t1\s+news\s+KeepMe/);
    expect(r.stdout).toMatch(/drop\s+t3\s+news\s+DropMe/);
  });

  it('breaks a same-tier url-dup tie in favour of the entry with a feed', () => {
    const sources = JSON.stringify([
      src({ name: 'NoFeed', homepage: 'https://tie.example', tier: 2, feed: null }),
      src({ name: 'HasFeed', homepage: 'https://tie.example/', tier: 2, feed: 'https://tie.example/rss' }),
    ]);
    const { scriptDst } = stage(DEDUP_SRC, sources);
    const r = run(scriptDst);
    expect(r.status).toBe(1);
    expect(r.stdout).toMatch(/KEEP\s+t2\s+news\s+HasFeed/);
    expect(r.stdout).toMatch(/drop\s+t2\s+news\s+NoFeed/);
  });

  it('exits 1 on a normalized-name duplicate across different domains', () => {
    // "The Verge" and "the-verge" both normalize to "the verge"; distinct
    // domains keep it out of the url-dup and domain-cluster buckets.
    const sources = JSON.stringify([
      src({ name: 'The Verge', homepage: 'https://theverge.com' }),
      src({ name: 'the-verge', homepage: 'https://theverge.net' }),
    ]);
    const { scriptDst } = stage(DEDUP_SRC, sources);
    const r = run(scriptDst);
    expect(r.status).toBe(1);
    expect(r.stdout).toContain('normalized-name duplicate groups: 1');
    expect(r.stdout).toContain('canonical-URL duplicate groups: 0');
  });

  it('reports a soft same-domain cluster WITHOUT failing (exit 0)', () => {
    // Same registrable domain, different paths (=> different canonical URLs)
    // and different names: a review-only cluster, not a hard duplicate.
    const sources = JSON.stringify([
      src({ name: 'Example Blog', homepage: 'https://example.com/blog' }),
      src({ name: 'Example News', homepage: 'https://example.com/news' }),
    ]);
    const { scriptDst } = stage(DEDUP_SRC, sources);
    const r = run(scriptDst);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('canonical-URL duplicate groups: 0');
    expect(r.stdout).toContain('normalized-name duplicate groups: 0');
    expect(r.stdout).toContain('same registrable-domain clusters (review for near-dups): 1');
  });

  it('exits non-zero with an error on malformed sources.json', () => {
    const { scriptDst } = stage(DEDUP_SRC, '{ this is not json');
    const r = run(scriptDst);
    expect(r.status).not.toBe(0);
    expect(r.stderr).toMatch(/JSON|SyntaxError|Unexpected/);
  });
});

describe('merge-sources.mjs (merge candidates into sources.json)', () => {
  const cand = (over: Record<string, unknown> = {}) => ({
    name: 'Cand',
    homepage: 'https://cand.example',
    category: 'devtools',
    tier: 3,
    paywalled: false,
    feed: null,
    alive: true,
    ...over,
  });

  /** Stage merge with an `existing` array; write a candidates file; return runner. */
  function stageMerge(existing: unknown[], candidates: unknown) {
    const { root, scriptDst, sourcesPath } = stage(MERGE_SRC, JSON.stringify(existing));
    const candPath = join(root, 'candidates.json');
    writeFileSync(candPath, typeof candidates === 'string' ? candidates : JSON.stringify(candidates));
    return { scriptDst, sourcesPath, candPath, read: () => readFileSync(sourcesPath, 'utf8') };
  }

  it('exits 2 with a usage message when no candidates path is given', () => {
    const { scriptDst } = stageMerge([src()], []);
    const r = run(scriptDst); // no argv[2]
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('usage: node scripts/merge-sources.mjs');
  });

  it('adds a valid candidate, writes the 6-field contract, and sorts by category then name', () => {
    const existing = [src({ name: 'Zeta', homepage: 'https://zeta.example', category: 'news', tier: 2 })];
    const candidates = [
      cand({ name: 'Alpha', homepage: 'https://alpha.example', category: 'aggregators', tier: 3, paywalled: true }),
    ];
    const { scriptDst, read, candPath } = stageMerge(existing, candidates);
    const r = run(scriptDst, [candPath]);
    expect(r.status).toBe(0);

    const out = JSON.parse(read());
    expect(out).toHaveLength(2);
    // sorted: aggregators(0) before news(5)
    expect(out.map((s: any) => s.name)).toEqual(['Alpha', 'Zeta']);
    // exact 6-field contract, no leftover `alive`
    for (const s of out) {
      expect(Object.keys(s).sort()).toEqual(['category', 'feed', 'homepage', 'name', 'paywalled', 'tier']);
    }
    const alpha = out.find((s: any) => s.name === 'Alpha');
    expect(alpha).toMatchObject({ category: 'aggregators', tier: 3, paywalled: true, feed: null });
    expect(r.stdout).toContain('added 1');
  });

  it('normalizes feed:// and scheme-relative feeds, nulls out non-URL feeds', () => {
    const existing = [src({ name: 'Base', homepage: 'https://base.example' })];
    const candidates = [
      cand({ name: 'FeedProto', homepage: 'https://f1.example', feed: 'feed://f1.example/rss' }),
      cand({ name: 'SchemeRel', homepage: 'https://f2.example', feed: '//f2.example/rss' }),
      cand({ name: 'Garbage', homepage: 'https://f3.example', feed: 'not-a-url' }),
    ];
    const { scriptDst, read, candPath } = stageMerge(existing, candidates);
    const r = run(scriptDst, [candPath]);
    expect(r.status).toBe(0);
    const out = JSON.parse(read());
    const by = (n: string) => out.find((s: any) => s.name === n);
    expect(by('FeedProto').feed).toBe('https://f1.example/rss');
    expect(by('SchemeRel').feed).toBe('https://f2.example/rss');
    expect(by('Garbage').feed).toBeNull();
  });

  it('rejects dead, bad-category, and malformed candidates (counts them, adds none)', () => {
    const existing = [src({ name: 'Keep', homepage: 'https://keep.example' })];
    const candidates = [
      cand({ name: 'Dead', homepage: 'https://dead.example', alive: false }),
      cand({ name: 'BadCat', homepage: 'https://badcat.example', category: 'sportsball' }),
      cand({ name: 'NoHttp', homepage: 'ftp://nohttp.example' }),
      cand({ name: 'NoHome', homepage: '' }),
    ];
    const { scriptDst, read, candPath } = stageMerge(existing, candidates);
    const r = run(scriptDst, [candPath]);
    expect(r.status).toBe(0);
    const out = JSON.parse(read());
    expect(out).toHaveLength(1);
    expect(out[0].name).toBe('Keep');
    expect(r.stdout).toContain('added 0');
    expect(r.stdout).toMatch(/dead 1/);
    expect(r.stdout).toMatch(/badCat 1/);
    expect(r.stdout).toMatch(/malformed 2/); // ftp scheme + empty homepage
  });

  it('dedupes candidates against existing entries by canonical URL and normalized name', () => {
    const existing = [
      src({ name: 'Zeta News', homepage: 'https://zeta.example', category: 'news', tier: 2 }),
    ];
    const candidates = [
      // url dup: www + trailing slash canonicalize to the existing homepage
      cand({ name: 'Totally Different', homepage: 'https://www.zeta.example/', category: 'news' }),
      // name dup: "zeta-news" normalizes to "zeta news"
      cand({ name: 'zeta-news', homepage: 'https://elsewhere.example', category: 'news' }),
    ];
    const { scriptDst, read, candPath } = stageMerge(existing, candidates);
    const r = run(scriptDst, [candPath]);
    expect(r.status).toBe(0);
    const out = JSON.parse(read());
    expect(out).toHaveLength(1);
    expect(r.stdout).toContain('added 0');
    expect(r.stdout).toMatch(/dup 2/);
  });

  it('caps tier-1 at 12%, demoting the longest-named newly-added flagships first', () => {
    // 9 existing tier-3 + 3 new tier-1 => merged 12, cap = floor(12*0.12) = 1.
    // need = 3 - 1 = 2 demotions; demotable sorted by name length desc, so the
    // two longest-named candidates drop to tier 2 and the shortest stays tier 1.
    const existing = Array.from({ length: 9 }, (_, i) =>
      src({ name: `Ex${i}`, homepage: `https://ex${i}.example`, category: 'news', tier: 3 }),
    );
    const candidates = [
      cand({ name: 'AAAA', homepage: 'https://a.example', category: 'devtools', tier: 1 }),
      cand({ name: 'BBBBB', homepage: 'https://b.example', category: 'devtools', tier: 1 }),
      cand({ name: 'CCCCCC', homepage: 'https://c.example', category: 'devtools', tier: 1 }),
    ];
    const { scriptDst, read, candPath } = stageMerge(existing, candidates);
    const r = run(scriptDst, [candPath]);
    expect(r.status).toBe(0);
    const out = JSON.parse(read());
    expect(out).toHaveLength(12);
    const tier1 = out.filter((s: any) => s.tier === 1);
    expect(tier1).toHaveLength(1);
    expect(tier1[0].name).toBe('AAAA'); // shortest name kept as flagship
    expect(out.find((s: any) => s.name === 'BBBBB').tier).toBe(2);
    expect(out.find((s: any) => s.name === 'CCCCCC').tier).toBe(2);
  });

  it('is idempotent: re-running with the same candidates changes nothing', () => {
    const existing = [src({ name: 'Zeta', homepage: 'https://zeta.example', category: 'news', tier: 2 })];
    const candidates = [cand({ name: 'Alpha', homepage: 'https://alpha.example', category: 'aggregators', tier: 3 })];
    const { scriptDst, read, candPath } = stageMerge(existing, candidates);

    const r1 = run(scriptDst, [candPath]);
    expect(r1.status).toBe(0);
    const afterFirst = read();

    const r2 = run(scriptDst, [candPath]);
    expect(r2.status).toBe(0);
    const afterSecond = read();

    expect(afterSecond).toBe(afterFirst); // byte-identical
    expect(r2.stdout).toContain('added 0');
  });

  it('exits non-zero with an error on a malformed candidates file', () => {
    const { scriptDst, candPath } = stageMerge([src()], '{ not valid json');
    const r = run(scriptDst, [candPath]);
    expect(r.status).not.toBe(0);
    expect(r.stderr).toMatch(/JSON|SyntaxError|Unexpected/);
  });
});
