import { describe, expect, it } from 'vitest';
import { href, absUrl, KINDS, KIND_LABEL, site, type DigestKind } from '../../src/site';

// Under vitest, import.meta.env.BASE_URL === '/', so href strips the trailing
// slash off the base (-> '') and then prefixes a single leading slash.

describe('href()', () => {
  it('keeps an already-rooted path intact (base "/" -> "")', () => {
    expect(href('/about/')).toBe('/about/');
    expect(href('/')).toBe('/');
    expect(href('/rss.xml')).toBe('/rss.xml');
  });

  it('adds a leading slash to a relative path', () => {
    expect(href('about/')).toBe('/about/');
    expect(href('foo')).toBe('/foo');
  });

  it('maps the empty string to the site root', () => {
    // '' does not start with '/', so it becomes '/' + '' === '/'
    expect(href('')).toBe('/');
  });

  it('does not collapse or add extra slashes beyond the single leading one', () => {
    // nested rooted path passes straight through
    expect(href('/a/b/c/')).toBe('/a/b/c/');
    // relative nested gets exactly one leading slash
    expect(href('a/b')).toBe('/a/b');
  });
});

describe('absUrl()', () => {
  it('prefixes the origin to the href (string siteUrl)', () => {
    expect(absUrl('/about/', 'https://eclecta.co')).toBe('https://eclecta.co/about/');
  });

  it('strips a trailing slash from the origin before joining', () => {
    expect(absUrl('/about/', 'https://eclecta.co/')).toBe('https://eclecta.co/about/');
    expect(absUrl('/about/', 'https://eclecta.co///')).toBe('https://eclecta.co/about/');
  });

  it('accepts a URL object (String() adds a trailing slash which is stripped)', () => {
    expect(absUrl('/rss.xml', new URL('https://eclecta.co'))).toBe('https://eclecta.co/rss.xml');
    expect(absUrl('/rss.xml', new URL('https://eclecta.co/sub/'))).toBe('https://eclecta.co/sub/rss.xml');
  });

  it('returns just the href when siteUrl is undefined', () => {
    expect(absUrl('/about/', undefined)).toBe('/about/');
    expect(absUrl('about/', undefined)).toBe('/about/');
  });

  it('applies href normalization to relative paths under an origin', () => {
    expect(absUrl('feed.xml', 'https://eclecta.co')).toBe('https://eclecta.co/feed.xml');
  });

  it('joins root path to a bare origin', () => {
    expect(absUrl('/', 'https://eclecta.co')).toBe('https://eclecta.co/');
  });
});

describe('KINDS + KIND_LABEL', () => {
  it('exposes the expected digest cadence order', () => {
    expect(KINDS).toEqual(['daily', 'weekly', 'monthly', 'quarterly', 'yearly']);
  });

  it('has no duplicate kinds', () => {
    expect(new Set(KINDS).size).toBe(KINDS.length);
  });

  it('maps each KIND to its exact display label', () => {
    // Pins the concrete cadence copy: this is what renders in the UI.
    expect(KIND_LABEL).toEqual({
      daily: 'Daily brief',
      weekly: 'Weekly digest',
      monthly: 'Monthly review',
      quarterly: 'Quarterly report',
      yearly: 'The year',
    });
    // Every KIND resolves to that exact label (no undefined gaps).
    expect(KINDS.map((k) => KIND_LABEL[k as DigestKind])).toEqual([
      'Daily brief',
      'Weekly digest',
      'Monthly review',
      'Quarterly report',
      'The year',
    ]);
  });

  it('has no extra label keys beyond KINDS', () => {
    expect(Object.keys(KIND_LABEL).sort()).toEqual([...KINDS].sort());
  });

  it('has distinct labels', () => {
    const labels = Object.values(KIND_LABEL);
    expect(new Set(labels).size).toBe(labels.length);
  });
});

describe('site constant', () => {
  it('names the publication Eclecta with the eclecta storage prefix', () => {
    expect(site.name).toBe('Eclecta');
    expect(site.storagePrefix).toBe('eclecta');
  });

  it('points identity URLs at their real https destinations', () => {
    expect(site.authorUrl).toBe('https://starikov.co');
    expect(site.contactUrl).toBe('https://starikov.co/contact/');
    expect(site.repoUrl).toBe('https://github.com/IllyaStarikov/eclecta');
    // Belt-and-suspenders: nothing here is ever plain http.
    for (const url of [site.authorUrl, site.contactUrl, site.repoUrl]) {
      expect(url).toMatch(/^https:\/\//);
    }
  });

  it('carries the exact identity copy', () => {
    expect(site.author).toBe('Illya Starikov');
    expect(site.kicker).toBe('The frontier, distilled');
    expect(site.tagline).toBe('We read the firehose, so you read what matters.');
    // Description is a multi-part concatenation; pin its stable opening and
    // closing clauses rather than the full em-dash-laden paragraph.
    expect(site.description).toContain(
      'Eclecta watches the places technology, AI, and the sciences break first',
    );
    expect(site.description).toContain('A daily brief, a weekly digest, and the long view.');
  });
});
