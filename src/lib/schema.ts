/**
 * THE canonical contract for the pipeline's JSON exports (src/data/*.json).
 * The pipeline (signalpipe/publish.py) emits exactly this shape — no legacy
 * fields, no optional-when-it-feels-like-it. A malformed export fails
 * `npm run test:unit` and the build.
 *
 * Pick contract v1:
 *   source_url  REQUIRED — the primary link (original source; cluster
 *               canonical URL pre-fetch)
 *   free_link   null unless a distinct, legitimate free read exists
 *   surfaces[]  where the story surfaced: {url, name, points|null,
 *               comments|null}
 */
import { z } from 'zod';

/* ── picks.json ────────────────────────────────────────────────────────── */

export const surfaceSchema = z.object({
  url: z.string().min(1),
  name: z.string().min(1),
  points: z.number().nullable(),
  comments: z.number().nullable(),
});

export const pickSchema = z.object({
  id: z.number(),
  title: z.string().min(1),
  relevance: z.number(),
  score: z.number(),
  why: z.string(),
  notes: z.array(z.string()),
  summary: z.string(),
  channels: z.array(z.string()),
  // v2 taxonomy (pipeline-emitted; the front end derives them from title +
  // channels until the pipeline writes them natively — see lib/taxonomy.ts).
  // Optional during the transition; they become the contract once the pipeline
  // v2 export is the sole writer.
  category: z.string().optional(),
  subcategories: z.array(z.string()).optional(),
  story_id: z.string().optional(),
  state: z.enum(['confident', 'developing']).optional(),
  published_at: z.string().nullable().optional(),
  novelty: z.string().nullable(),
  audience: z.string().nullable(),
  source_url: z.string().min(1),
  read_kind: z.string().nullable(),
  free_link: z.string().nullable(),
  paywalled: z.boolean(),
  surfaces: z.array(surfaceSchema),
  sources_count: z.number(),
  first_seen: z.string().nullable(),
  curated_at: z.string(),
  model: z.string().nullable(),
});

export const picksSchema = z.array(pickSchema);
export type Pick = z.infer<typeof pickSchema>;

/* ── spotlight.json ────────────────────────────────────────────────────── */
/* Stories gaining traction across the internet right now: pipeline-selected
   clusters with unusual cross-surface breadth + velocity, curated or not.
   The file may not exist yet; the section hides when it is absent. */

export const spotlightItemSchema = z
  .object({
    story_id: z.string(),
    title: z.string().min(1),
    url: z.string().min(1).optional(),
    canonical_url: z.string().min(1).optional(),
    first_seen: z.string(),
    surface_count: z.number(),
    surfaces: z.array(surfaceSchema).default([]),
    velocity_hours: z.number().nullable().optional(),
    points: z.number().nullable().optional(),
    comments: z.number().nullable().optional(),
    score: z.number(),
    curated: z.boolean(),
    pick_id: z.number().nullable().optional(),
  })
  .refine((i) => i.url || i.canonical_url, { message: 'url or canonical_url required' });

/* Accept both a bare array and { generated_at, items } while the pipeline
   contract settles. */
export const spotlightFileSchema = z.union([
  z.array(spotlightItemSchema),
  z.object({
    generated_at: z.string().optional(),
    window_hours: z.number().optional(),
    items: z.array(spotlightItemSchema),
  }),
]);
export type SpotlightItem = z.infer<typeof spotlightItemSchema>;

/* ── channels.json ─────────────────────────────────────────────────────── */

export const channelSchema = z.object({
  slug: z.string().min(1),
  name: z.string().min(1),
  blurb: z.string(),
});

export const channelsSchema = z.array(channelSchema);
export type Channel = z.infer<typeof channelSchema>;

/* ── stats.json ────────────────────────────────────────────────────────── */

/* ── stats.json v2 coverage blocks ─────────────────────────────────────────
 * Emitted by pipeline publish.py::export_stats v2. ALL optional: the site
 * must build against either export generation (the deployed pipeline lags
 * the repo until Illya syncs it). coverage.astro renders a band only when
 * its block is present. */

export const dayPointSchema = z.object({
  d: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  items: z.number(),
  clusters: z.number(),
  curated: z.number(),
});
export type DayPoint = z.infer<typeof dayPointSchema>;

const funnelCountsSchema = z.object({
  items: z.number(),
  clusters: z.number(),
  fetched: z.number(),
  curated: z.number(),
  published: z.number(),
});
export type FunnelCounts = z.infer<typeof funnelCountsSchema>;

const scoreBucketsSchema = z.record(z.string(), z.number());

export const modelUsageSchema = z.object({
  scope: z.enum(['curation', 'digest']),
  model: z.string().min(1),
  backend: z.string().nullable(),
  count: z.number(),
  avg_relevance: z.number().nullable(),
});
export type ModelUsage = z.infer<typeof modelUsageSchema>;

export const statsSchema = z.object({
  generated_at: z.string(),
  site_name: z.string().optional(),
  sources: z.object({
    total: z.number(),
    enabled: z.number().optional(),
    verified: z.number(),
    by_category: z.record(z.string(), z.number()),
    by_tier: z.record(z.string(), z.number()),
  }),
  pipeline: z.object({
    items_total: z.number(),
    clusters_total: z.number(),
    curations_done: z.number(),
    items_7d: z.number().optional(),
    curated_7d: z.number().optional(),
    avg_relevance_7d: z.number().optional(),
  }),
  digests: z.object({
    total: z.number(),
    by_kind: z.record(z.string(), z.number()),
    latest: z
      .object({
        kind: z.string(),
        period: z.string(),
        title: z.string(),
        date: z.string(),
      })
      .nullable()
      .optional(),
  }),
  channels: z.array(
    z.object({
      slug: z.string(),
      picks_current: z.number(),
    })
  ),
  top_surfaces_7d: z.array(
    z.object({
      name: z.string(),
      clusters: z.number(),
    })
  ),
  // The pipeline's model stages. Pinned to the three the site renders by name
  // (about page, /coverage/) so a renamed or missing stage fails validation with a
  // clear message instead of a build-time TypeError; extra stages still pass.
  models: z
    .object({
      triage: z.string(),
      deep: z.string(),
      digest: z.string(),
    })
    .catchall(z.string()),
  // v2 coverage blocks — optional during the export transition.
  series_daily: z.array(dayPointSchema).optional(),
  funnel: z
    .object({ all_time: funnelCountsSchema, last_30d: funnelCountsSchema })
    .optional(),
  relevance_hist_30d: z
    .object({ kept: scoreBucketsSchema, skipped: scoreBucketsSchema })
    .optional(),
  models_used_30d: z.array(modelUsageSchema).optional(),
  fetch_30d: z
    .object({
      ok: z.number(),
      paywalled: z.number(),
      failed: z.number(),
      skipped: z.number(),
    })
    .optional(),
  top_sources_30d: z
    .array(z.object({ name: z.string(), items: z.number() }))
    .optional(),
  echo_dist: z
    .object({
      '1': z.number(),
      '2': z.number(),
      '3_5': z.number(),
      '6_plus': z.number(),
    })
    .optional(),
  rhythm_7x24: z.array(z.array(z.number()).length(24)).length(7).optional(),
});

export type Stats = z.infer<typeof statsSchema>;

/* ── parse helpers ─────────────────────────────────────────────────────── */

export function parsePicks(data: unknown): Pick[] {
  return picksSchema.parse(data);
}

export function parseChannels(data: unknown): Channel[] {
  return channelsSchema.parse(data);
}

export function parseStats(data: unknown): Stats {
  return statsSchema.parse(data);
}
