import { describe, expect, it } from 'vitest';
import {
  pickPrimaryLink,
  pickFreeLink,
  pickItemHtml,
  digestItemHtml,
  getFeed,
  FEED_STYLESHEET,
  FEEDS,
  type FeedPick,
  type FeedDigest,
} from '../../src/lib/feeds';
import { site } from '../../src/site';

/* pickPrimaryLink — always the source, never the aggregator. */
describe('pickPrimaryLink()', () => {
  it('returns the source_url verbatim', () => {
    const p: FeedPick = { title: 't', source_url: 'https://example.com/a?x=1&y=2' };
    expect(pickPrimaryLink(p)).toBe('https://example.com/a?x=1&y=2');
  });

  it('ignores free_link and surfaces', () => {
    const p: FeedPick = {
      title: 't',
      source_url: 'https://src.example/post',
      free_link: 'https://free.example/post',
      surfaces: [{ url: 'https://news.ycombinator.com/item?id=9', name: 'HN' }],
    };
    expect(pickPrimaryLink(p)).toBe('https://src.example/post');
  });
});

/* pickFreeLink — a distinct backup read, or nothing. */
describe('pickFreeLink()', () => {
  it('returns the free_link when present and different from the source', () => {
    const p: FeedPick = {
      title: 't',
      source_url: 'https://src.example/post',
      free_link: 'https://free.example/post',
    };
    expect(pickFreeLink(p)).toBe('https://free.example/post');
  });

  it('returns null when free_link equals source_url', () => {
    const url = 'https://same.example/post';
    const p: FeedPick = { title: 't', source_url: url, free_link: url };
    expect(pickFreeLink(p)).toBeNull();
  });

  it('returns null when free_link is undefined', () => {
    const p: FeedPick = { title: 't', source_url: 'https://src.example/post' };
    expect(pickFreeLink(p)).toBeNull();
  });

  it('returns null when free_link is explicitly null', () => {
    const p: FeedPick = { title: 't', source_url: 'https://src.example/post', free_link: null };
    expect(pickFreeLink(p)).toBeNull();
  });

  it('returns null when free_link is an empty string', () => {
    const p: FeedPick = { title: 't', source_url: 'https://src.example/post', free_link: '' };
    expect(pickFreeLink(p)).toBeNull();
  });
});

/* pickItemHtml — edge cases beyond the fully-populated pick in feeds.test.ts. */
describe('pickItemHtml() edge cases', () => {
  it('renders only the Read line for a bare pick (no why/notes/summary/free/surfaces)', () => {
    const p: FeedPick = { title: 'bare', source_url: 'https://src.example/post' };
    const html = pickItemHtml(p);
    expect(html).toBe(
      '<p><strong>Read</strong> · <a href="https://src.example/post">Primary source</a></p>'
    );
    // None of the optional sections should appear.
    expect(html).not.toContain('Why it matters');
    expect(html).not.toContain('Notes');
    expect(html).not.toContain('Free read');
    expect(html).not.toContain('paywalled');
    expect(html).not.toContain('Surfaced on');
  });

  it('omits the Notes block when notes is an empty array', () => {
    const p: FeedPick = { title: 't', source_url: 'https://src.example/post', notes: [] };
    expect(pickItemHtml(p)).not.toContain('Notes');
  });

  it('omits the Surfaced-on block when surfaces is an empty array', () => {
    const p: FeedPick = { title: 't', source_url: 'https://src.example/post', surfaces: [] };
    expect(pickItemHtml(p)).not.toContain('Surfaced on');
  });

  it('renders a surface with null points/comments as a bare name link', () => {
    const p: FeedPick = {
      title: 't',
      source_url: 'https://src.example/post',
      surfaces: [{ url: 'https://reddit.com/r/x', name: 'r/x', points: null, comments: null }],
    };
    const html = pickItemHtml(p);
    expect(html).toContain('<strong>Surfaced on</strong>');
    expect(html).toContain('<a href="https://reddit.com/r/x">r/x</a>');
    // No score/comment suffixes when both are null.
    expect(html).not.toContain('(');
    expect(html).not.toContain('c</a>');
  });

  it('shows points but no comment suffix when only points is present', () => {
    const p: FeedPick = {
      title: 't',
      source_url: 'https://src.example/post',
      surfaces: [{ url: 'https://hn.example/i', name: 'HN', points: 250, comments: null }],
    };
    const html = pickItemHtml(p);
    expect(html).toContain('HN (250)</a>');
    expect(html).not.toContain('c</a>');
  });

  it('escapes surface url and name', () => {
    const p: FeedPick = {
      title: 't',
      source_url: 'https://src.example/post',
      surfaces: [{ url: 'https://x.example/?a=1&b=2', name: 'A & <B>' }],
    };
    const html = pickItemHtml(p);
    expect(html).toContain('href="https://x.example/?a=1&amp;b=2"');
    expect(html).toContain('A &amp; &lt;B&gt;');
  });
});

/* digestItemHtml — blurb + KIND_LABEL + on-site link, all escaped. */
describe('digestItemHtml()', () => {
  const d: FeedDigest = {
    kind: 'daily',
    period: '2026-07-04',
    blurb: 'The day, in "brief" & <sharp>.',
  };
  const url = 'https://eclecta.co/daily/2026-07-04/?ref=a&b';
  const html = digestItemHtml(d, url);

  it('wraps the escaped blurb in an emphasized paragraph', () => {
    expect(html).toContain('<p><em>The day, in &quot;brief&quot; &amp; &lt;sharp&gt;.</em></p>');
  });

  it('shows the KIND_LABEL for the digest kind', () => {
    expect(html).toContain('Daily brief');
  });

  it('shows the period', () => {
    expect(html).toContain('2026-07-04');
  });

  it('links to the on-site edition, labelled with the site name', () => {
    expect(html).toContain(`<a href="https://eclecta.co/daily/2026-07-04/?ref=a&amp;b">`);
    expect(html).toContain(`Read on ${site.name}</a>`);
  });

  it('uses the right KIND_LABEL for other kinds', () => {
    expect(digestItemHtml({ kind: 'yearly', period: '2026', blurb: 'x' }, '/y/')).toContain(
      'The year'
    );
    expect(
      digestItemHtml({ kind: 'quarterly', period: '2026-Q2', blurb: 'x' }, '/q/')
    ).toContain('Quarterly report');
  });
});

/* FEED_STYLESHEET — base-aware; BASE_URL is '/' under vitest. */
describe('FEED_STYLESHEET', () => {
  it('resolves to the base-rooted stylesheet path', () => {
    expect(FEED_STYLESHEET).toBe('/rss/styles.xsl');
  });
});

/* getFeed — lookup by slug, hard failure on typos. */
describe('getFeed()', () => {
  it('returns the matching feed definition', () => {
    const feed = getFeed('everything');
    expect(feed.slug).toBe('everything');
    expect(feed.path).toBe('/rss.xml');
    expect(feed).toBe(FEEDS.find((f) => f.slug === 'everything'));
  });

  it('throws with the offending slug on an unknown slug', () => {
    expect(() => getFeed('does-not-exist')).toThrow('Unknown feed slug: does-not-exist');
  });
});
