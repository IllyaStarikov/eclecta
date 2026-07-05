/**
 * Sitemap lastmod, derived from digest slugs (the slug IS the date, so no
 * frontmatter parsing in astro.config). Plain .mjs: astro.config.mjs cannot
 * import TypeScript. Unit-tested in tests/unit/sitemap.test.ts.
 */

/** ISO date (UTC) a digest period was last touched, or null for non-digests. */
export function lastmodForUrl(url) {
  const m = url.match(/\/digests\/(daily|weekly|monthly|quarterly|yearly)\/([a-z0-9-]+)\/?$/i);
  if (!m) return null;
  const period = m[2];

  let d = null;
  let mm;
  if ((mm = period.match(/^(\d{4})-(\d{2})-(\d{2})$/))) {
    // daily: the date itself
    d = new Date(Date.UTC(+mm[1], +mm[2] - 1, +mm[3]));
  } else if ((mm = period.match(/^(\d{4})-w(\d{2})$/i))) {
    // weekly: the Sunday closing ISO week N (Jan 4 anchors week 1)
    const jan4 = new Date(Date.UTC(+mm[1], 0, 4));
    const week1Monday = new Date(jan4);
    week1Monday.setUTCDate(jan4.getUTCDate() - ((jan4.getUTCDay() + 6) % 7));
    d = new Date(week1Monday);
    d.setUTCDate(week1Monday.getUTCDate() + (+mm[2] - 1) * 7 + 6);
  } else if ((mm = period.match(/^(\d{4})-(\d{2})$/))) {
    // monthly: last day of the month
    d = new Date(Date.UTC(+mm[1], +mm[2], 0));
  } else if ((mm = period.match(/^(\d{4})-q([1-4])$/i))) {
    // quarterly: last day of the quarter
    d = new Date(Date.UTC(+mm[1], +mm[2] * 3, 0));
  } else if ((mm = period.match(/^(\d{4})$/))) {
    // yearly: Dec 31
    d = new Date(Date.UTC(+mm[1], 11, 31));
  }
  return d && !Number.isNaN(d.getTime()) ? d.toISOString().slice(0, 10) : null;
}
