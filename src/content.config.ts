import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

// Digests — markdown with frontmatter, written by the Signal pipeline into
// src/content/digests/<kind>/<period>.md. Schema is validated at build time,
// so a malformed pipeline export fails the build instead of shipping.
const digests = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/digests' }),
  schema: z.object({
    title: z.string(),
    kind: z
      .enum(['daily', 'weekly', 'monthly', 'quarterly', 'yearly'])
      .default('weekly'),
    period: z.string(),
    date: z.coerce.date(),
    blurb: z.string(),
    items: z.number().optional(),
    /* model provenance (optional until the pipeline emits it): which model
       wrote this edition, e.g. "claude-opus-4-8" */
    model: z.string().optional(),
  }),
});

export const collections = { digests };
