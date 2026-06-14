#!/usr/bin/env node
/**
 * Read-only duplicate reporter for src/data/sources.json.
 *
 *   node scripts/dedup-sources.mjs
 *
 * Reports, and exits non-zero if any are found:
 *   - canonical-URL duplicate groups (the real dedup key)
 *   - normalized-name duplicate groups (same source, different URL)
 *   - same registrable-domain clusters (candidate near-duplicates to review)
 *
 * For each duplicate group it names the proposed canonical winner using the
 * precedence: lowest tier → has feed → more-specific homepage path → cleaner
 * name. The dedup/normalize logic mirrors src/lib/sources.ts (kept in sync by
 * tests/unit/sources.test.ts, which uses the TS implementation).
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCES = resolve(__dirname, '../src/data/sources.json');

const TRACKING = /^(utm_|fbclid$|gclid$|mc_|ref$|source$|igshid$)/i;

export function canonicalUrl(input) {
  const raw = (input ?? '').trim();
  try {
    const u = new URL(raw);
    const host = u.hostname.toLowerCase().replace(/^www\./, '');
    const path = u.pathname.replace(/\/+$/, '');
    const kept = [];
    for (const [k, v] of [...u.searchParams.entries()].sort()) {
      if (TRACKING.test(k)) continue;
      kept.push(`${k}=${v}`);
    }
    const qs = kept.length ? `?${kept.join('&')}` : '';
    return `${host}${path}${qs}`;
  } catch {
    return raw
      .toLowerCase()
      .replace(/^https?:\/\//, '')
      .replace(/^www\./, '')
      .replace(/#.*$/, '')
      .replace(/\?.*$/, '')
      .replace(/\/+$/, '');
  }
}

export function normalizeName(name) {
  return name
    .toLowerCase()
    .replace(/\([^)]*\)/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

const MULTI_TLD = new Set(['co', 'com', 'org', 'net', 'ac', 'gov', 'edu']);
function registrableDomain(homepage) {
  try {
    const host = new URL(homepage).hostname.toLowerCase().replace(/^www\./, '');
    const parts = host.split('.');
    if (parts.length <= 2) return host;
    const secondLast = parts[parts.length - 2];
    return MULTI_TLD.has(secondLast)
      ? parts.slice(-3).join('.')
      : parts.slice(-2).join('.');
  } catch {
    return homepage;
  }
}

/** Lower is better. */
function rank(s) {
  return [
    s.tier, // 1 flagship beats 3
    s.feed ? 0 : 1, // having a feed beats not
    -((() => {
      try {
        return new URL(s.homepage).pathname.replace(/\/+$/, '').length;
      } catch {
        return 0;
      }
    })()), // more-specific path wins
    s.name.length, // shorter, cleaner name as final tiebreak
  ];
}
function winner(group) {
  return [...group].sort((a, b) => {
    const ra = rank(a);
    const rb = rank(b);
    for (let i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] - rb[i];
    return 0;
  })[0];
}

function groupBy(items, keyFn) {
  const m = new Map();
  for (const it of items) {
    const k = keyFn(it);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(it);
  }
  return m;
}

function main() {
  const sources = JSON.parse(readFileSync(SOURCES, 'utf8'));
  let problems = 0;

  console.log(`sources.json: ${sources.length} entries\n`);

  const byUrl = groupBy(sources, (s) => canonicalUrl(s.homepage));
  const urlDups = [...byUrl.entries()].filter(([, g]) => g.length > 1);
  console.log(`== canonical-URL duplicate groups: ${urlDups.length} ==`);
  for (const [key, g] of urlDups.sort((a, b) => b[1].length - a[1].length)) {
    const w = winner(g);
    console.log(`  [${g.length}x] ${key}`);
    for (const s of g) {
      const mark = s === w ? 'KEEP' : 'drop';
      console.log(`      ${mark}  t${s.tier} ${s.category.padEnd(16)} ${s.name}  <${s.homepage}>`);
    }
  }
  problems += urlDups.length;

  const byName = groupBy(sources, (s) => normalizeName(s.name));
  const nameDups = [...byName.entries()].filter(([, g]) => g.length > 1);
  console.log(`\n== normalized-name duplicate groups: ${nameDups.length} ==`);
  for (const [key, g] of nameDups.sort((a, b) => b[1].length - a[1].length)) {
    console.log(`  [${g.length}x] "${key}"`);
    for (const s of g) console.log(`      t${s.tier} ${s.category.padEnd(16)} ${s.name}  <${s.homepage}>`);
  }
  problems += nameDups.length;

  const byDomain = groupBy(sources, (s) => registrableDomain(s.homepage));
  const domainClusters = [...byDomain.entries()].filter(([, g]) => {
    if (g.length < 2) return false;
    // only flag if not already a pure canonical-URL dup
    const urls = new Set(g.map((s) => canonicalUrl(s.homepage)));
    return urls.size > 1;
  });
  console.log(`\n== same registrable-domain clusters (review for near-dups): ${domainClusters.length} ==`);
  for (const [dom, g] of domainClusters.sort((a, b) => b[1].length - a[1].length).slice(0, 60)) {
    console.log(`  [${g.length}x] ${dom}`);
    for (const s of g) console.log(`      t${s.tier} ${s.category.padEnd(16)} ${s.name}  <${s.homepage}>`);
  }

  console.log(
    `\nSUMMARY: ${urlDups.length} url-dup groups, ${nameDups.length} name-dup groups, ${domainClusters.length} domain clusters`
  );
  // Exit non-zero only on hard duplicates (url/name), not soft domain clusters.
  process.exit(urlDups.length + nameDups.length > 0 ? 1 : 0);
}

main();
