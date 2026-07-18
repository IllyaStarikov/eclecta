/**
 * Every feed in the registry: 200, well-formed XML, absolute https links.
 * Item-count expectations are data-aware: a cadence with no editions yet
 * (e.g. yearly before January) or an empty category legitimately serves a
 * valid, empty feed.
 */
import { readdirSync, existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { XMLValidator, XMLParser } from 'fast-xml-parser';
import { expect, test } from '@playwright/test';
import { FEEDS } from '../../src/lib/feeds';
import { resolveCategory } from '../../src/lib/taxonomy';
const here = dirname(fileURLToPath(import.meta.url));
const picks: { title: string; channels?: string[]; category?: string; subcategories?: string[] }[] =
  JSON.parse(readFileSync(join(here, '../../src/data/picks.json'), 'utf8'));
const digestDir = join(here, '../../src/content/digests');

function digestCount(kind?: string): number {
  const dir = kind ? join(digestDir, kind) : digestDir;
  if (!existsSync(dir)) return 0;
  return readdirSync(dir, { recursive: true }).filter((f) => String(f).endsWith('.md')).length;
}

function expectedNonEmpty(slug: string): boolean {
  if (slug === 'everything') return picks.length + digestCount() > 0;
  if (slug === 'digests') return digestCount() > 0;
  if (slug.startsWith('digests-')) return digestCount(slug.slice('digests-'.length)) > 0;
  if (slug.startsWith('cat-')) {
    const cat = slug.slice('cat-'.length);
    return picks.some((p) => resolveCategory(p).category === cat);
  }
  return false;
}

for (const feed of FEEDS) {
  test(`feed ${feed.path} is valid RSS with absolute links`, async ({ request }) => {
    const resp = await request.get(feed.path);
    expect(resp.status()).toBe(200);
    const text = await resp.text();
    expect(XMLValidator.validate(text), `malformed XML at ${feed.path}`).toBe(true);

    const doc = new XMLParser({ ignoreAttributes: false }).parse(text);
    const items = [doc?.rss?.channel?.item ?? []].flat();
    if (expectedNonEmpty(feed.slug)) {
      expect(items.length, `${feed.path} has no items but its source data is non-empty`).toBeGreaterThan(0);
    }
    for (const item of items) {
      const link = String(item.link ?? '');
      expect(link, `relative link in ${feed.path}: ${link}`).toMatch(/^https?:\/\//);
    }
  });
}
