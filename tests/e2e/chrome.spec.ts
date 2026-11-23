/**
 * The site chrome carries the theme: machines read the feeds, the result is
 * open source. Dateline says the issue number and links the repo; the footer
 * is a colophon grid; the archive gives every cadence equal standing; no
 * em-dash survives in rendered chrome copy.
 */
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import { BASE_URL } from '../../playwright.config';

const u = (path: string) => `${BASE_URL}${path}`;
const here = dirname(fileURLToPath(import.meta.url));
const digestsDir = join(here, '../../src/content/digests');

/** Published editions per kind, straight from the content on disk. */
const KIND_COUNTS: Record<string, number> = {};
for (const kind of readdirSync(digestsDir)) {
  KIND_COUNTS[kind] = readdirSync(join(digestsDir, kind)).filter((f) => f.endsWith('.md')).length;
}
const TOTAL = Object.values(KIND_COUNTS).reduce((a, b) => a + b, 0);

// The archive's open window per cadence — keep in sync with archive.astro.
const RECENT: Record<string, number> = { daily: 7, weekly: 4, monthly: 3, quarterly: 4, yearly: 5 };

test('dateline claims a real issue number and links the current edition', async ({ page }) => {
  await page.goto(u('/'));
  const edition = page.locator('.dateline__edition');
  await expect(edition).toContainText(`No. ${TOTAL}`);
  // the edition slot now links the CURRENT edition (useful), not the static repo
  const link = edition.locator('a');
  await expect(link).toHaveText(/^(Daily Brief|Weekly Digest|Monthly Review|Quarterly Report|The Year)$/);
  expect(await link.getAttribute('href')).toMatch(/\/digests\/.+\/$/);
  await expect(page.getByText('Automated edition')).toHaveCount(0);
});

test('every page carries the same masthead: a digest shows the wordmark home link + channels nav', async ({ page }) => {
  const dailies = readdirSync(join(digestsDir, 'daily')).filter((f) => f.endsWith('.md')).sort();
  test.skip(dailies.length === 0, 'no daily digests to check');
  const slug = dailies[dailies.length - 1].replace(/\.md$/, '');
  await page.goto(u(`/digests/daily/${slug}/`));
  const wordmark = page.locator('.masthead__wordmark a');
  await expect(wordmark).toBeVisible();
  const href = await wordmark.getAttribute('href');
  expect(href === '/' || href?.endsWith('/')).toBeTruthy();
  // the full chrome (channels nav + dateline) is present, identical to the front
  await expect(page.locator('nav.channels')).toBeVisible();
  await expect(page.locator('.dateline__edition')).toContainText('No. ');
});

test('the footer is a colophon grid: identity plus three link columns', async ({ page }) => {
  await page.goto(u('/'));
  const foot = page.locator('footer.foot');
  await expect(foot.locator('.foot__id p')).toContainText('open source');
  await expect(foot.locator('.foot__colophon')).toContainText(`No. ${TOTAL}`);
  for (const label of ['Read', 'The wire', 'Made by']) {
    await expect(foot.locator(`nav[aria-label="${label}"]`)).toBeVisible();
  }
  const mit = foot.getByRole('link', { name: 'Open source, MIT' });
  expect(await mit.getAttribute('href')).toMatch(/^https:\/\/github\.com\//);
  // the colophon credit resolves to a real anchor on the about page
  const models = foot.getByRole('link', { name: 'Colophon' });
  const anchor = (await models.getAttribute('href'))!;
  await page.goto(u(anchor.replace(/^.*(?=\/about\/)/, '')));
  await expect(page.locator(`#${anchor.split('#')[1]}`)).toHaveCount(1);
});

test('archive windows: a fixed recent slice per cadence, the rest folded', async ({ page }) => {
  await page.goto(u('/archive/'));
  for (const [kind, total] of Object.entries(KIND_COUNTS)) {
    if (total === 0) continue;
    const section = page.locator(`section[aria-label*="${kind === 'yearly' ? 'year' : kind}" i]`);
    await expect(section).toHaveCount(1);
    const open = section.locator('> .briefing > article');
    const expectedOpen = Math.min(total, RECENT[kind]);
    await expect(open).toHaveCount(expectedOpen);
    const folded = total - expectedOpen;
    const fold = section.locator('details.arch-month');
    if (folded > 0) {
      await expect(fold.locator('summary')).toContainText(`${folded} more`);
      // the fold really carries the remainder
      await expect(fold.locator('article')).toHaveCount(folded);
    } else {
      await expect(fold).toHaveCount(0);
    }
  }
});

test('no em-dash reaches the reader in chrome copy', async ({ page }) => {
  // Pages whose every rendered word is chrome (no pipeline-written prose,
  // which is the pipeline style guide's jurisdiction, not the templates').
  for (const path of ['/feeds/', '/preferences/', '/contact/']) {
    await page.goto(u(path));
    const text = await page.locator('body').innerText();
    expect(text.includes('—'), `em-dash rendered on ${path}`).toBe(false);
  }
  // On content pages, check the chrome furniture around the prose.
  await page.goto(u('/'));
  for (const sel of ['header', 'footer.foot', '.erail']) {
    const text = await page.locator(sel).innerText();
    expect(text.includes('—'), `em-dash in ${sel} on /`).toBe(false);
  }
});
