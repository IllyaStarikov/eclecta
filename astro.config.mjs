// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// Lede — served at the apex of starikov.io via GitHub Pages.
export default defineConfig({
  site: 'https://starikov.io',
  integrations: [sitemap()],
  build: { format: 'directory' },
});
