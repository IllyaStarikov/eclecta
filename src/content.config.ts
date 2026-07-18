import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

// Digests — markdown with frontmatter, written by the Signal pipeline into
// src/content/digests/<kind>/<period>.md. Schema is validated at build time,
// so a malformed pipeline export fails the build instead of shipping.
const digests = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/digests' }),
  schema: z.object({
    title: z.string(),
    // Required, no default: the pipeline always writes kind, and a missing one
    // must fail the build loudly rather than silently masquerade as 'weekly'
    // (wrong archive group, wrong feed, wrong label).
    kind: z.enum(['daily', 'weekly', 'monthly', 'quarterly', 'yearly']),
    period: z.string(),
    date: z.coerce.date(),
    blurb: z.string(),
    items: z.number().optional(),
    /* model provenance (optional until the pipeline emits it): which model
       wrote this edition, e.g. "claude-opus-4-8" */
    model: z.string().optional(),
  }),
});

// Library — the entity wiki, written by the Signal pipeline into
// src/content/library/<slug>.md (companies, models, technologies, standards,
// events, projects; never people in v1). Each page is a dated timeline built
// deterministically from coverage.
const library = defineCollection({
  loader: glob({ pattern: '*.md', base: './src/content/library' }),
  schema: z.object({
    name: z.string(),
    slug: z.string(),
    type: z.enum(['company', 'model', 'technology', 'standard', 'event', 'project']),
    summary: z.string(),
    updated: z.coerce.date(),
    coverage: z.number().optional(),
  }),
});

export const collections = { digests, library };
