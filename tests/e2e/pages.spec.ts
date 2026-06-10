import { expect, test } from '@playwright/test';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { BASE_URL } from '../../playwright.config';

const u = (path: string) => `${BASE_URL}${path}`;
const here = dirname(fileURLToPath(import.meta.url));

const channels: { slug: string }[] = JSON.parse(
  readFileSync(join(here, '../../src/data/channels.json'), 'utf8')
);

// Discover digest routes from the content directory, so new editions are
// covered without touching this file.
const digestDir = join(here, '../../src/content/digests');
const digestIds = readdirSync(digestDir, { recursive: true, withFileTypes: true })
  .filter((e) => e.isFile() && e.name.endsWith('.md'))
  .map((e) => join(e.parentPath ?? (e as any).path, e.name))
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

test('front page loads without console errors', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', (err) => errors.push(String(err)));
  await page.goto(u('/'));
  await page.waitForLoadState('networkidle');
  expect(errors).toEqual([]);
});
