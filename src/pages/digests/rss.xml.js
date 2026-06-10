import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import { absUrl, KIND_LABEL } from '../../site';
import { getFeed, digestItemHtml } from '../../lib/feeds';

export async function GET(context) {
  const feed = getFeed('digests');
  const digests = (await getCollection('digests')).sort(
    (a, b) => b.data.date.valueOf() - a.data.date.valueOf()
  );
  return rss({
    title: feed.title,
    description: feed.description,
    site: context.site,
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
