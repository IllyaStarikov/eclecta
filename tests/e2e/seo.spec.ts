/** SEO/social contracts per page archetype, plus the 404. */
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

const here = dirname(fileURLToPath(import.meta.url));
const dailyDir = join(here, '../../src/content/digests/daily');
const latestDaily = readdirSync(dailyDir)
  .filter((f) => f.endsWith('.md'))
  .sort()
  .at(-1)!
  .replace(/\.md$/, '');

test('front page: website og + WebSite JSON-LD + theme-colors + global feeds', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('meta[property="og:type"]')).toHaveAttribute('content', 'website');
  await expect(page.locator('meta[name="theme-color"]')).toHaveCount(2);
  await expect(page.locator('link[rel="alternate"][type="application/rss+xml"]')).toHaveCount(2);
  const ld = JSON.parse((await page.locator('script[type="application/ld+json"]').textContent())!);
  expect(ld['@type']).toBe('WebSite');
  await expect(page.locator('meta[property="og:image"]')).toHaveAttribute('content', /og\/default\.png$/);
});

test('digest: article og + published_time + Article JSON-LD + kind feed + live card', async ({ page, request }) => {
  await page.goto(`/digests/daily/${latestDaily}/`);
  await expect(page.locator('meta[property="og:type"]')).toHaveAttribute('content', 'article');
  await expect(page.locator('meta[property="article:published_time"]')).toHaveCount(1);
  const ld = JSON.parse((await page.locator('script[type="application/ld+json"]').textContent())!);
  expect(ld['@type']).toBe('Article');
  await expect(page.locator('link[rel="alternate"][type="application/rss+xml"]')).toHaveCount(3);
  const og = await page.locator('meta[property="og:image"]').getAttribute('content');
  const card = await request.get(og!.replace(/^https?:\/\/[^/]+/, ''));
  expect(card.status()).toBe(200);
  expect(card.headers()['content-type']).toContain('image/png');
});

test('the default OG card actually renders (every non-digest page references it)', async ({ request }) => {
  const card = await request.get('/og/default.png');
  expect(card.status()).toBe(200);
  expect(card.headers()['content-type']).toContain('image/png');
});

test('sitemap: index + url set exist, list routes, and digests carry lastmod', async ({ request }) => {
  // robots.txt advertises https://eclecta.co/sitemap-index.xml — it must be real.
  const index = await request.get('/sitemap-index.xml');
  expect(index.status()).toBe(200);
  expect(index.headers()['content-type']).toContain('xml');
  const sub = (await index.text()).match(/<loc>([^<]*sitemap-\d+\.xml)<\/loc>/)?.[1];
  expect(sub, 'index references a url sitemap').toBeTruthy();

  const urls = await request.get(sub!.replace(/^https?:\/\/[^/]+/, ''));
  expect(urls.status()).toBe(200);
  const xml = await urls.text();
  expect(xml).toContain('https://eclecta.co/');
  expect(xml).toContain('https://eclecta.co/ai/');
  expect(xml).toContain('https://eclecta.co/library/');
  expect(xml).toContain(`https://eclecta.co/digests/daily/${latestDaily}/`);
  // the serialize() hook stamps digest URLs with a computed <lastmod>
  expect(xml).toContain('<lastmod>');
});

test('category page advertises its own feed', async ({ page }) => {
  await page.goto('/ai/');
  await expect(
    page.locator('link[rel="alternate"][type="application/rss+xml"][href$="/ai/rss.xml"]')
  ).toHaveCount(1);
});

test('404 serves the not-found page with status 404', async ({ page }) => {
  const resp = await page.goto('/definitely-not-a-page/');
  expect(resp!.status()).toBe(404);
  await expect(page.locator('.nf__line')).toContainText('below the relevance threshold');
});
