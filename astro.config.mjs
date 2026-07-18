// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import { lastmodForUrl } from './src/lib/sitemap.mjs';

// Deploy target is env-driven. The live site is the custom apex domain
// eclecta.co (base /); override with ECLECTA_SITE / ECLECTA_BASE for a
// project-pages preview (e.g. https://illyastarikov.github.io + /eclecta).
const SITE = process.env.ECLECTA_SITE || 'https://eclecta.co';
const BASE = process.env.ECLECTA_BASE || '/';

export default defineConfig({
  site: SITE,
  base: BASE,
  integrations: [
    sitemap({
      serialize(item) {
        const lastmod = lastmodForUrl(item.url);
        return lastmod ? { ...item, lastmod } : item;
      },
    }),
  ],
  build: { format: 'directory' },
});
