import { expect, test } from '@playwright/test';
import { BASE_URL } from '../../playwright.config';

const u = (path: string) => `${BASE_URL}${path}`;

test('downvote marks the pick, persists, and hideDownvoted hides it', async ({ page }) => {
  await page.goto(u('/'));
  const first = page.locator('article.pick').first();
  const pickId = await first.getAttribute('data-pick-id');
  expect(pickId).toBeTruthy();
  const pick = page.locator(`article.pick[data-pick-id="${pickId}"]`);

  await pick.locator('button[data-vote-btn="down"]').click();
  await expect(pick).toHaveAttribute('data-vote', 'down');

  // device-local persistence
  await page.reload();
  await expect(pick).toHaveAttribute('data-vote', 'down');

  // hideDownvoted removes it from the page entirely
  await page.goto(u('/preferences/'));
  await page.check('#pref-hidedown');
  await page.goto(u('/'));
  await expect(pick).toBeHidden();
});
