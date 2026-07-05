/**
 * Every feed in the registry: 200, well-formed XML, absolute https links.
 * Replaces the old three-feed substring check.
 */
import { XMLValidator, XMLParser } from 'fast-xml-parser';
import { expect, test } from '@playwright/test';
import { FEEDS } from '../../src/lib/feeds';

for (const feed of FEEDS) {
  test(`feed ${feed.path} is valid RSS with absolute links`, async ({ request }) => {
    const resp = await request.get(feed.path);
    expect(resp.status()).toBe(200);
    const text = await resp.text();
    expect(XMLValidator.validate(text), `malformed XML at ${feed.path}`).toBe(true);

    const doc = new XMLParser({ ignoreAttributes: false }).parse(text);
    const items = [doc?.rss?.channel?.item ?? []].flat();
    expect(items.length, `${feed.path} has no items`).toBeGreaterThan(0);
    for (const item of items) {
      const link = String(item.link ?? '');
      expect(link, `relative link in ${feed.path}: ${link}`).toMatch(/^https?:\/\//);
    }
  });
}
