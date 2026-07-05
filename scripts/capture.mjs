/**
 * Full-site screenshot harness.
 *
 *   npm run capture
 *
 * Starts `astro preview` itself (or reuses one already on :4321), then
 * captures every page route × [light, dark] × three viewports as full-page
 * PNGs into screenshots/<timestamp>/<route>--<scheme>--<vp>.png.
 * Also captures forced-dark (localStorage eclecta:theme=dark) variants for
 * / and /preferences/ only. Every URL is served at the site root.
 *
 * Extra matrix (one desktop viewport each, light):
 *   - reader-preference states (compact density, muted AI, signals+scores,
 *     fontsize xl) on / and /ai/
 *   - print emulation on /, /ai/, and the newest daily digest
 *   - one subcategory page currently in its empty state (probed from dist/)
 *
 * Compare two runs with scripts/shotdiff.mjs.
 */
import { spawn } from 'node:child_process';
import { mkdirSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
// Dedicated port: NEVER reuse :4321 — a dev server from another checkout on
// the default port once fed a whole capture run the wrong site.
const PORT = 4331;
const BASE_URL = `http://localhost:${PORT}`;

const VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 834, height: 1112 },
  { width: 1440, height: 900 },
];
const SCHEMES = ['light', 'dark'];
const FORCED_DARK_ROUTES = ['/', '/preferences/'];

/* ── routes ────────────────────────────────────────────────────────────── */
const CATEGORY_SLUGS = ['ai', 'research', 'software', 'security', 'hardware', 'industry'];
const SAMPLE_SUBS = ['/ai/models/', '/research/science/'];
const digestDir = join(ROOT, 'src/content/digests');
const digestIds = readdirSync(digestDir, { recursive: true, withFileTypes: true })
  .filter((e) => e.isFile() && e.name.endsWith('.md'))
  .map((e) => join(e.parentPath ?? e.path, e.name))
  .map((p) => p.slice(digestDir.length + 1).replace(/\.md$/, ''));

const routes = [
  '/',
  ...CATEGORY_SLUGS.map((s) => `/${s}/`),
  ...SAMPLE_SUBS,
  ...digestIds.map((id) => `/digests/${id}/`),
  '/coverage/',
  '/sources/',
  '/archive/',
  '/feeds/',
  '/preferences/',
  '/stats/',
  '/contact/',
  '/about/',
  '/404.html',
];

const routeName = (route) =>
  route === '/' ? 'front' : route.replace(/^\/|\/$/g, '').replace(/[/.]/g, '-');

/* ── preview server: reuse or spawn ────────────────────────────────────── */
async function isUp() {
  try {
    const r = await fetch(`${BASE_URL}/`, { signal: AbortSignal.timeout(2000) });
    return r.ok;
  } catch {
    return false;
  }
}

console.log(`Starting astro preview on :${PORT}…`);
const server = spawn('npx', ['astro', 'preview', '--port', String(PORT)], {
  cwd: ROOT,
  stdio: 'ignore',
  detached: true, // own process group, so cleanup kills astro, not just npx
});
{
  const deadline = Date.now() + 30_000;
  while (!(await isUp())) {
    if (Date.now() > deadline) {
      server.kill();
      throw new Error(`astro preview did not come up on :${PORT} in 30s — did you run \`npm run build\`?`);
    }
    await new Promise((r) => setTimeout(r, 400));
  }
}

/* ── capture ───────────────────────────────────────────────────────────── */
const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
const outDir = join(ROOT, 'screenshots', stamp);
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
let shots = 0;

async function capture(context, route, scheme, tag = scheme) {
  const page = await context.newPage();
  await page.emulateMedia({
    colorScheme: scheme === 'forced-dark' ? 'light' : scheme,
    reducedMotion: 'reduce',
  });
  for (const vp of VIEWPORTS) {
    await page.setViewportSize(vp);
    await page.goto(`${BASE_URL}${route}`, { waitUntil: 'networkidle' });
    const file = `${routeName(route)}--${tag}--${vp.width}x${vp.height}.png`;
    await page.screenshot({ path: join(outDir, file), fullPage: true });
    shots++;
    console.log(`  ${file}`);
  }
  await page.close();
}

