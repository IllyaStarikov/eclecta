#!/usr/bin/env node
/**
 * Feed discovery + liveness checker for source lists.
 *
 *   node scripts/feed-health.mjs <input.json> [output.json] [--concurrency=24] [--timeout=12000] [--freshness]
 *
 * Reads a JSON array of objects that each have a `homepage`, fetches each
 * homepage with a browser-like UA, and enriches every object with:
 *   feed        discovered RSS/Atom URL (or null)
 *   http_status final status after redirects (or 0 on network error)
 *   alive       false only for hard-dead hosts (DNS/refused/404/410/5xx);
 *               403/401/429 are treated as alive-but-bot-blocked (kept)
 *   last_post   ISO date of newest feed item when --freshness and a feed exists
 *   note        short diagnostic
 *
 * Defaults to src/data/sources.json -> stdout when no args. Network-bound;
 * run manually, never in CI.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flags = Object.fromEntries(
  args.filter((a) => a.startsWith('--')).map((a) => {
    const [k, v] = a.replace(/^--/, '').split('=');
    return [k, v ?? true];
  })
);
const positional = args.filter((a) => !a.startsWith('--'));
const INPUT = positional[0] ? resolve(positional[0]) : resolve(__dirname, '../src/data/sources.json');
const OUTPUT = positional[1] ? resolve(positional[1]) : null;
const CONCURRENCY = Number(flags.concurrency ?? 24);
const TIMEOUT = Number(flags.timeout ?? 12000);
const FRESHNESS = Boolean(flags.freshness);

const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';

const FEED_PROBES = [
  '/feed',
  '/feed/',
  '/rss',
  '/rss.xml',
  '/index.xml',
  '/atom.xml',
  '/feed.xml',
  '/feeds/posts/default?alt=rss',
  '/blog/rss.xml',
  '/blog/feed',
];

async function get(url, { method = 'GET' } = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), TIMEOUT);
  try {
    const res = await fetch(url, {
      method,
      redirect: 'follow',
      signal: ctrl.signal,
      headers: { 'user-agent': UA, accept: '*/*' },
    });
    return res;
  } finally {
    clearTimeout(t);
  }
}

function looksLikeFeed(text) {
  const head = text.slice(0, 600).toLowerCase();
  return (
    head.includes('<rss') ||
    head.includes('<feed') ||
    head.includes('<?xml') && (head.includes('rss') || head.includes('atom') || head.includes('rdf'))
  );
}

