import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import picks from '../data/picks.json';
import { absUrl, KIND_LABEL } from '../site';
import { getFeed, pickItemHtml, pickPrimaryLink, digestItemHtml, FEED_STYLESHEET } from '../lib/feeds';

export async function GET(context) {
  const feed = getFeed('everything');
  const digests = (await getCollection('digests')).sort(
    (a, b) => b.data.date.valueOf() - a.data.date.valueOf()
  );
  const items = [
    ...digests.map((d) => {
      const url = absUrl(`/digests/${d.id}/`, context.site);
      return {
        title: `${KIND_LABEL[d.data.kind]} · ${d.data.title}`,
        link: url,
        pubDate: d.data.date,
        description: d.data.blurb,
        content: digestItemHtml(d.data, url),
      };
    }),
    ...picks.map((p) => ({
      title: p.title,
      link: pickPrimaryLink(p),
      pubDate: p.curated_at ? new Date(p.curated_at) : undefined,
      description: p.why || '',
      content: pickItemHtml(p),
    })),
  ];
  return rss({
    stylesheet: FEED_STYLESHEET,
    title: feed.title,
    description: feed.description,
    site: new URL(import.meta.env.BASE_URL, context.site).href,
    items,
    customData: '<language>en-us</language>',
  });
}
