import { expect, test } from '@playwright/test';
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { BASE_URL } from '../../playwright.config';
import { CATEGORIES } from '../../src/lib/taxonomy';

const u = (path: string) => `${BASE_URL}${path}`;
const here = dirname(fileURLToPath(import.meta.url));

// Discover digest routes from the content directory, so new editions are
// covered without touching this file.
const digestDir = join(here, '../../src/content/digests');
const digestIds = readdirSync(digestDir, { recursive: true, withFileTypes: true })
  .filter((e) => e.isFile() && e.name.endsWith('.md'))
  .map((e) => join(e.parentPath ?? (e as any).path, e.name))
  .map((p) => p.slice(digestDir.length + 1).replace(/\.md$/, ''));

// Sample instead of enumerating (the daily archive grows forever): the
// newest edition of each kind + the oldest daily as a regression canary.
const byKind = new Map<string, string[]>();
for (const id of digestIds) {
  const kind = id.split('/')[0];
  byKind.set(kind, [...(byKind.get(kind) ?? []), id]);
}
const sampledDigests = [
  ...[...byKind.values()].map((ids) => [...ids].sort().at(-1)!),
  ...(byKind.has('daily') ? [[...byKind.get('daily')!].sort()[0]] : []),
];

const routes = [
  '/',
  ...CATEGORIES.map((c) => `/${c.slug}/`),
  ...CATEGORIES.flatMap((c) => c.subcategories.map((s) => `/${c.slug}/${s.slug}/`)),
  ...sampledDigests.map((id) => `/digests/${id}/`),
  '/sources/',
  '/coverage/',
  '/archive/',
  '/feeds/',
  '/preferences/',
  '/stats/',
  '/contact/',
  '/about/',
];

for (const route of routes) {
  test(`page ${route} returns 200`, async ({ page }) => {
    const resp = await page.goto(u(route));
    expect(resp, `no response for ${route}`).toBeTruthy();
    expect(resp!.status()).toBe(200);
  });
}

const feedRoutes = ['/rss.xml', '/digests/rss.xml', '/digests/weekly/rss.xml'];

for (const route of feedRoutes) {
  test(`feed ${route} serves RSS`, async ({ request }) => {
    const resp = await request.get(u(route));
    expect(resp.status()).toBe(200);
    expect(await resp.text()).toContain('<rss');
  });
}

const consoleRoutes = [
  '/',
  '/ai/',
  '/ai/agents/',
  '/coverage/',
  '/sources/',
  '/preferences/',
  '/archive/',
  '/feeds/',
];

for (const route of consoleRoutes) {
  test(`page ${route} loads without console errors`, async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });
    page.on('pageerror', (err) => errors.push(String(err)));
    await page.goto(u(route));
    await page.waitForLoadState('networkidle');
    expect(errors, `console errors on ${route}`).toEqual([]);
  });
}
