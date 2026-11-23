/**
 * The Library: a reader-facing entity wiki built from coverage. The index maps
 * what we track; each entity page is a dated timeline. It is reachable from the
 * chrome, carries no archive.* links, and is in the sitemap.
 */
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import { BASE_URL } from '../../playwright.config';

const u = (path: string) => `${BASE_URL}${path}`;
const here = dirname(fileURLToPath(import.meta.url));
const libDir = join(here, '../../src/content/library');
const slugs = readdirSync(libDir)
  .filter((f) => f.endsWith('.md'))
  .map((f) => f.replace(/\.md$/, ''));
const firstSlug = slugs[0];

test('the Library is reachable from the header nav and the footer', async ({ page }) => {
  await page.goto(u('/'));
  await expect(
    page.locator('nav.channels').getByRole('link', { name: 'Library' })
  ).toHaveAttribute('href', /\/library\/$/);
  await expect(
    page.locator('footer.foot nav[aria-label="Read"]').getByRole('link', { name: 'Library' })
  ).toHaveCount(1);
});

test('library index: heading, and it lists at least one entity page', async ({ page }) => {
  await page.goto(u('/library/'));
  await expect(page.locator('h1')).toContainText('Library');
  // links out to an entity page
  const entityLinks = page.locator('a[href*="/library/"]');
  expect(await entityLinks.count()).toBeGreaterThan(0);
  await expect(page.getByRole('link', { name: new RegExp(firstSlug.replace(/-/g, '.'), 'i') }).first()).toBeVisible();
});

test('an entity page renders a dated timeline and no archive.* links', async ({ page }) => {
  test.skip(!firstSlug, 'no library content seeded');
  await page.goto(u(`/library/${firstSlug}/`));
  await expect(page.locator('.article__kind')).toBeVisible();
  await expect(page.locator('h2', { hasText: 'Timeline' })).toBeVisible();
  // at least one dated timeline bullet with a source link
  expect(await page.locator('.prose li').count()).toBeGreaterThan(0);
  const html = await page.content();
  expect(html).not.toMatch(/archive\.(ph|today|is|org)/i);
});

test('an entity page carries canonical + DefinedTerm JSON-LD', async ({ page }) => {
  test.skip(!firstSlug, 'no library content seeded');
  await page.goto(u(`/library/${firstSlug}/`));
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
    'href',
    new RegExp(`/library/${firstSlug}/$`)
  );
  const ld = JSON.parse((await page.locator('script[type="application/ld+json"]').first().textContent())!);
  expect(ld['@type']).toBe('DefinedTerm');
});
