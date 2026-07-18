import { expect, test } from '@playwright/test';
import { BASE_URL } from '../../playwright.config';

const u = (path: string) => `${BASE_URL}${path}`;

test('front page renders the edition rail with a working section index (desktop)', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(u('/'));

  const index = page.locator('.erail__index');
  await expect(index).toBeVisible();

  // every "in this edition" anchor resolves to a real section id on the page
  const hrefs = await index.locator('a').evaluateAll((as) =>
    as.map((a) => (a as HTMLAnchorElement).getAttribute('href') ?? '')
  );
  expect(hrefs.length).toBeGreaterThan(0);
  for (const h of hrefs) {
    expect(h).toMatch(/^#s-/);
    await expect(page.locator(h)).toHaveCount(1);
  }
});

test('the rail section index collapses on mobile, subscribe persists', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(u('/'));
  // index + sibling nav fold away on narrow; the rail still carries subscribe
  await expect(page.locator('.erail__index')).toBeHidden();
  await expect(page.locator('.erail__sub')).toBeVisible();
});

test('the reading marker tracks scroll position down the section index', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(u('/'));

  // the marker is JS-built inside the index list
  const marker = page.locator('.erail__index .erail__marker');
  await expect(marker).toHaveCount(1);

  // at the top of the page the first section owns the marker
  const links = page.locator('.erail__index a');
  await expect(links.first()).toHaveAttribute('aria-current', 'location');

  // at the bottom of the page the last section owns it
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(links.last()).toHaveAttribute('aria-current', 'location');
  await expect(links.first()).not.toHaveAttribute('aria-current', 'location');
});

test('the rail carries one editions ledger, every cadence, no separate brief', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(u('/'));

  const editions = page.locator('.erail__editions');
  await expect(editions).toBeVisible();
  await expect(editions.locator('.erail__head')).toHaveText('Editions');
  // the daily leads the ledger; the old standalone brief block is gone
  await expect(editions.locator('li').first()).toContainText('Daily Brief');
  expect(await editions.locator('li').count()).toBeGreaterThanOrEqual(3);
  await expect(page.locator('.erail__brief')).toHaveCount(0);
  await expect(page.getByText('Latest brief')).toHaveCount(0);
});

test('muting a category hides its section and its rail index entry', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(u('/preferences/'));
  await page.locator('label.pref-toggle:has(#mute-ai)').click();
  await expect(page.locator('html')).toHaveAttribute('data-muted', /\bai\b/);

  await page.goto(u('/'));
  // the AI section (id="s-ai") and the rail's "in this edition" AI entry both hide
  await expect(page.locator('#s-ai')).toBeHidden();
  await expect(page.locator('.erail__index li[data-category="ai"]')).toBeHidden();
});
