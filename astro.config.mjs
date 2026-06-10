// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// Deploy target is env-driven so the custom-domain flip is one variable:
//   today:  GitHub project pages  -> site https://illyastarikov.github.io, base /lede
//   later:  lede.starikov.io      -> LEDE_SITE=https://lede.starikov.io LEDE_BASE=/
const SITE = process.env.LEDE_SITE || 'https://illyastarikov.github.io';
const BASE = process.env.LEDE_BASE || '/lede';

export default defineConfig({
  site: SITE,
  base: BASE,
  integrations: [sitemap()],
  build: { format: 'directory' },
});
