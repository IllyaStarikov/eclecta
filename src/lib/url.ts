/**
 * Neutralize URLs that arrive from machine-written, third-party-derived data.
 * picks.json / spotlight.json are built from ~1,500 uncontrolled feeds; a
 * hostile feed can serve `<link>javascript:…</link>`, and Astro's attribute
 * binding escapes quotes but NOT the URL scheme — so a raw bind renders a
 * clickable `href="javascript:…"` on eclecta.co (stored XSS).
 *
 * Only http(s) absolute URLs and site-relative paths (`/…`, `#…`) may become
 * an href; anything else returns undefined so the sink omits the link instead
 * of rendering the vector. Sanitizing at RENDER — not hard-rejecting in the
 * zod schema — is deliberate: one hostile URL in a 90-minute republish would
 * otherwise fail the whole build and take the site down. Parsing via `URL`
 * (not substring matching) matches the browser's own tab/newline stripping,
 * so `java\tscript:` is caught too.
 */
export function safeUrl(u: string | null | undefined): string | undefined {
  if (!u) return undefined;
  const s = u.trim();
  if (s === '') return undefined;
  // Site-relative paths and pure fragments cannot carry a scheme. Guard against
  // protocol-relative '//host' (an OFF-site navigation, not a local path).
  if (s.startsWith('#')) return s;
  if (s.startsWith('/') && !s.startsWith('//')) return s;
  try {
    const proto = new URL(s).protocol;
    return proto === 'http:' || proto === 'https:' ? s : undefined;
  } catch {
    // Not an absolute URL (protocol-relative `//host`, bare word, malformed).
    return undefined;
  }
}
