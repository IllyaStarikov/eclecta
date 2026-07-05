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

  it('has a non-empty label for every KIND', () => {
    for (const kind of KINDS) {
      const label = KIND_LABEL[kind as DigestKind];
      expect(typeof label).toBe('string');
      expect(label.length).toBeGreaterThan(0);
    }
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

  it('exposes https:// URLs', () => {
    for (const url of [site.authorUrl, site.contactUrl, site.repoUrl]) {
      expect(url).toMatch(/^https:\/\//);
    }
  });

  it('has non-empty identity copy', () => {
    for (const field of [site.kicker, site.tagline, site.description, site.author]) {
      expect(typeof field).toBe('string');
      expect(field.length).toBeGreaterThan(0);
    }
  });
});
