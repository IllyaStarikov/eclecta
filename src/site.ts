/**
 * The ONE place the publication's identity lives.
 * Renaming the site = edit this file (+ `site.name` in the pipeline's
 * config/signal.json, the GitHub repo name, and public/CNAME).
 */
export const site = {
  name: 'Eclecta',
  kicker: 'The frontier, distilled',
  tagline: 'We read the firehose, so you read what matters.',
  description:
    'Eclecta watches the places technology, AI, and the sciences break first, ' +
    'thousands of sources at a time, and distills what actually matters, with ' +
    'a clear account of why. A daily brief, a weekly digest, and the long view.',
  author: 'Illya Starikov',
  authorUrl: 'https://starikov.co',
  repoUrl: 'https://github.com/IllyaStarikov/eclecta',
  contactUrl: 'https://starikov.co/contact/',
  storagePrefix: 'eclecta',
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

/** Base-aware internal href ('/about/' -> '<base>/about/'; base is '/' on eclecta.co). */
export function href(path: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/+$/, '');
  return base + (path.startsWith('/') ? path : '/' + path);
}

/** Absolute URL for feeds/meta: site origin + base + path. */
export function absUrl(path: string, siteUrl: URL | string | undefined): string {
  const origin = siteUrl ? String(siteUrl).replace(/\/+$/, '') : '';
  return origin + href(path);
}
