import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

// Weekly digests — markdown with frontmatter, written by the Signal pipeline
// into src/content/digests/. Schema is validated at build time.
const digests = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/digests' }),
  schema: z.object({
    title: z.string(),
    week: z.string(),
    date: z.coerce.date(),
    blurb: z.string(),
  }),
});

export const collections = { digests };
