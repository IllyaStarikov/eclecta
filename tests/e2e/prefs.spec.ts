import { expect, test } from '@playwright/test';
import { BASE_URL } from '../../playwright.config';

const u = (path: string) => `${BASE_URL}${path}`;

test('dark theme radio stamps html[data-theme=dark] and persists', async ({ page }) => {
  await page.goto(u('/preferences/'));
  await expect(page.locator('html')).not.toHaveAttribute('data-theme', 'dark');

  // the radio itself is visually hidden; its label is the control
  await page.click('label[for="theme-dark"]');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');

  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.locator('#theme-dark')).toBeChecked();
});

test('showSignals reveals the signals panel on the front page', async ({ page }) => {
  await page.goto(u('/'));
  await expect(page.locator('.pick__signals').first()).toBeHidden();

  await page.goto(u('/preferences/'));
  // the checkbox is a visually-hidden switch; its wrapping label is the control
  await page.getByText('Show curation signals').click();
  await expect(page.locator('#pref-signals')).toBeChecked();
  await expect(page.locator('html')).toHaveAttribute('data-showsignals', '1');

  await page.goto(u('/'));
  await expect(page.locator('.pick__signals').first()).toBeVisible();
});

test('fontSize xl stamps html[data-fontsize=xl]', async ({ page }) => {
  await page.goto(u('/preferences/'));
  await page.click('label[for="fontsize-xl"]');
  await expect(page.locator('html')).toHaveAttribute('data-fontsize', 'xl');

  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-fontsize', 'xl');
});

test('muting a section stamps data-muted and hides it on the front page', async ({ page }) => {
  await page.goto(u('/preferences/'));
  // toggles default ON (shown); clicking the AI switch mutes it
  await page.locator('label.pref-toggle:has(#mute-ai)').click();
  await expect(page.locator('html')).toHaveAttribute('data-muted', /\bai\b/);

  await page.goto(u('/'));
  await expect(page.locator('html')).toHaveAttribute('data-muted', /\bai\b/);
  // the AI section (and its nav link) are display:none when muted
  await expect(page.locator('nav.channels a[data-category="ai"]')).toBeHidden();
});
