/**
 * The rail's editions ledger: the latest edition of every cadence, daily
 * first. One helper so the front page, category pages, and sub pages carry
 * the identical block — the reader learns it once.
 */
import { getCollection } from 'astro:content';
import { href, KIND_LABEL, KINDS } from '../site';

export interface EditionsBlock {
  title: string;
  items: { label: string; href: string; meta: string }[];
  moreLabel: string;
  moreHref: string;
}

const fmt = (d: Date) =>
  d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });

/** Null while no digest has ever published, so rails render nothing. */
export async function latestEditions(): Promise<EditionsBlock | null> {
  const digests = (await getCollection('digests')).sort(
    (a, b) => b.data.date.valueOf() - a.data.date.valueOf()
  );
  const items = KINDS.map((k) => digests.find((d) => d.data.kind === k))
    .filter((d): d is (typeof digests)[number] => Boolean(d))
    .map((d) => ({
      label: KIND_LABEL[d.data.kind],
      href: href(`/digests/${d.id}/`),
      meta: fmt(d.data.date),
    }));
  if (items.length === 0) return null;
  return {
    title: 'Editions',
    items,
    moreLabel: 'All editions',
    moreHref: href('/archive/'),
  };
}
