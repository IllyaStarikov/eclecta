/**
 * Mobile layout integrity. Two invariants a phone reader depends on:
 *   1. no page scrolls sideways (nothing wider than the viewport);
 *   2. the footer keeps its gutter — it once lost its horizontal padding on
 *      phones (the `.foot` padding shorthand clobbered the `.wrap` gutter) and
 *      clipped flush against the left edge.
 */
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import { BASE_URL } from '../../playwright.config';

const u = (path: string) => `${BASE_URL}${path}`;
const here = dirname(fileURLToPath(import.meta.url));
const digestsDir = join(here, '../../src/content/digests');
const dailies = readdirSync(join(digestsDir, 'daily'))
  .filter((f) => f.endsWith('.md'))
  .sort();
const latestDaily = dailies.length
  ? `/digests/daily/${dailies[dailies.length - 1].replace(/\.md$/, '')}/`
  : '/';

const ROUTES = ['/', latestDaily, '/coverage/', '/preferences/', '/feeds/', '/about/'];
// 360px is the production floor — covers current phones (iPhone SE2/3 = 375,
// small Android = 360). Below that is outside the design's support range.
const WIDTHS = [390, 360];

for (const width of WIDTHS) {
  for (const route of ROUTES) {
    test(`no horizontal overflow at ${width}px — ${route}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 });
      await page.goto(u(route));
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      expect(overflow, `horizontal overflow on ${route} at ${width}px`).toBeLessThanOrEqual(0);
    });
  }
}

test('the footer keeps its left gutter on mobile (no flush-left clip)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(u('/'));
  const foot = page.locator('footer.foot');
  // The footer element spans the width; the gutter lives on its content, so
  // assert the identity block sits inside the gutter, not at x=0.
  const idBox = await foot.locator('.foot__id').boundingBox();
  expect(idBox).not.toBeNull();
  expect(idBox!.x, 'footer content flush against the left edge').toBeGreaterThanOrEqual(12);
  // and the identity + three link columns all survive the narrow layout
  for (const label of ['Read', 'The wire', 'Made by']) {
    await expect(foot.locator(`nav[aria-label="${label}"]`)).toBeVisible();
  }
});