function feedLinksFromHtml(html, baseUrl) {
  const out = [];
  const re = /<link\b[^>]*>/gi;
  let m;
  while ((m = re.exec(html))) {
    const tag = m[0];
    if (!/rel\s*=\s*["']?alternate/i.test(tag)) continue;
    if (!/type\s*=\s*["']?application\/(rss|atom|rdf)\+xml/i.test(tag)) continue;
    const href = tag.match(/href\s*=\s*["']([^"']+)["']/i);
    if (href) {
      try {
        out.push(new URL(href[1], baseUrl).href);
      } catch {
        /* ignore */
      }
    }
  }
  return out;
}

function platformFeed(homepage) {
  let u;
  try {
    u = new URL(homepage);
  } catch {
    return null;
  }
  const host = u.hostname.replace(/^www\./, '');
  const path = u.pathname.replace(/\/+$/, '');
  if (host.endsWith('substack.com')) return `${u.origin}/feed`;
  if (host.endsWith('.blogspot.com')) return `${u.origin}/feeds/posts/default?alt=rss`;
  if (host === 'medium.com' && path.startsWith('/@')) return `https://medium.com${path}/feed`;
  if (host.endsWith('.medium.com')) return `${u.origin}/feed`;
  if (host === 'github.com') {
    const parts = path.split('/').filter(Boolean);
    if (parts.length >= 2) return `https://github.com/${parts[0]}/${parts[1]}/releases.atom`;
  }
  if (host === 'reddit.com' && path.startsWith('/r/')) return `${u.origin}${path}/.rss`;
  if (host === 'news.ycombinator.com') return 'https://news.ycombinator.com/rss';
  if (host.endsWith('bearblog.dev')) return `${u.origin}/feed/`;
  return null;
}

async function newestDate(feedUrl) {
  try {
    const res = await get(feedUrl);
    if (!res.ok) return null;
    const xml = await res.text();
    const dates = [];
    for (const re of [
      /<pubDate>([^<]+)<\/pubDate>/gi,
      /<updated>([^<]+)<\/updated>/gi,
      /<published>([^<]+)<\/published>/gi,
      /<dc:date>([^<]+)<\/dc:date>/gi,
    ]) {
      let m;
      while ((m = re.exec(xml))) {
        const d = new Date(m[1]);
        if (!isNaN(d)) dates.push(d);
      }
    }
    if (!dates.length) return null;
    return new Date(Math.max(...dates.map((d) => +d))).toISOString().slice(0, 10);
  } catch {
    return null;
  }
}

async function checkOne(src) {
  const out = { ...src, feed: null, http_status: 0, alive: true, last_post: null, note: '' };
  const provided = src.feed || null; // verify rather than trust (may be hallucinated)
  // 1) platform-derived feed (high confidence, no homepage fetch needed)
  const pf = platformFeed(src.homepage);
  // 2) fetch homepage for status + <link> discovery
  let html = '';
  try {
    const res = await get(src.homepage);
    out.http_status = res.status;
    if (res.status === 404 || res.status === 410 || res.status >= 500) {
      out.alive = false;
      out.note = `dead ${res.status}`;
    }
    const ctype = res.headers.get('content-type') || '';
    if (res.ok && ctype.includes('html')) html = await res.text();
  } catch (e) {
    const msg = String(e?.cause?.code || e?.name || e?.message || e);
    if (/ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ERR_TLS|certificate/i.test(msg)) {
      out.alive = false;
    }
    out.note = msg.slice(0, 40);
  }
  // resolve feed: verify a provided feed first, then <link> discovery, platform, probes
  if (!out.feed && provided) {
    try {
      const r = await get(provided);
      if (r.ok) {
        const txt = (await r.text()).trim();
        if (looksLikeFeed(txt)) out.feed = provided;
      }
    } catch {
      /* ignore */
    }
  }
  if (!out.feed && html) {
    const links = feedLinksFromHtml(html, src.homepage);
    if (links.length) out.feed = links[0];
  }
  if (!out.feed && pf) {
    try {
      const r = await get(pf);
      if (r.ok) {
        const txt = (await r.text()).trim();
        if (looksLikeFeed(txt)) out.feed = pf;
      }
    } catch {
      /* ignore */
    }
  }
  if (!out.feed) {
    let origin;
    try {
      origin = new URL(src.homepage).origin;
    } catch {
      origin = null;
    }
    if (origin) {
      for (const p of FEED_PROBES) {
        try {
          const r = await get(origin + p);
          if (r.ok) {
            const txt = (await r.text()).trim();
            if (looksLikeFeed(txt)) {
              out.feed = origin + p;
              break;
            }
          }
        } catch {
          /* ignore */
        }
      }
    }
  }
  // normalize pseudo-schemes (feed://, protocol-relative) to https
  if (out.feed) {
    out.feed = out.feed.replace(/^feed:\/\//i, 'https://').replace(/^\/\//, 'https://');
    if (!/^https?:\/\//i.test(out.feed)) out.feed = null;
  }
  if (FRESHNESS && out.feed) out.last_post = await newestDate(out.feed);
  return out;
}

async function pool(items, n, fn, onProgress) {
  const results = new Array(items.length);
  let i = 0;
  let done = 0;
  async function worker() {
    while (i < items.length) {
      const idx = i++;
      results[idx] = await fn(items[idx], idx);
      done++;
      if (onProgress && done % 25 === 0) onProgress(done, items.length);
    }
  }
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, worker));
  return results;
}

async function main() {
  const data = JSON.parse(readFileSync(INPUT, 'utf8'));
  process.stderr.write(`feed-health: ${data.length} sources, concurrency=${CONCURRENCY}, timeout=${TIMEOUT}ms\n`);
  const enriched = await pool(data, CONCURRENCY, checkOne, (d, n) =>
    process.stderr.write(`  ${d}/${n}\r`)
  );
  process.stderr.write('\n');
  const dead = enriched.filter((s) => !s.alive);
  const withFeed = enriched.filter((s) => s.feed);
  process.stderr.write(
    `done: ${withFeed.length}/${enriched.length} have feeds, ${dead.length} hard-dead\n`
  );
  const json = JSON.stringify(enriched, null, 2);
  if (OUTPUT) writeFileSync(OUTPUT, json);
  else process.stdout.write(json + '\n');
}

main();
