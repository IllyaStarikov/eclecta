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

test('accent cobalt stamps html[data-accent] and persists', async ({ page }) => {
  await page.goto(u('/preferences/'));
  await expect(page.locator('html')).not.toHaveAttribute('data-accent', 'cobalt');

  await page.click('label[for="accent-cobalt"]');
  await expect(page.locator('html')).toHaveAttribute('data-accent', 'cobalt');

  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-accent', 'cobalt');
  await expect(page.locator('#accent-cobalt')).toBeChecked();
});

test('high contrast stamps html[data-contrast=high] and persists', async ({ page }) => {
  await page.goto(u('/preferences/'));
  await page.click('label[for="contrast-high"]');
  await expect(page.locator('html')).toHaveAttribute('data-contrast', 'high');

  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-contrast', 'high');
});

test('underline-links toggle stamps html[data-underline]', async ({ page }) => {
  await page.goto(u('/preferences/'));
  await page.getByText('Underline every link').click();
  await expect(page.locator('#pref-underline')).toBeChecked();
  await expect(page.locator('html')).toHaveAttribute('data-underline', '1');
});

test('the data panel round-trips settings as JSON', async ({ page }) => {
  await page.goto(u('/preferences/'));
  await page.click('label[for="theme-dark"]');
  await expect(page.locator('[data-prefs-io]')).toHaveValue(/"theme": "dark"/);

  // paste a different set and apply it: pasted keys win, the rest reset
  await page.fill('[data-prefs-io]', '{ "accent": "moss", "wideSpacing": "1" }');
  await page.click('[data-prefs-import]');
  await expect(page.locator('html')).toHaveAttribute('data-accent', 'moss');
  await expect(page.locator('html')).toHaveAttribute('data-widespacing', '1');
  await expect(page.locator('html')).not.toHaveAttribute('data-theme', 'dark');
});

test('reset returns every preference to defaults', async ({ page }) => {
  await page.goto(u('/preferences/'));
  await page.click('label[for="theme-dark"]');
  await page.click('label[for="accent-plum"]');
  await expect(page.locator('[data-prefs-count]')).toHaveText('2 settings changed');

  await page.click('[data-prefs-reset]');
  await expect(page.locator('html')).not.toHaveAttribute('data-theme', 'dark');
  await expect(page.locator('html')).not.toHaveAttribute('data-accent', 'plum');
  await expect(page.locator('[data-prefs-count]')).toHaveText('all defaults');
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