/* one-shot capture at a single desktop viewport (variant matrix) */
const DESKTOP = { width: 1440, height: 900 };
async function captureOne(context, route, tag, media = {}) {
  const page = await context.newPage();
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce', ...media });
  await page.setViewportSize(DESKTOP);
  await page.goto(`${BASE_URL}${route}`, { waitUntil: 'networkidle' });
  const file = `${routeName(route)}--${tag}--${DESKTOP.width}x${DESKTOP.height}.png`;
  await page.screenshot({ path: join(outDir, file), fullPage: true });
  shots++;
  console.log(`  ${file}`);
  await page.close();
}

/* reader-preference variants: [tag, {localStorage key (sans prefix): value}] */
const PREF_VARIANTS = [
  ['compact', { density: 'compact' }],
  ['muted-ai', { mutedCategories: 'ai' }],
  ['signals', { showSignals: '1', showScores: '1' }],
  ['fontsize-xl', { fontSize: 'xl' }],
];
const PREF_ROUTES = ['/', '/ai/'];
const PRINT_ROUTES = ['/', '/ai/', ...digestIds.filter((id) => id.startsWith('daily/')).sort().slice(-1).map((id) => `/digests/${id}/`)];

/* find one subcategory page currently rendering its empty state */
async function findEmptySubRoute() {
  const distDir = join(ROOT, 'dist');
  const subRoutes = readdirSync(distDir, { recursive: true, withFileTypes: true })
    .filter((e) => e.isFile() && e.name === 'index.html')
    .map((e) => join(e.parentPath ?? e.path, e.name).slice(distDir.length, -'index.html'.length))
    .filter((r) => /^\/[a-z]+\/[a-z]+\/$/.test(r) && !r.startsWith('/digests/'));
  for (const r of subRoutes) {
    const html = await (await fetch(`${BASE_URL}${r}`)).text();
    if (html.includes('empty-state')) return r;
  }
  return null;
}

try {
  // OS-scheme variants for everything
  const context = await browser.newContext();
  for (const route of routes) {
    for (const scheme of SCHEMES) {
      await capture(context, route, scheme);
    }
  }

  // print emulation (desktop, light)
  for (const route of PRINT_ROUTES) {
    await captureOne(context, route, 'print', { media: 'print' });
  }

  // one empty-state subcategory page, if the current data has one
  const emptySub = await findEmptySubRoute();
  if (emptySub) {
    for (const scheme of SCHEMES) await capture(context, emptySub, scheme, `empty-${scheme}`);
  } else {
    console.log('  (no subcategory currently empty — skipping empty-state shot)');
  }
  await context.close();

  // forced-dark (reader preference, not OS) for the front + preferences
  const forced = await browser.newContext();
  await forced.addInitScript(() => localStorage.setItem('eclecta:theme', 'dark'));
  for (const route of FORCED_DARK_ROUTES) {
    await capture(forced, route, 'forced-dark');
  }
  await forced.close();

  // reader-preference variants (desktop, light)
  for (const [tag, prefs] of PREF_VARIANTS) {
    const ctx = await browser.newContext();
    await ctx.addInitScript((entries) => {
      for (const [k, v] of entries) localStorage.setItem(`eclecta:${k}`, v);
    }, Object.entries(prefs));
    for (const route of PREF_ROUTES) {
      await captureOne(ctx, route, tag);
    }
    await ctx.close();
  }
} finally {
  await browser.close();
  try {
    process.kill(-server.pid);
  } catch {
    server.kill();
  }
}

console.log(`\n${shots} screenshots → ${outDir}`);
