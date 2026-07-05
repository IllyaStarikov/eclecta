// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// Deploy target is env-driven. The live site is the custom apex domain
// eclecta.co (base /); override with ECLECTA_SITE / ECLECTA_BASE for a
// project-pages preview (e.g. https://illyastarikov.github.io + /eclecta).
const SITE = process.env.ECLECTA_SITE || 'https://eclecta.co';
const BASE = process.env.ECLECTA_BASE || '/';

export default defineConfig({
  site: SITE,
  base: BASE,
  integrations: [sitemap()],
  build: { format: 'directory' },
  // /stats/ merged into /coverage/ (spec 2026-07-04); static meta-refresh.
  redirects: { '/stats': '/coverage/' },
});
