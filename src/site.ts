/**
 * The ONE place the publication's identity lives.
 * Renaming the site = edit this file (+ `site.name` in the pipeline's
 * config/signal.json, the GitHub repo name, and public/CNAME when the
 * custom domain lands).
 */
export const site = {
  name: 'Lede',
  kicker: 'Technology · AI · Science',
  tagline: 'We read the firehose, so you read the lede.',
  description:
    'Lede watches the places technology, AI, and the sciences break first — ' +
    'thousands of sources — and distills what actually matters, with a clear ' +
    'account of why. A daily brief, a weekly digest, and the long view.',
  author: 'Illya Starikov',
  authorUrl: 'https://starikov.co',
  contactUrl: 'https://starikov.co/contact/',
  storagePrefix: 'lede',
} as const;

export const KINDS = ['daily', 'weekly', 'monthly', 'quarterly', 'yearly'] as const;
export type DigestKind = (typeof KINDS)[number];

export const KIND_LABEL: Record<DigestKind, string> = {
  daily: 'Daily brief',
  weekly: 'Weekly digest',
  monthly: 'Monthly review',
  quarterly: 'Quarterly report',
  yearly: 'The year',
};

/** Base-aware internal href ('/about/' -> '/lede/about/' on project pages). */
export function href(path: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/+$/, '');
  return base + (path.startsWith('/') ? path : '/' + path);
}

/** Absolute URL for feeds/meta: site origin + base + path. */
export function absUrl(path: string, siteUrl: URL | string | undefined): string {
  const origin = siteUrl ? String(siteUrl).replace(/\/+$/, '') : '';
  return origin + href(path);
}
