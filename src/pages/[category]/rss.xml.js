import rss from '@astrojs/rss';
import picks from '../../data/picks.json';
import { absUrl } from '../../site';
import { CATEGORIES, deriveCategory } from '../../lib/taxonomy';
import { getFeed, pickItemHtml, pickPrimaryLink, FEED_STYLESHEET } from '../../lib/feeds';

export function getStaticPaths() {
  return CATEGORIES.map((c) => ({ params: { category: c.slug }, props: { category: c } }));
}

export function GET(context) {
  const { category } = context.props;
  const feed = getFeed(`cat-${category.slug}`);
  const list = picks.filter(
    (p) => deriveCategory(p.title, p.channels).category === category.slug
  );
  return rss({
    stylesheet: FEED_STYLESHEET,
    title: feed.title,
    description: feed.description,
    site: new URL(import.meta.env.BASE_URL, context.site).href,
    items: list.map((p) => ({
      title: p.title,
      link: pickPrimaryLink(p),
      pubDate: p.curated_at ? new Date(p.curated_at) : undefined,
      description: p.why || '',
      content: pickItemHtml(p),
    })),
    customData:
      '<language>en-us</language>' +
      `<docs>${absUrl(`/${category.slug}/`, context.site)}</docs>`,
  });
}
