/** Sitemap lastmod is derived purely from digest slugs; see src/lib/sitemap.mjs. */
import { describe, expect, it } from 'vitest';
import { lastmodForUrl } from '../../src/lib/sitemap.mjs';

describe('lastmodForUrl', () => {
  it('daily: the date itself', () => {
    expect(lastmodForUrl('https://eclecta.co/digests/daily/2026-06-23/')).toBe('2026-06-23');
  });
  it('weekly: the Sunday closing the ISO week', () => {
    expect(lastmodForUrl('https://eclecta.co/digests/weekly/2026-w27/')).toBe('2026-07-05');
  });
  it('monthly: the last day of the month', () => {
    expect(lastmodForUrl('https://eclecta.co/digests/monthly/2026-06/')).toBe('2026-06-30');
  });
  it('quarterly: the last day of the quarter', () => {
    expect(lastmodForUrl('https://eclecta.co/digests/quarterly/2026-q2/')).toBe('2026-06-30');
  });
  it('yearly: Dec 31', () => {
    expect(lastmodForUrl('https://eclecta.co/digests/yearly/2026/')).toBe('2026-12-31');
  });
  it('non-digest URLs: null', () => {
    expect(lastmodForUrl('https://eclecta.co/ai/')).toBeNull();
    expect(lastmodForUrl('https://eclecta.co/archive/')).toBeNull();
  });
  it('garbage periods: null, never a crash', () => {
    expect(lastmodForUrl('https://eclecta.co/digests/daily/not-a-date/')).toBeNull();
  });
});
