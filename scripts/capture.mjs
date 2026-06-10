/**
 * Full-site screenshot harness.
 *
 *   npm run capture
 *
 * Starts `astro preview` itself (or reuses one already on :4321), then
 * captures every page route × [light, dark] × three viewports as full-page
 * PNGs into screenshots/<timestamp>/<route>--<scheme>--<vp>.png.
 * Also captures forced-dark (localStorage lede:theme=dark) variants for
 * / and /preferences/ only. Every URL includes the /lede base.
 */
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const BASE_URL = 'http://localhost:4321/lede';

const VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 834, height: 1112 },
  { width: 1440, height: 900 },
];
const SCHEMES = ['light', 'dark'];
const FORCED_DARK_ROUTES = ['/', '/preferences/'];

/* ── routes ────────────────────────────────────────────────────────────── */
const channels = JSON.parse(readFileSync(join(ROOT, 'src/data/channels.json'), 'utf8'));
const digestDir = join(ROOT, 'src/content/digests');
const digestIds = readdirSync(digestDir, { recursive: true, withFileTypes: true })
  .filter((e) => e.isFile() && e.name.endsWith('.md'))
  .map((e) => join(e.parentPath ?? e.path, e.name))
  .map((p) => p.slice(digestDir.length + 1).replace(/\.md$/, ''));

const routes = [
  '/',
  ...channels.map((c) => `/${c.slug}/`),
  ...digestIds.map((id) => `/digests/${id}/`),
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

let server = null;
if (await isUp()) {
  console.log('Reusing astro preview already running on :4321');
} else {
  console.log('Starting astro preview…');
  server = spawn('npx', ['astro', 'preview'], { cwd: ROOT, stdio: 'ignore', detached: false });
  const deadline = Date.now() + 30_000;
  while (!(await isUp())) {
    if (Date.now() > deadline) {
      server.kill();
      throw new Error('astro preview did not come up on :4321 in 30s — did you run `npm run build`?');
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

async function capture(context, route, scheme) {
  const page = await context.newPage();
  await page.emulateMedia({
    colorScheme: scheme === 'forced-dark' ? 'light' : scheme,
    reducedMotion: 'reduce',
  });
  for (const vp of VIEWPORTS) {
    await page.setViewportSize(vp);
    await page.goto(`${BASE_URL}${route}`, { waitUntil: 'networkidle' });
    const file = `${routeName(route)}--${scheme}--${vp.width}x${vp.height}.png`;
    await page.screenshot({ path: join(outDir, file), fullPage: true });
    shots++;
    console.log(`  ${file}`);
  }
  await page.close();
}

try {
  // OS-scheme variants for everything
  const context = await browser.newContext();
  for (const route of routes) {
    for (const scheme of SCHEMES) {
      await capture(context, route, scheme);
    }
  }
  await context.close();

  // forced-dark (reader preference, not OS) for the front + preferences
  const forced = await browser.newContext();
  await forced.addInitScript(() => localStorage.setItem('lede:theme', 'dark'));
  for (const route of FORCED_DARK_ROUTES) {
    await capture(forced, route, 'forced-dark');
  }
  await forced.close();
} finally {
  await browser.close();
  if (server) server.kill();
}

console.log(`\n${shots} screenshots → ${outDir}`);
