import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import { absUrl, KINDS, KIND_LABEL } from '../../../site';
import { getFeed, digestItemHtml, FEED_STYLESHEET } from '../../../lib/feeds';

export function getStaticPaths() {
  return KINDS.map((kind) => ({ params: { kind } }));
}

export async function GET(context) {
  const { kind } = context.params;
  const feed = getFeed(`digests-${kind}`);
  const digests = (await getCollection('digests'))
    .filter((d) => d.data.kind === kind)
    .sort((a, b) => b.data.date.valueOf() - a.data.date.valueOf());
  return rss({
    stylesheet: FEED_STYLESHEET,
    title: feed.title,
    description: feed.description,
    site: new URL(import.meta.env.BASE_URL, context.site).href,
    items: digests.map((d) => {
      const url = absUrl(`/digests/${d.id}/`, context.site);
      return {
        title: `${KIND_LABEL[d.data.kind]} · ${d.data.title}`,
        link: url,
        pubDate: d.data.date,
        description: d.data.blurb,
        content: digestItemHtml(d.data, url),
      };
    }),
    customData: '<language>en-us</language>',
  });
}
