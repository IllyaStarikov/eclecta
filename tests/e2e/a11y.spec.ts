/**
 * Accessibility gate: zero WCAG A/AA violations on the key page archetypes.
 * The design's contrast tokens were tuned for AA; this keeps them honest.
 * No disableRules — a rule may only be disabled with an inline comment
 * naming the design constraint it fights.
 */
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

const here = dirname(fileURLToPath(import.meta.url));
const dailyDir = join(here, '../../src/content/digests/daily');
const latestDaily = readdirSync(dailyDir)
  .filter((f) => f.endsWith('.md'))
  .sort()
  .at(-1)!
  .replace(/\.md$/, '');

const ROUTES = ['/', '/ai/', `/digests/daily/${latestDaily}/`, '/coverage/', '/preferences/'];

for (const route of ROUTES) {
  test(`axe: ${route} has no WCAG A/AA violations`, async ({ page }) => {
    await page.goto(route);
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze();
    expect(
      results.violations.map((v) => `${v.id}: ${v.nodes.length} nodes (${v.helpUrl})`)
    ).toEqual([]);
  });
}
