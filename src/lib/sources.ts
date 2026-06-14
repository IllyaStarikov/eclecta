/**
 * Contract + helpers for src/data/sources.json — the curated source roll the
 * /sources page renders. Unlike picks/stats/channels (pipeline-owned), this
 * file is site-owned and hand-curated; no `signal:` commit writes it. These
 * helpers keep it deduplicated, well-typed, and deterministically ordered so
 * the diff stays reviewable and a malformed edit fails `npm run test:unit`.
 *
 * Source contract:
 *   name       REQUIRED — canonical publication name
 *   homepage   REQUIRED — http(s) landing page
 *   category   REQUIRED — one of SOURCE_CATEGORIES (the /sources grouping)
 *   tier       REQUIRED — 1 flagship · 2 core · 3 secondary
 *   paywalled  REQUIRED — true for a hard paywall
 *   feed       OPTIONAL — RSS/Atom URL when one was discovered (null when none)
 */
import { z } from 'zod';

/* The 12 source categories. Must match the SRC_CAT_LABEL map in
 * src/pages/sources.astro. These are NOT the 6 pick-taxonomy categories in
 * src/lib/taxonomy.ts — do not conflate the two. */
export const SOURCE_CATEGORIES = [
  'aggregators',
  'ai_companies',
  'devtools',
  'expert_blogs',
  'hardware_science',
  'news',
  'newsletters',
  'physics',
  'research',
  'science',
  'security',
  'tech_news',
] as const;

export type SourceCategory = (typeof SOURCE_CATEGORIES)[number];

const httpUrl = z
  .string()
  .regex(/^https?:\/\/.+/i, 'must be an http(s) URL');

export const sourceSchema = z.object({
  name: z.string().min(1),
  homepage: httpUrl,
  category: z.enum(SOURCE_CATEGORIES),
  tier: z.union([z.literal(1), z.literal(2), z.literal(3)]),
  paywalled: z.boolean(),
  feed: httpUrl.nullable().optional(),
});

export const sourcesSchema = z.array(sourceSchema);
export type Source = z.infer<typeof sourceSchema>;

export function parseSources(data: unknown): Source[] {
  return sourcesSchema.parse(data);
}

/**
 * Stable dedup key for a URL. Lowercases host, drops the scheme, a leading
 * `www.`, the fragment, trailing slashes, and tracking query params — but KEEPS
 * the path, so `host.com/blog` and `host.com` are distinct sources while
 * `https://www.host.com/blog/` and `http://host.com/blog` collapse to one.
 */
export function canonicalUrl(input: string): string {
  const raw = (input ?? '').trim();
  const TRACKING = /^(utm_|fbclid$|gclid$|mc_|ref$|source$|igshid$)/i;
  try {
    const u = new URL(raw);
    const host = u.hostname.toLowerCase().replace(/^www\./, '');
    const path = u.pathname.replace(/\/+$/, '');
    const kept: string[] = [];
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

/** Normalized name key for catching same-source-different-URL duplicates. */
export function normalizeName(name: string): string {
  return name
    .toLowerCase()
    .replace(/\([^)]*\)/g, '') // drop parentheticals e.g. "(MLST)"
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

/**
 * Deterministic order: category, then tier (flagship first), then name. Keeps
 * the JSON diff stable across re-runs and makes the file scannable by section.
 */
export function sortSources(sources: Source[]): Source[] {
  const catIndex = (c: string) => {
    const i = (SOURCE_CATEGORIES as readonly string[]).indexOf(c);
    return i === -1 ? SOURCE_CATEGORIES.length : i;
  };
  return [...sources].sort(
    (a, b) =>
      catIndex(a.category) - catIndex(b.category) ||
      a.tier - b.tier ||
      a.name.toLowerCase().localeCompare(b.name.toLowerCase())
  );
}
