/**
 * Shared feed machinery — the ONE place feeds are defined.
 * Pages (the /feeds/ directory) and every rss.xml.js endpoint consume the
 * FEEDS registry below; item HTML is built here so a pick carries its full
 * record (why, notes, summary, primary + free links, surfaces) in RSS
 * regardless of any on-site display preference.
 */
import { CATEGORIES } from './taxonomy';
import { site, KINDS, KIND_LABEL, type DigestKind } from '../site';

/** Digest feeds serve the newest N editions; dailies accumulate forever. */
export const FEED_DIGEST_CAP = 50;

/** Minimal XML/HTML escaping for feed content. */
export function esc(s: unknown): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

interface FeedSurface {
  url: string;
  name: string;
  points?: number | null;
  comments?: number | null;
}

export interface FeedPick {
  title: string;
  why?: string;
  notes?: string[];
  summary?: string;
  source_url: string;
  free_link?: string | null;
  paywalled?: boolean;
  surfaces?: FeedSurface[];
}

/** Primary link first, always: the source, not the aggregator. */
export function pickPrimaryLink(p: FeedPick): string {
  return p.source_url;
}

/** Backup free read, when it exists and differs from the primary. */
export function pickFreeLink(p: FeedPick): string | null {
  return p.free_link && p.free_link !== p.source_url ? p.free_link : null;
}

/** Full item body for a pick — everything the site knows, in the feed. */
export function pickItemHtml(p: FeedPick): string {
  const primary = pickPrimaryLink(p);
  const free = pickFreeLink(p);
  const parts: string[] = [];
  if (p.why) parts.push(`<p><strong>Why it matters:</strong> ${esc(p.why)}</p>`);
  if (p.notes && p.notes.length) {
    parts.push(
      `<p><strong>Notes</strong></p><ul>${p.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`
    );
  }
  if (p.summary) parts.push(`<p>${esc(p.summary)}</p>`);
  const links = [`<a href="${esc(primary)}">Primary source</a>`];
  if (p.paywalled) links.push('<em>paywalled</em>');
  if (free) links.push(`<a href="${esc(free)}">Free read</a>`);
  parts.push(`<p><strong>Read</strong> · ${links.join(' · ')}</p>`);
  if (p.surfaces && p.surfaces.length) {
    parts.push(
      `<p><strong>Surfaced on</strong> ${p.surfaces
        .map(
          (s) =>
            `<a href="${esc(s.url)}">${esc(s.name)}${s.points ? ` (${s.points})` : ''}${
              s.comments ? ` · ${s.comments}c` : ''
            }</a>`
        )
        .join(' · ')}</p>`
    );
  }
  return parts.join('');
}

export interface FeedDigest {
  kind: DigestKind;
  period: string;
  blurb: string;
}

/** Item body for a digest entry; `url` is the absolute on-site link. */
export function digestItemHtml(d: FeedDigest, url: string): string {
  return (
    `<p><em>${esc(d.blurb)}</em></p>` +
    `<p>${esc(KIND_LABEL[d.kind])} · ${esc(d.period)} · ` +
    `<a href="${esc(url)}">Read on ${esc(site.name)}</a></p>`
  );
}

/**
 * XSL that renders every raw feed as a human-readable "subscribe" page in the
 * browser (public/rss/styles.xsl), while the feed stays valid RSS for readers.
 * Passed as the `stylesheet` option to each rss.xml.js endpoint. Base-aware:
 * BASE_URL is '/' on eclecta.co, so this resolves to '/rss/styles.xsl'.
 */
export const FEED_STYLESHEET = import.meta.env.BASE_URL + 'rss/styles.xsl';

/* ── the registry ──────────────────────────────────────────────────────── */

export type FeedGroup = 'everything' | 'digests' | 'cadence' | 'category';

export interface FeedDef {
  slug: string;
  title: string;
  path: string;
  description: string;
  group: FeedGroup;
}

const CADENCE_DESC: Record<DigestKind, string> = {
  daily: 'The daily brief, weekday mornings. Just the editions — no individual picks.',
  weekly: 'The weekly digest, Fridays. The week, distilled to one read.',
  monthly: 'The monthly review. What actually moved.',
  quarterly: 'The quarterly report. The slower curves.',
  yearly: 'The year, in one edition.',
};

export const FEEDS: FeedDef[] = [
  {
    slug: 'everything',
    title: `${site.name} — everything`,
    path: '/rss.xml',
    description: 'Every curated pick and every digest edition, as they publish.',
    group: 'everything',
  },
  {
    slug: 'digests',
    title: `${site.name} — digests`,
    path: '/digests/rss.xml',
    description: 'All editions, daily brief to the year in review. No individual picks.',
    group: 'digests',
  },
  ...KINDS.map(
    (k): FeedDef => ({
      slug: `digests-${k}`,
      title: `${site.name} — ${KIND_LABEL[k].toLowerCase()}`,
      path: `/digests/${k}/rss.xml`,
      description: CADENCE_DESC[k],
      group: 'cadence',
    })
  ),
  ...CATEGORIES.map(
    (c): FeedDef => ({
      slug: `cat-${c.slug}`,
      title: `${site.name} — ${c.name.toLowerCase()}`,
      path: `/${c.slug}/rss.xml`,
      description: c.blurb,
      group: 'category',
    })
  ),
];

/** Look up a feed by slug; throws on a typo so endpoints fail at build. */
export function getFeed(slug: string): FeedDef {
  const feed = FEEDS.find((f) => f.slug === slug);
  if (!feed) throw new Error(`Unknown feed slug: ${slug}`);
  return feed;
}
