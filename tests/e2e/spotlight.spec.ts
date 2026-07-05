/**
 * Spotlight is data-state-conditional: the section renders exactly when
 * src/data/spotlight.json exists with valid items, and hides otherwise.
 * The spec reads the data from disk so it passes in both states.
 */
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '@playwright/test';
import { spotlightFileSchema } from '../../src/lib/schema';
import { SPOTLIGHT_LIMIT } from '../../src/lib/spotlight';

function expectedCount(): number {
  const path = join(__dirname, '../../src/data/spotlight.json');
  if (!existsSync(path)) return 0;
  const parsed = spotlightFileSchema.safeParse(JSON.parse(readFileSync(path, 'utf8')));
  if (!parsed.success) return 0;
  const items = Array.isArray(parsed.data) ? parsed.data : parsed.data.items;
  return Math.min(items.length, SPOTLIGHT_LIMIT);
}

test('spotlight renders exactly when data exists', async ({ page }) => {
  const n = expectedCount();
  await page.goto('/');
  await expect(page.locator('#s-spotlight .spot__item')).toHaveCount(n);
  const railEntry = page.locator('.erail__index a[href="#s-spotlight"]');
  await expect(railEntry).toHaveCount(n > 0 ? 1 : 0);
});

test('spotlight survives muting a category', async ({ page }) => {
  test.skip(expectedCount() === 0, 'dormant: no spotlight data yet');
  await page.addInitScript(() => localStorage.setItem('eclecta:mutedCategories', 'ai'));
  await page.goto('/');
  await expect(page.locator('#s-spotlight')).toBeVisible();
  await expect(page.locator('#s-ai')).toBeHidden();
});
